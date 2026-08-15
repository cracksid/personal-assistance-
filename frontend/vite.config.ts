/**
 * Vite configuration.
 *
 * THE PROXY IS THE INTERESTING PART.
 *
 * In development two servers run: Vite on 5173 serving the UI, and FastAPI
 * on 8000 serving the API. To a browser those are different ORIGINS
 * (scheme + host + port), and the "same-origin policy" blocks a page on one
 * from calling the other.
 *
 * The usual fix is CORS: the backend sends headers saying "5173 may talk to
 * me". That works, and it is the wrong move here. CLAUDE.md's security
 * position is "bind to 127.0.0.1" -- keep the surface closed -- and CORS is
 * the backend explicitly opening itself to another origin.
 *
 * A proxy avoids the question. The browser sends everything to 5173; Vite
 * forwards anything matching the paths below to 8000 and returns the reply
 * as its own. One origin as far as the browser is concerned, and the
 * backend never learns the frontend exists.
 *
 * It also means the app's code contains no host or port at all -- it fetches
 * "/tools" and opens a socket at "/ws/chat". In production those are served
 * by the same server anyway, so the same code works unchanged.
 */

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// This file runs in Node, not in the browser, so `process` exists at build
// time -- but TypeScript only knows about browser globals here. Declaring
// the one property used is enough, and avoids adding @types/node (a large
// dependency) for a single line.
declare const process: { env: Record<string, string | undefined> };

// Overridable so a second backend can be run alongside the usual one --
// on a different port for a test, or on another machine. Defaults to where
// `uvicorn app.main:app` puts it.
const backend = process.env.JARVIS_BACKEND ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],

  // Phase 14 (Electron) loads the built files from disk rather than from a
  // server, where an absolute path like /assets/index.js resolves against
  // the filesystem root and 404s. Relative paths work in both.
  base: "./",

  server: {
    port: 5173,
    proxy: {
      // ws: true upgrades the connection rather than treating it as HTTP.
      // Without it the WebSocket handshake is proxied as a normal request
      // and fails with a 400 that is genuinely confusing to diagnose.
      "/ws": { target: backend, ws: true },
      "/tools": { target: backend },
      "/plugins": { target: backend },
      "/settings": { target: backend },
      "/memory": { target: backend },
      "/voice": { target: backend },
      "/vision": { target: backend },
      "/health": { target: backend },
    },
  },
  build: {
    outDir: "dist",
  },
});
