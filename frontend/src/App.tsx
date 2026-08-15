/**
 * The whole app.
 *
 * Everything about the connection lives in useJarvis; everything about
 * drawing lives in the components. This file joins them and owns one small
 * piece of behaviour of its own: keeping the view scrolled to the bottom.
 */

import { useEffect, useRef } from "react";

import { Composer } from "./components/Composer";
import { Transcript } from "./components/Transcript";
import { useJarvis } from "./lib/useJarvis";

export default function App() {
  const {
    entries,
    status,
    conversationId,
    busy,
    sendMessage,
    answerConfirmation,
    newConversation,
  } = useJarvis();

  const bottomRef = useRef<HTMLDivElement>(null);

  // Scroll down whenever anything is added. Runs after the browser has laid
  // the new content out, which is exactly what useEffect guarantees --
  // doing it during render would scroll to where the content was, not where
  // it now is.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries]);

  return (
    <div className="app">
      <header>
        <div className="brand">
          <span className={`dot ${status}`} />
          <h1>JARVIS</h1>
          {conversationId !== null && (
            <span className="conversation">thread #{conversationId}</span>
          )}
        </div>

        <button
          className="new-chat"
          onClick={newConversation}
          disabled={status !== "open"}
          // The control that did not exist until now. Resuming the newest
          // conversation is right for "close the tab and come back" and
          // wrong for "drop this thread" -- and with no way to drop one, a
          // stale detail in the history followed you around for good.
          title="Start a new conversation"
        >
          New chat
        </button>
      </header>

      <main>
        <Transcript entries={entries} onAnswer={answerConfirmation} />
        <div ref={bottomRef} />
      </main>

      {status === "closed" && (
        <div className="banner">
          Disconnected. Is the backend running? Reload the page to reconnect.
        </div>
      )}

      <Composer onSend={sendMessage} disabled={status !== "open"} busy={busy} />
    </div>
  );
}
