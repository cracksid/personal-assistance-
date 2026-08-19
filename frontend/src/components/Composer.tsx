/**
 * The box you type in.
 *
 * A "CONTROLLED COMPONENT".
 *
 * The textarea's value comes from React state, and every keystroke calls
 * setText, which re-renders and puts the new value back. That sounds like a
 * detour -- the browser can hold text by itself -- but it means React always
 * knows what is in the box, so clearing it after send is one line and
 * cannot get out of step with what is on screen.
 */

import { KeyboardEvent, useEffect, useRef, useState } from "react";

/** Tallest the box grows before it starts scrolling instead. */
const MAX_HEIGHT = 200;

interface Props {
  onSend: (text: string) => void;
  disabled: boolean;
  busy: boolean;

  // Voice. Optional so the composer still works in a browser with no
  // microphone, and so nothing here needs to know how speech works.
  micState?: "idle" | "recording" | "transcribing" | "speaking";

  // Text to drop into the box -- a finished transcript. Carries an id so
  // the effect below can tell a NEW transcript from a re-render: saying the
  // same words twice must insert them twice.
  insert?: { id: string; text: string } | null;
  onMicDown?: () => void;
  onMicUp?: () => void;
  micSupported?: boolean;
}

export function Composer({
  onSend,
  disabled,
  busy,
  micState = "idle",
  insert = null,
  onMicDown,
  onMicUp,
  micSupported = false,
}: Props) {
  const [text, setText] = useState("");
  const boxRef = useRef<HTMLTextAreaElement>(null);

  const recording = micState === "recording";
  const transcribing = micState === "transcribing";

  // Append a finished transcript and focus the box.
  //
  // It fills the composer rather than sending. Whisper mishears names and
  // technical words, and a wrong sentence sent automatically is a wrong
  // sentence acted on -- by an assistant holding filesystem tools. The box
  // is focused, so confirming is one keystroke.
  const lastInsertRef = useRef<string | null>(null);
  useEffect(() => {
    if (!insert || insert.id === lastInsertRef.current) return;
    lastInsertRef.current = insert.id;
    setText((prev) => (prev ? `${prev.trimEnd()} ${insert.text}` : insert.text));
    boxRef.current?.focus();
  }, [insert]);

  // Grow with the text, up to a limit. Reset to "auto" first, otherwise
  // scrollHeight only ever reports the current height and the box can grow
  // but never shrink again.
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    box.style.height = "auto";
    const wanted = box.scrollHeight;
    box.style.height = `${Math.min(wanted, MAX_HEIGHT)}px`;
    // Scroll only once the box has stopped growing. Leaving overflow on
    // permanently gives a one-line input a full scrollbar with arrows,
    // which looks broken and steals horizontal space for nothing.
    box.style.overflowY = wanted > MAX_HEIGHT ? "auto" : "hidden";
  }, [text]);

  function submit() {
    if (!text.trim() || disabled) return;
    onSend(text);
    setText("");
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift+Enter makes a new line -- the convention every
    // chat app uses, so it is what fingers already expect.
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="composer">
      {micSupported && onMicDown && onMicUp && (
        <button
          className={`mic ${recording ? "recording" : ""}`}
          disabled={disabled || transcribing}
          // PUSH TO TALK: held down, not toggled. Holding a key makes the
          // recording's start and end unambiguous, and there is no state
          // where JARVIS is listening and you have forgotten -- letting go
          // is the same gesture as being finished.
          onPointerDown={onMicDown}
          onPointerUp={onMicUp}
          // Leaving the button while held still ends the recording, rather
          // than stranding it open because the pointer slipped.
          onPointerLeave={() => recording && onMicUp()}
          title={recording ? "Release to send" : "Hold to speak"}
        >
          {transcribing ? "..." : recording ? "REC" : "MIC"}
        </button>
      )}

      <textarea
        ref={boxRef}
        rows={1}
        value={text}
        placeholder={
          disabled
            ? "Not connected"
            : recording
              ? "Listening…"
              : transcribing
                ? "Transcribing…"
                : "Message JARVIS…"
        }
        disabled={disabled}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <button onClick={submit} disabled={disabled || !text.trim()}>
        {busy ? "…" : "Send"}
      </button>
    </div>
  );
}
