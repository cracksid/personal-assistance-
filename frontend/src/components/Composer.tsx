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

interface Props {
  onSend: (text: string) => void;
  disabled: boolean;
  busy: boolean;
}

export function Composer({ onSend, disabled, busy }: Props) {
  const [text, setText] = useState("");
  const boxRef = useRef<HTMLTextAreaElement>(null);

  // Grow with the text, up to a limit. Reset to "auto" first, otherwise
  // scrollHeight only ever reports the current height and the box can grow
  // but never shrink again.
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight, 200)}px`;
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
      <textarea
        ref={boxRef}
        rows={1}
        value={text}
        placeholder={disabled ? "Not connected" : "Message JARVIS…"}
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
