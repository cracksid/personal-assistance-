/**
 * The settings and memory panel.
 *
 * A slide-over rather than a route, because this is a thing you glance at
 * and dismiss, not a place you navigate to. It also keeps the app a single
 * screen, which means no router and no second dependency.
 *
 * DATA IS FETCHED WHEN IT OPENS, NOT HELD FOREVER.
 *
 * Both tabs load on open and reload after a change. Neither is large, and
 * a settings page showing stale values is worse than one that pauses for
 * 40ms -- particularly the memory list, where the whole point is being able
 * to trust that what you are looking at is what JARVIS actually knows.
 */

import { useCallback, useEffect, useState } from "react";

interface SettingInfo {
  key: string;
  label: string;
  help: string;
  kind: string;
  choices: string[];
  restart: boolean;
  value: string;
}

interface FactInfo {
  id: number;
  content: string;
  kind: string;
  created_at: string;
  source_conversation_id: number | null;
}

interface Props {
  open: boolean;
  onClose: () => void;
}

export function Panel({ open, onClose }: Props) {
  const [tab, setTab] = useState<"settings" | "memory">("settings");

  return (
    <>
      {/* Clicking away closes it -- the behaviour every panel like this has,
          and cheaper than a close button people still miss. */}
      <div className={`scrim ${open ? "open" : ""}`} onClick={onClose} />

      <aside className={`panel ${open ? "open" : ""}`}>
        <header className="panel-head">
          <div className="tabs">
            <button
              className={tab === "settings" ? "tab active" : "tab"}
              onClick={() => setTab("settings")}
            >
              SETTINGS
            </button>
            <button
              className={tab === "memory" ? "tab active" : "tab"}
              onClick={() => setTab("memory")}
            >
              MEMORY
            </button>
          </div>
          <button className="close" onClick={onClose} title="Close">
            ✕
          </button>
        </header>

        {/* `open &&` means the contents unmount when closed, so each open
            fetches fresh rather than showing last time's values. */}
        <div className="panel-body">
          {open && tab === "settings" && <SettingsTab />}
          {open && tab === "memory" && <MemoryTab />}
        </div>
      </aside>
    </>
  );
}

function SettingsTab() {
  const [items, setItems] = useState<SettingInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/settings");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setItems((await response.json()).settings);
      setError(null);
    } catch (exc) {
      setError(String(exc));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function change(key: string, value: string) {
    setSaving(key);
    try {
      const response = await fetch("/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ changes: { [key]: value } }),
      });
      const body = await response.json();
      if (!response.ok) {
        // The backend's message says exactly what was wrong with the
        // value, which is more useful than "invalid".
        setError(body.detail ?? `HTTP ${response.status}`);
        return;
      }
      // Trust the server's full list rather than patching the one field.
      // Changing one setting can affect what another means, and this
      // cannot drift.
      setItems(body.settings);
      setError(null);
    } catch (exc) {
      setError(String(exc));
    } finally {
      setSaving(null);
    }
  }

  if (error && !items) return <p className="panel-error">Could not load settings: {error}</p>;
  if (!items) return <p className="panel-muted">Loading…</p>;

  return (
    <>
      {error && <p className="panel-error">{error}</p>}

      <p className="panel-note">
        Secrets are not here and cannot be. The API key lives in <code>.env</code>,
        is never sent to this page, and cannot be changed through it.
      </p>

      {items.map((item) => (
        <div className="setting" key={item.key}>
          <label htmlFor={item.key}>
            {item.label}
            {item.restart && <span className="badge">restart</span>}
          </label>

          {item.kind === "choice" ? (
            <select
              id={item.key}
              value={item.value}
              disabled={saving === item.key}
              onChange={(event) => change(item.key, event.target.value)}
            >
              {item.choices.map((choice) => (
                <option key={choice} value={choice}>
                  {choice}
                </option>
              ))}
            </select>
          ) : item.kind === "toggle" ? (
            <button
              id={item.key}
              className={`toggle ${item.value === "True" ? "on" : ""}`}
              disabled={saving === item.key}
              onClick={() => change(item.key, item.value === "True" ? "false" : "true")}
            >
              {item.value === "True" ? "ON" : "OFF"}
            </button>
          ) : (
            // Committed on blur or Enter rather than on every keystroke,
            // which would send a request per character and store garbage
            // like "claude-son" on the way to a full model name.
            <input
              id={item.key}
              type={item.kind === "number" ? "number" : "text"}
              defaultValue={item.value}
              disabled={saving === item.key}
              onBlur={(event) => {
                if (event.target.value !== item.value) change(item.key, event.target.value);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
              }}
            />
          )}

          <p className="setting-help">{item.help}</p>
        </div>
      ))}
    </>
  );
}

function MemoryTab() {
  const [facts, setFacts] = useState<FactInfo[] | null>(null);
  const [counts, setCounts] = useState({ total: 0, indexed: 0 });
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/memory/facts");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const body = await response.json();
      setFacts(body.facts);
      setCounts({ total: body.total, indexed: body.indexed });
      setError(null);
    } catch (exc) {
      setError(String(exc));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function forget(id: number) {
    // Removed from the list immediately rather than after a reload. The
    // request is a single DELETE that either works or errors, and waiting
    // a round trip to see a row vanish feels broken.
    setFacts((prev) => (prev ? prev.filter((f) => f.id !== id) : prev));
    setCounts((prev) => ({ total: prev.total - 1, indexed: prev.indexed - 1 }));
    try {
      const response = await fetch(`/memory/facts/${id}`, { method: "DELETE" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } catch (exc) {
      setError(`Could not forget that: ${exc}`);
      load(); // put it back if the delete did not happen
    }
  }

  if (error && !facts) return <p className="panel-error">Could not load memory: {error}</p>;
  if (!facts) return <p className="panel-muted">Loading…</p>;

  return (
    <>
      {error && <p className="panel-error">{error}</p>}

      <p className="panel-note">
        Facts are extracted from conversation automatically, and models get
        things wrong. Deleting one removes it from both the database and the
        search index, permanently.
      </p>

      <div className="memory-counts">
        <span>{counts.total} REMEMBERED</span>
        {/* A mismatch means the derived index has drifted from the source of
            truth, which is worth seeing rather than hiding. */}
        {counts.indexed !== counts.total && (
          <span className="warn">{counts.indexed} INDEXED</span>
        )}
      </div>

      {facts.length === 0 && <p className="panel-muted">Nothing remembered yet.</p>}

      {facts.map((fact) => (
        <div className="fact" key={fact.id}>
          <div className="fact-head">
            <span className="fact-kind">{fact.kind}</span>
            <span className="fact-date">{fact.created_at.slice(0, 10)}</span>
            <button className="forget" onClick={() => forget(fact.id)} title="Forget this">
              ✕
            </button>
          </div>
          <p className="fact-body">{fact.content}</p>
        </div>
      ))}
    </>
  );
}
