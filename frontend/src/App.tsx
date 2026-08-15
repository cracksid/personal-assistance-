/**
 * The whole app.
 *
 * Everything about the connection lives in useJarvis; everything about
 * drawing lives in the components. This file joins them and owns one small
 * piece of behaviour of its own: keeping the view scrolled to the bottom.
 */

import { useEffect, useRef, useState } from "react";

import { Composer } from "./components/Composer";
import { Panel } from "./components/Panel";
import { Reactor } from "./components/Reactor";
import { ShaderBackground } from "./components/ShaderBackground";
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
  const [panelOpen, setPanelOpen] = useState(false);

  // Scroll down whenever anything is added. Runs after the browser has laid
  // the new content out, which is exactly what useEffect guarantees --
  // doing it during render would scroll to where the content was, not where
  // it now is.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries]);

  return (
    <div className="app">
      <ShaderBackground />

      <header>
        <div className="brand">
          <Reactor status={status} active={busy} />
          <div className="titles">
            <h1>J.A.R.V.I.S.</h1>
            <span className="subtitle">
              {status === "open"
                ? busy
                  ? "PROCESSING"
                  : "ONLINE"
                : status === "connecting"
                  ? "LINKING"
                  : "OFFLINE"}
              {conversationId !== null && ` · THREAD ${conversationId}`}
            </span>
          </div>
        </div>

        <div className="actions">
        <button
          className="panel-open"
          onClick={() => setPanelOpen(true)}
          title="Settings and memory"
        >
          ⚙ CONFIG
        </button>

        <button
          className="new-chat"
          onClick={newConversation}
          disabled={status !== "open"}
          // The control that did not exist until Phase 13. Resuming the
          // newest conversation is right for "close the tab and come back"
          // and wrong for "drop this thread" -- and with no way to drop
          // one, a stale detail in the history followed you around for
          // good.
          title="Start a new conversation"
        >
          NEW THREAD
        </button>
        </div>
      </header>

      <Panel open={panelOpen} onClose={() => setPanelOpen(false)} />

      <main>
        <Transcript entries={entries} onAnswer={answerConfirmation} />
        <div ref={bottomRef} />
      </main>

      {status === "closed" && (
        <div className="banner">
          CONNECTION LOST — is the backend running? Reload to reconnect.
        </div>
      )}

      <Composer onSend={sendMessage} disabled={status !== "open"} busy={busy} />
    </div>
  );
}
