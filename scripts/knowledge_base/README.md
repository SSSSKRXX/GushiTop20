# Knowledge Database Collector

The breakout screener uses a local SQLite database, but the database itself is
too large for GitHub. This folder keeps the reproducible collector script in the
repository while ignoring the generated `.db` files.

## Tables Used By The App

- `stock_daily`: daily A-share bars used to check all-time-high close breakouts.
- `stock_info`: market-cap snapshots used by the breakout market-cap filters.

## Default Paths

The web app reads the database in this order:

1. `KNOWLEDGE_DB_FILE` environment variable.
2. `data/knowledge.db` inside this repository.
3. Existing legacy path `D:\Agent_Prooogram\stock-knowledge-base\knowledge.db`
   when it exists on this machine.

## Commands

Initialize an empty database and fetch current market-cap data:

```powershell
.\.venv\Scripts\python.exe scripts\knowledge_base\collect_knowledge_db.py --init --market-cap
```

Build historical daily data. This can take a long time because it fetches all
A-share history:

```powershell
.\.venv\Scripts\python.exe scripts\knowledge_base\collect_knowledge_db.py --backfill --resume --start 19900101 --delay 0.4
```

Write today's rows from StockMonitor's local snapshot:

```powershell
.\.venv\Scripts\python.exe scripts\knowledge_base\collect_knowledge_db.py --from-spot D:\StockMonitor\data\spot.json
```

For daily maintenance, run `--market-cap` and `--from-spot` after the market
close, or place the command in Windows Task Scheduler.

```powershell
.\.venv\Scripts\python.exe scripts\knowledge_base\collect_knowledge_db.py --market-cap --from-spot D:\StockMonitor\data\spot.json
```

Generated files such as `data/knowledge.db`, `*.db-wal`, and `*.db-shm` are
ignored by Git.
