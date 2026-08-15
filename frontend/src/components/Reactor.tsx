/**
 * The arc reactor.
 *
 * Drawn as SVG rather than as an image, so it scales to any size, inherits
 * colour from CSS, and can be animated a ring at a time. Every ring below
 * is a plain circle -- the effect comes entirely from stroke-dasharray
 * (dashes rather than a solid line) and rotating each one at a different
 * speed in the opposite direction.
 *
 * IT IS ALSO THE CONNECTION INDICATOR, NOT DECORATION.
 *
 * The colour is driven by the socket's state: cyan alive, amber connecting,
 * red disconnected. A decorative reactor plus a separate status dot would
 * be two things saying one thing. The most eye-catching element on screen
 * should carry the most important status.
 */

import { Status } from "../lib/protocol";

interface Props {
  status: Status;
  /** True while a reply is being generated -- the reactor spins up. */
  active: boolean;
}

export function Reactor({ status, active }: Props) {
  return (
    <div
      className={`reactor ${status} ${active ? "active" : ""}`}
      title={
        status === "open"
          ? "Connected"
          : status === "connecting"
            ? "Connecting…"
            : "Disconnected"
      }
    >
      <svg viewBox="0 0 100 100" aria-hidden="true">
        {/* Outer ring: long dashes, slow, clockwise. */}
        <circle
          className="ring outer"
          cx="50"
          cy="50"
          r="46"
          strokeDasharray="18 6"
        />

        {/* Second ring: fine ticks, faster, counter-clockwise. Opposing
            directions are what stop it reading as one spinning object. */}
        <circle
          className="ring mid"
          cx="50"
          cy="50"
          r="38"
          strokeDasharray="2 4"
        />

        {/* Third ring: three long arcs, slow. */}
        <circle
          className="ring inner"
          cx="50"
          cy="50"
          r="30"
          strokeDasharray="47 47"
        />

        {/* The core: a solid disc that pulses. */}
        <circle className="core" cx="50" cy="50" r="21" />

        {/* The triangle, which is the bit everyone actually recognises. */}
        <path className="triangle" d="M50 33 L67 62 L33 62 Z" />

        {/* Coil marks around the core, evenly spaced by rotating one line.
            Generated rather than typed out, so changing the count is one
            number instead of eight lines of near-identical markup. */}
        {Array.from({ length: 8 }, (_, i) => (
          <line
            key={i}
            className="coil"
            x1="50"
            y1="12"
            x2="50"
            y2="17"
            transform={`rotate(${i * 45} 50 50)`}
          />
        ))}
      </svg>
    </div>
  );
}
