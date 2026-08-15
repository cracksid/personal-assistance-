/**
 * The conversation, rendered.
 *
 * WHAT JSX IS.
 *
 * The HTML-looking syntax below is not HTML and not a template language. It
 * compiles to function calls -- `<p className="x">hi</p>` becomes
 * roughly `React.createElement("p", { className: "x" }, "hi")`. That is why
 * it is `className` and not `class`: `class` is a reserved word in
 * JavaScript, and these are function arguments.
 *
 * Anything in braces is an expression, so `{entries.map(...)}` is ordinary
 * JavaScript producing an array of elements.
 *
 * WHY EVERY ITEM IN A LIST NEEDS A key.
 *
 * React re-renders by comparing the previous elements with the new ones. For
 * a list it needs to know which item is which -- without a stable key it
 * matches by position, so inserting at the top makes it think every item
 * changed. Here the ids come from protocol.ts and never change.
 */

import { Markdown } from "../lib/markdown";
import { Entry } from "../lib/protocol";

interface Props {
  entries: Entry[];
  onAnswer: (confirmationId: string, approve: boolean) => void;
}

export function Transcript({ entries, onAnswer }: Props) {
  if (entries.length === 0) {
    return (
      <div className="empty">
        <div className="boot">
          {/* Each line fades in after the one before it, via a CSS
              animation-delay stepped by index. A staggered boot sequence is
              the single cheapest way to make an interface feel like it is
              starting up rather than simply being there. */}
          {[
            "SYSTEM ONLINE",
            "PROVIDERS · MEMORY · TOOLS · PLUGINS",
            "AWAITING INSTRUCTION",
          ].map((line, i) => (
            <div key={line} className="boot-line" style={{ animationDelay: `${i * 0.5}s` }}>
              {line}
            </div>
          ))}
        </div>
        <ul className="suggestions">
          <li>What files are in my Downloads folder?</li>
          <li>Remind me in 10 minutes to stretch.</li>
          <li>Convert 5 km to miles.</li>
        </ul>
      </div>
    );
  }

  return (
    <div className="transcript">
      {entries.map((entry) => (
        <EntryView key={entry.id} entry={entry} onAnswer={onAnswer} />
      ))}
    </div>
  );
}

function EntryView({ entry, onAnswer }: { entry: Entry; onAnswer: Props["onAnswer"] }) {
  switch (entry.kind) {
    case "user":
      return (
        <div className="row user">
          <div className="bubble user">{entry.text}</div>
        </div>
      );

    case "assistant":
      return (
        <div className="row assistant">
          <div className="bubble assistant">
            {/* Markdown, not plain text: models write **bold**, lists and
                fenced code whether asked to or not, and showing the raw
                syntax makes every reply look broken. */}
            <Markdown text={entry.text} />
            {/* A caret while the reply is still arriving, so a pause reads
                as "still thinking" rather than "finished, and that was
                it". */}
            {entry.streaming && <span className="caret" />}
          </div>
        </div>
      );

    case "tool":
      return (
        <details className={`tool ${entry.ok ? "" : "failed"}`}>
          <summary>
            <span className="tool-icon">{entry.ok ? "🔧" : "⚠️"}</span>
            <code>{entry.name ?? "tool"}</code>
            {!entry.ok && <span className="tool-failed-label">failed</span>}
          </summary>
          {/* Collapsed by default. A directory listing or a fetched page is
              hundreds of lines, and burying the reply under it is worse
              than hiding it behind one click. */}
          <pre>{entry.output || "(no output)"}</pre>
        </details>
      );

    case "confirmation":
      return <ConfirmationCard entry={entry} onAnswer={onAnswer} />;

    case "error":
      return <div className="notice error">{entry.text}</div>;

    case "reminder":
      return (
        <div className="notice reminder">
          <strong>Reminder</strong>
          {entry.text}
        </div>
      );

    case "task":
      return (
        <div className="notice task">
          <strong>{entry.name}</strong>
          {entry.text}
        </div>
      );

    case "file":
      return (
        <div className="notice file">
          <strong>File {entry.change}</strong>
          <code>{entry.name}</code>
        </div>
      );
  }
}

function ConfirmationCard({
  entry,
  onAnswer,
}: {
  entry: Extract<Entry, { kind: "confirmation" }>;
  onAnswer: Props["onAnswer"];
}) {
  const pending = entry.status === "pending";

  return (
    <div className={`confirmation ${entry.status}`}>
      <div className="confirmation-head">
        <span className="pulse-dot" />
        <span>AUTHORISATION REQUIRED</span>
        <code>{entry.toolName}</code>
      </div>

      {/* The sentence the tool's describe_action() produced. This is the
          whole basis for the decision, so it gets the most weight on the
          card. */}
      <p className="confirmation-body">{entry.description}</p>

      {pending ? (
        <div className="confirmation-actions">
          <button className="danger" onClick={() => onAnswer(entry.confirmationId, true)}>
            Approve
          </button>
          <button onClick={() => onAnswer(entry.confirmationId, false)}>Decline</button>
        </div>
      ) : (
        // The card stays after answering rather than disappearing, so the
        // transcript still shows a destructive action was proposed and what
        // was decided about it.
        <p className="confirmation-outcome">
          {entry.status === "approved" ? "You approved this." : "You declined this."}
        </p>
      )}
    </div>
  );
}
