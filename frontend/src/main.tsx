/**
 * Where the app starts.
 *
 * createRoot takes the empty <div id="root"> from index.html and hands it to
 * React, which renders <App /> into it. Everything on screen from then on is
 * React's doing.
 *
 * StrictMode is development-only and deliberately hostile: it mounts every
 * component, unmounts it, and mounts it again, to surface effects that do
 * not clean up after themselves. A socket opened without a matching close
 * shows up here as two connections rather than as a mysterious duplicate
 * reminder six weeks later. It does nothing in a production build.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("index.html is missing <div id='root'>");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
