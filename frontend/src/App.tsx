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
import { useVoice } from "./lib/useVoice";

export default function App() {
  const {
    entries,
    status,
    conversationId,
    busy,
    lastReply,
    sendMessage,
    answerConfirmation,
    newConversation,
  } = useJarvis();

  const bottomRef = useRef<HTMLDivElement>(null);
  const [panelOpen, setPanelOpen] = useState(false);

  const voice = useVoice();
  const [transcript, setTranscript] = useState<{ id: string; text: string } | null>(
    null,
  );

  // Whether replies are spoken aloud. Off by default: an assistant that
  // starts talking unprompted is startling, and this one also delivers
  // reminders and scheduled task results on its own.
  const [speakReplies, setSpeakReplies] = useState(false);

  // Speak each reply once it is complete. Piper needs a whole sentence, and
  // a reply arrives as dozens of chunks -- so this waits for the turn to
  // finish rather than trying to speak as it streams.
  const spokenRef = useRef<string | null>(null);
  useEffect(() => {
    if (!speakReplies || !lastReply) return;
    if (spokenRef.current === lastReply.id) return;
    spokenRef.current = lastReply.id;
    voice.speak(lastReply.text);
  }, [speakReplies, lastReply, voice]);

  async function finishRecording() {
    const said = await voice.stopAndTranscribe();
    if (said) setTranscript({ id: `${Date.now()}`, text: said });
  }

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
          className={`speak-toggle ${speakReplies ? "on" : ""}`}
          onClick={() => {
            if (speakReplies) voice.stopSpeaking();
            setSpeakReplies((on) => !on);
          }}
          title={speakReplies ? "Replies are spoken aloud" : "Replies are silent"}
        >
          {speakReplies ? "🔊 VOICE" : "🔇 VOICE"}
        </button>

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
          <span>CONNECTION LOST — is the backend running?</span>
          {/* A button rather than "press F5", because in the Electron shell
              there is no F5: the menu bar is hidden and there is no reload
              control. reload() works in both a browser tab and a window. */}
          <button onClick={() => window.location.reload()}>RETRY</button>
        </div>
      )}

      {voice.error && (
        <div className="banner">
          <span>{voice.error}</span>
          <button onClick={voice.clearError}>DISMISS</button>
        </div>
      )}

      <Composer
        onSend={sendMessage}
        disabled={status !== "open"}
        busy={busy}
        micState={voice.state}
        micSupported={voice.supported}
        insert={transcript}
        onMicDown={voice.start}
        onMicUp={finishRecording}
      />
    </div>
  );
}
