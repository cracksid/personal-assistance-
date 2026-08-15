/**
 * The preload script.
 *
 * This runs in the renderer's process, before the page loads, but with
 * access to Node. It is the ONLY bridge between the sandboxed page and the
 * desktop, and everything it exposes is a hole in that sandbox -- so it
 * exposes almost nothing.
 *
 * contextBridge is what makes the bridge safe. Assigning to `window.x`
 * directly would put the object in the page's own JavaScript world, where
 * page code could reach into it, walk its prototype chain and get at Node
 * through it. contextBridge copies values across an isolation boundary, so
 * the page receives plain data and functions with nothing behind them.
 *
 * WHAT IS DELIBERATELY NOT HERE.
 *
 * No filesystem access, no shell, no ability to spawn anything, no IPC
 * channel that takes an arbitrary command. JARVIS already has filesystem
 * tools -- they go through the confirmation gate and the audit log in the
 * backend, where CLAUDE.md says that decision belongs. A second path from
 * the UI straight to the disk would be exactly the "two paths to
 * execution, only one of them gated" failure the gate exists to prevent.
 */

const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("jarvisDesktop", {
  // A flag so the UI can tell it is in the desktop shell rather than a
  // browser tab -- for wording, not for capability.
  isDesktop: true,
  platform: process.platform,
});
