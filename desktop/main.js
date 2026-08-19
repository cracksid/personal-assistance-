/**
 * The Electron shell.
 *
 * Electron is Chromium plus Node in one process tree. This file is the
 * "main process": it runs in Node, owns the windows and the tray, and can
 * touch the filesystem and spawn processes. The page inside the window is
 * the "renderer" and is an ordinary sandboxed web page.
 *
 * What it does, in order:
 *
 *   1. Refuse to start twice.
 *   2. Start the Python backend as a child process.
 *   3. Wait for /health to answer.
 *   4. Open a window pointed at it, and put an icon in the tray.
 *   5. On quit, stop the backend properly.
 *
 * WHY IT LOADS http://127.0.0.1:8000 AND NOT A FILE.
 *
 * The frontend contains no host or port anywhere -- it fetches "/settings"
 * and opens "/ws/chat". From a file:// URL those resolve against the
 * filesystem and every one fails. FastAPI serves the built files (see
 * _serve_built_frontend in backend/app/main.py), so the UI and the API
 * share an origin, and the same code works here and in a browser.
 *
 * THE SECURITY SETTINGS ARE NOT OPTIONAL.
 *
 * nodeIntegration:false and contextIsolation:true mean the page cannot
 * reach Node -- no require, no fs, no child_process. This matters more here
 * than in most Electron apps: since Phase 10 JARVIS fetches arbitrary web
 * pages and puts what it finds on screen. With Node exposed, a hostile page
 * quoted into a reply would be running in a process that can read the
 * filesystem. The renderer is treated as untrusted, because in effect it is.
 */

const { app, BrowserWindow, Menu, Tray, shell, dialog, session } = require("electron");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");
const http = require("node:http");

const DEV = process.argv.includes("--dev");
const PROJECT_ROOT = path.resolve(__dirname, "..");
const BACKEND_DIR = path.join(PROJECT_ROOT, "backend");

const PORT = 8000;
const BACKEND_URL = `http://127.0.0.1:${PORT}`;
// In dev the UI comes from Vite, which proxies the API to the backend --
// the same setup as working in a browser, with hot reload intact.
const UI_URL = DEV ? "http://localhost:5173" : BACKEND_URL;

let backend = null;
let window = null;
let tray = null;
let quitting = false;

/** The Python interpreter inside the project's virtual environment. */
function pythonPath() {
  const candidates = [
    path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe"), // Windows
    path.join(PROJECT_ROOT, ".venv", "bin", "python"), // everywhere else
  ];
  return candidates.find((p) => fs.existsSync(p)) ?? null;
}

function startBackend() {
  const python = pythonPath();
  if (!python) {
    dialog.showErrorBox(
      "JARVIS cannot start",
      `No virtual environment found at ${path.join(PROJECT_ROOT, ".venv")}.\n\n` +
        "Create one and install the requirements first.",
    );
    app.quit();
    return;
  }

  console.log("Starting backend:", python);
  backend = spawn(python, ["-m", "uvicorn", "app.main:app", "--port", String(PORT)], {
    cwd: BACKEND_DIR,
    // Inherited so the backend's logs appear in this terminal. A packaged
    // build would send them to a file instead.
    stdio: "inherit",
    windowsHide: true,
  });

  backend.on("exit", (code) => {
    console.log("Backend exited with", code);
    backend = null;
    // A backend that dies while the app is running leaves a window that
    // cannot do anything. Say so rather than presenting a dead UI.
    if (!quitting && code !== 0) {
      dialog.showErrorBox(
        "JARVIS backend stopped",
        `The Python backend exited with code ${code}. Check the terminal for the reason.`,
      );
    }
  });
}

/**
 * Wait for the backend to answer /health.
 *
 * Polling rather than a fixed delay: startup time varies a lot -- loading
 * the embedding model and the plugin folder is not instant, and a machine
 * under load is slower still. A sleep long enough to be safe would make
 * every launch feel broken.
 */
function waitForBackend(timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;

  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(`${BACKEND_URL}/health`, (response) => {
        response.resume();
        if (response.statusCode === 200) return resolve();
        retry();
      });
      request.on("error", retry);
      request.setTimeout(2000, () => request.destroy());
    };

    const retry = () => {
      if (Date.now() > deadline) {
        reject(new Error("The backend did not start in time."));
        return;
      }
      setTimeout(attempt, 300);
    };

    attempt();
  });
}

function createWindow() {
  window = new BrowserWindow({
    width: 1100,
    height: 800,
    minWidth: 480,
    minHeight: 400,
    backgroundColor: "#050a12", // matches the UI, so no white flash on open
    title: "JARVIS",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      // See the security note at the top. The page gets no Node at all.
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  });

  // MICROPHONE PERMISSION.
  //
  // Electron denies media access by default, so without this the mic button
  // fails silently in the desktop app while working fine in a browser.
  //
  // Granted only for "media", and only to our own origin. Everything else --
  // geolocation, notifications, MIDI, and any request from a page that
  // somehow is not ours -- is refused. Since Phase 10 this window renders
  // text fetched from arbitrary sites, so a blanket `callback(true)` would
  // hand those pages the microphone.
  session.defaultSession.setPermissionRequestHandler(
    (contents, permission, callback) => {
      const fromUs = (contents.getURL() || "").startsWith(UI_URL);
      callback(fromUs && permission === "media");
    },
  );

  window.loadURL(UI_URL);

  // Anything that would open a new window -- a link in a fetched page the
  // model quoted -- goes to the real browser instead. A second Electron
  // window pointed at an arbitrary site is a browser nobody audited.
  window.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  // Same for in-page navigation away from our own origin.
  window.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith(UI_URL)) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  // Closing hides to the tray instead of quitting, so reminders and
  // scheduled tasks keep working -- which is the entire point of them.
  // Quit deliberately from the tray menu.
  window.on("close", (event) => {
    if (!quitting) {
      event.preventDefault();
      window.hide();
    }
  });
}

function createTray() {
  // nativeImage from a data URI would need an icon file; using the window
  // icon keeps this dependency-free. Electron falls back to a default.
  tray = new Tray(path.join(__dirname, "icon.png"));
  tray.setToolTip("JARVIS");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Show JARVIS", click: () => window?.show() },
      { type: "separator" },
      {
        label: "Quit",
        click: () => {
          quitting = true;
          app.quit();
        },
      },
    ]),
  );
  tray.on("double-click", () => window?.show());
}

// A second copy would try to bind port 8000, fail, and leave the user with
// a window that cannot talk to anything. Focus the existing one instead.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (window) {
      window.show();
      window.focus();
    }
  });

  app.whenReady().then(async () => {
    if (!DEV) startBackend();

    try {
      await waitForBackend();
    } catch (error) {
      dialog.showErrorBox("JARVIS cannot start", String(error));
      app.quit();
      return;
    }

    createWindow();
    try {
      createTray();
    } catch (error) {
      // A missing icon file must not stop the app opening.
      console.warn("No tray icon:", error.message);
    }
  });

  app.on("window-all-closed", () => {
    // Deliberately does nothing on Windows and Linux: the app lives in the
    // tray so the scheduler keeps running with no window open.
  });

  app.on("before-quit", () => {
    quitting = true;
  });

  app.on("will-quit", () => {
    if (!backend) return;
    console.log("Stopping backend");
    // On Windows, killing the parent leaves uvicorn's children running and
    // holding port 8000 -- the exact orphaned-process problem that made a
    // stale server serve a browser for hours during Phase 13. taskkill /T
    // takes the whole tree.
    if (process.platform === "win32") {
      spawn("taskkill", ["/pid", String(backend.pid), "/f", "/t"]);
    } else {
      backend.kill("SIGTERM");
    }
    backend = null;
  });
}
