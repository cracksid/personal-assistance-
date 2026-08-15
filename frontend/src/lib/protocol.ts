/**
 * The WebSocket protocol, in TypeScript.
 *
 * This mirrors what app/api/routes/chat.py sends. The two are written by
 * hand and kept in step by hand -- there is no generator -- so this file is
 * the place to look when a frame stops making sense.
 *
 * WHY A "DISCRIMINATED UNION" IS WORTH THE WORDS.
 *
 * Frame below is a union of several shapes, each with a different literal
 * `type`. TypeScript can narrow it: inside `if (frame.type === "chunk")` it
 * knows the value has `.text`, and it will refuse `frame.description`
 * because a chunk frame has no such field.
 *
 * That is the payoff for typing the protocol at all. The alternative --
 * treating every frame as a bag of optional fields -- compiles happily and
 * then renders "undefined" on screen when a field is renamed on the Python
 * side.
 */

export interface ChunkFrame {
  type: "chunk";
  text: string;
}

export interface ToolFrame {
  type: "tool";
  tool_name: string | null;
  ok: boolean;
  output: string;
}

export interface ConfirmationFrame {
  type: "confirmation";
  confirmation_id: string;
  tool_name: string;
  description: string;
}

export interface DoneFrame {
  type: "done";
}

export interface ErrorFrame {
  type: "error";
  message: string;
}

export interface ConversationFrame {
  type: "conversation";
  id: number;
}

/** Frames nobody asked for -- these arrive on their own. */

export interface ReminderFrame {
  type: "reminder";
  id: number;
  message: string;
  due_at: string;
}

export interface TaskResultFrame {
  type: "task_result";
  task_id: number;
  name: string;
  text: string;
  tools_used: string[];
}

export interface FileEventFrame {
  type: "file_event";
  change: string;
  path: string;
  name: string;
}

export type Frame =
  | ChunkFrame
  | ToolFrame
  | ConfirmationFrame
  | DoneFrame
  | ErrorFrame
  | ConversationFrame
  | ReminderFrame
  | TaskResultFrame
  | FileEventFrame;

/**
 * What the UI keeps on screen.
 *
 * Deliberately NOT the same shape as the frames. A reply arrives as dozens
 * of chunk frames and is one message; a confirmation is a frame and a card
 * the user acts on. Translating once, on arrival, keeps every component
 * that renders this simple.
 */
export type Entry =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; text: string; streaming: boolean }
  | { kind: "tool"; id: string; name: string | null; ok: boolean; output: string }
  | {
      kind: "confirmation";
      id: string;
      confirmationId: string;
      toolName: string;
      description: string;
      // "pending" until answered, then whichever the user chose. The card
      // stays on screen either way, so the transcript still shows that a
      // destructive action was proposed and what was decided.
      status: "pending" | "approved" | "declined" | "expired";
    }
  | { kind: "error"; id: string; text: string }
  | { kind: "reminder"; id: string; text: string }
  | { kind: "task"; id: string; name: string; text: string }
  | { kind: "file"; id: string; change: string; name: string };

export type Status = "connecting" | "open" | "closed";

/** Unique enough for React keys, and readable in the devtools. */
let counter = 0;
export function nextId(prefix: string): string {
  counter += 1;
  return `${prefix}-${counter}`;
}
