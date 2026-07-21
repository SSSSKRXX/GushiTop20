# StockMonitor collector

This directory contains the reproducible source for the dashboard's local
intraday snapshot. It replaces the machine-specific legacy directory
`D:\StockMonitor`.

- `fetch_spot.py` obtains the A-share code list with AkShare and quotes from
  Sina Finance. It does not require an API key.
- `run_fetch.ps1` runs the collector with the repository virtual environment.
- `install_task.ps1` installs `StockMonitor_Fetch` for A-share trading sessions,
  every 15 minutes.
- The default output is `data\stockmonitor\spot.json`.
- Snapshot writes are atomic, so the web process never reads a partially
  written JSON file.

Run from the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stockmonitor\install_task.ps1
```

See `docs/MIGRATION_AND_STOCKMONITOR.md` for the complete Windows migration
procedure.
