# Plugins

Drop a `.py` file in this folder and restart JARVIS. It becomes a tool the
model can call, with the same confirmation gate and audit log as anything
built in.

**Restart is required.** Plugins load once, at startup. And `--reload` will
not help by itself: uvicorn watches the folder it was launched from
(`backend/`), not this one. Point it here as well:

```powershell
python -m uvicorn app.main:app --reload --reload-dir . --reload-dir ..\plugins
```

See [docs/plugins.md](../docs/plugins.md) for the guide, and
`example_units.py` here for a working plugin you can copy.

Check what loaded:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/plugins"
```

**A plugin is ordinary Python running with JARVIS's permissions.** There is
no sandbox around it. Install plugins you trust, the same way you would treat
any Python package.
