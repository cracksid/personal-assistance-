# Plugins

Drop a `.py` file in this folder and restart JARVIS. It becomes a tool the
model can call, with the same confirmation gate and audit log as anything
built in.

See [docs/plugins.md](../docs/plugins.md) for the guide, and
`example_units.py` here for a working plugin you can copy.

Check what loaded:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/plugins"
```

**A plugin is ordinary Python running with JARVIS's permissions.** There is
no sandbox around it. Install plugins you trust, the same way you would treat
any Python package.
