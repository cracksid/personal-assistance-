/**
 * The WebSocket connection, as a React hook.
 *
 * WHAT A HOOK IS.
 *
 * A React component is a function that returns what should be on screen. It
 * is called again every time anything it displays changes, so it cannot keep
 * anything in ordinary local variables -- those are recreated on every call.
 *
 * Hooks are how a component remembers things between calls:
 *
 *   useState   a value React stores for you. Changing it re-runs the
 *              component, which is what puts the new value on screen.
 *   useRef     a box React stores for you that does NOT re-run anything
 *              when it changes. For things the screen does not show --
 *              here, the socket itself.
 *   useEffect  code that runs after rendering, for things outside React:
 *              opening a socket, setting a timer. It can return a cleanup
 *              function, which React calls before re-running it or when the
 *              component goes away.
 *   useCallback  keeps a function identity stable between renders, so
 *              passing it to a child does not make the child re-render for
 *              no reason.
 *
 * A "custom hook" is just a function that calls those. Putting the socket
 * in one keeps every component below free of connection logic -- they
 * receive a list to render and functions to call.
 *
 * WHY THE SOCKET LIVES IN A REF AND NOT IN STATE.
 *
 * Putting it in useState would re-render the whole app every time the socket
 * object changed identity, for a value nothing draws. A ref is the right
 * tool for "I need to keep this, nobody displays it".
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { Entry, Frame, Status, nextId } from "./protocol";

/**
 * How many times to retry before showing the connection as dead.
 *
 * With the backoff below this works out at roughly two minutes of trying,
 * which comfortably covers a backend restart and stops well short of
 * retrying forever into a machine that has gone to sleep.
 */
const MAX_RECONNECT_ATTEMPTS = 10;
const MAX_RECONNECT_DELAY_MS = 15_000;

/**
 * Relative, deliberately -- no host and no port anywhere in the app.
 *
 * In development Vite proxies this to 127.0.0.1:8000; in production the same
 * server serves both. window.location supplies the rest, and swapping ws://
 * for wss:// keeps it correct if this is ever served over HTTPS.
 */
function socketUrl(): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/ws/chat`;
}

export function useJarvis() {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [status, setStatus] = useState<Status>("connecting");
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  // The assistant message that most recently FINISHED, with an id so a
  // caller can tell a new reply from a re-render. Text-to-speech reads
  // this: speaking has to wait for the whole reply, because a sentence
  // arrives as dozens of chunks and Piper needs a complete one.
  const [lastReply, setLastReply] = useState<{ id: string; text: string } | null>(
    null,
  );

  const socketRef = useRef<WebSocket | null>(null);

  // Whether the assistant message currently being built is still open. A ref
  // rather than state because it changes on every chunk -- dozens of times a
  // second -- and nothing renders it.
  const streamingRef = useRef(false);

  const append = useCallback((entry: Entry) => {
    // The updater form (prev => ...) rather than [...entries, entry].
    // Chunks arrive faster than React re-renders, so a version of `entries`
    // captured when this function was created would already be stale and
    // updates would be lost. `prev` is always the latest.
    setEntries((prev) => [...prev, entry]);
  }, []);

  const handleFrame = useCallback(
    (frame: Frame) => {
      switch (frame.type) {
        case "chunk": {
          setEntries((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.kind === "assistant" && last.streaming) {
              // Append to the open message. A new array and a new object,
              // never a mutation: React decides what changed by comparing
              // references, so editing in place would render nothing.
              const updated = [...prev];
              updated[updated.length - 1] = { ...last, text: last.text + frame.text };
              return updated;
            }
            return [
              ...prev,
              {
                kind: "assistant",
                id: nextId("a"),
                text: frame.text,
                streaming: true,
              },
            ];
          });
          streamingRef.current = true;
          break;
        }

        case "tool":
          // A tool result closes the current message: whatever the model
          // says next belongs after the tool, not glued onto the sentence
          // before it.
          streamingRef.current = false;
          setEntries((prev) => [
            ...closeStreaming(prev),
            {
              kind: "tool",
              id: nextId("t"),
              name: frame.tool_name,
              ok: frame.ok,
              output: frame.output,
            },
          ]);
          break;

        case "confirmation":
          streamingRef.current = false;
          setEntries((prev) => [
            ...closeStreaming(prev),
            {
              kind: "confirmation",
              id: nextId("c"),
              confirmationId: frame.confirmation_id,
              toolName: frame.tool_name,
              description: frame.description,
              status: "pending",
            },
          ]);
          break;

        case "done":
          streamingRef.current = false;
          setEntries((prev) => {
            const closed = closeStreaming(prev);
            const last = closed[closed.length - 1];
            if (last && last.kind === "assistant" && last.text.trim()) {
              setLastReply({ id: last.id, text: last.text });
            }
            return closed;
          });
          setBusy(false);
          break;

        case "error":
          streamingRef.current = false;
          setEntries((prev) => [
            ...closeStreaming(prev),
            { kind: "error", id: nextId("e"), text: frame.message },
          ]);
          setBusy(false);
          break;

        case "conversation":
          setConversationId(frame.id);
          break;

        case "reminder":
          append({ kind: "reminder", id: nextId("r"), text: frame.message });
          break;

        case "task_result":
          append({
            kind: "task",
            id: nextId("k"),
            name: frame.name,
            text: frame.text,
          });
          break;

        case "file_event":
          append({
            kind: "file",
            id: nextId("f"),
            change: frame.change,
            name: frame.name,
          });
          break;
      }
    },
    [append],
  );

  useEffect(() => {
    // StrictMode in development mounts, unmounts and remounts every
    // component once, on purpose, to surface missing cleanup. Without the
    // cleanup below that would leave a second live socket receiving every
    // reminder -- so this flag ignores frames from a socket already torn
    // down, and stops a pending reconnect from firing into nothing.
    let disposed = false;
    let socket: WebSocket | null = null;
    let retryTimer: number | undefined;
    let attempt = 0;

    /**
     * RECONNECTION EXISTS BECAUSE OF THE DESKTOP APP.
     *
     * In a browser, a dropped socket is survivable: the banner says so and
     * you press F5. In the Electron shell there is no F5 -- the menu bar is
     * hidden and there is no reload control -- so a single dropped
     * connection left the window permanently dead until the app was
     * restarted. Observed live: connected at 01:00, dropped at 01:14 while
     * the backend was still perfectly healthy, and never came back.
     *
     * Backoff rather than a tight retry loop: the usual reason for a drop
     * is the backend restarting, which takes a few seconds, and hammering
     * it while it boots achieves nothing.
     */
    const connect = () => {
      if (disposed) return;

      socket = new WebSocket(socketUrl());
      socketRef.current = socket;

      socket.onopen = () => {
        if (disposed) return;
        attempt = 0; // a good connection resets the backoff
        setStatus("open");
      };

      socket.onmessage = (event) => {
        if (disposed) return;
        try {
          handleFrame(JSON.parse(event.data) as Frame);
        } catch {
          // A malformed frame is a bug on the server, not something the
          // user can act on. Better to drop it than to blank the screen.
          console.warn("Ignoring an unparseable frame", event.data);
        }
      };

      socket.onclose = () => {
        if (disposed) return;
        setBusy(false);

        attempt += 1;
        if (attempt > MAX_RECONNECT_ATTEMPTS) {
          // Given up. "closed" is what turns the reactor red and shows the
          // banner -- a distinct state from "trying", so a brief blip does
          // not look like a dead app and a dead app does not look like a
          // blip.
          setStatus("closed");
          return;
        }

        setStatus("connecting");
        const delay = Math.min(1000 * 2 ** (attempt - 1), MAX_RECONNECT_DELAY_MS);
        retryTimer = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      disposed = true;
      window.clearTimeout(retryTimer);
      socket?.close();
    };
  }, [handleFrame]);

  const send = useCallback((text: string) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(text);
    return true;
  }, []);

  const sendMessage = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || !send(trimmed)) return;
      append({ kind: "user", id: nextId("u"), text: trimmed });
      setBusy(true);
    },
    [append, send],
  );

  const answerConfirmation = useCallback(
    (confirmationId: string, approve: boolean) => {
      if (!send(JSON.stringify({ type: approve ? "confirm" : "cancel", confirmation_id: confirmationId }))) {
        return;
      }
      setBusy(true);
      setEntries((prev) =>
        prev.map((entry) =>
          entry.kind === "confirmation" && entry.confirmationId === confirmationId
            ? { ...entry, status: approve ? "approved" : "declined" }
            : entry,
        ),
      );
    },
    [send],
  );

  const newConversation = useCallback(() => {
    if (!send(JSON.stringify({ type: "new" }))) return;
    // Cleared immediately rather than waiting for the server's reply. The
    // transcript is a view of this session, and the old thread still exists
    // in the database -- "new chat" starts one, it does not delete one.
    setEntries([]);
    setBusy(false);
  }, [send]);

  return {
    entries,
    status,
    conversationId,
    busy,
    lastReply,
    sendMessage,
    answerConfirmation,
    newConversation,
  };
}

/** Mark the open assistant message finished, if there is one. */
function closeStreaming(entries: Entry[]): Entry[] {
  const last = entries[entries.length - 1];
  if (!last || last.kind !== "assistant" || !last.streaming) return entries;
  const updated = [...entries];
  updated[updated.length - 1] = { ...last, streaming: false };
  return updated;
}
