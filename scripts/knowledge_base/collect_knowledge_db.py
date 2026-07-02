#!/usr/bin/env python
r"""
Build and update the local SQLite knowledge database used by the breakout screen.

The web app only needs two tables:
- stock_daily: daily OHLCV/amount history
- stock_info: market-cap snapshot

Typical usage:
  python scripts/knowledge_base/collect_knowledge_db.py --init --market-cap
  python scripts/knowledge_base/collect_knowledge_db.py --from-spot D:\StockMonitor\data\spot.json
  python scripts/knowledge_base/collect_knowledge_db.py --backfill --resume --start 19900101
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd
import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "knowledge.db"
DEFAULT_SPOT = Path(os.environ.get("STOCKMONITOR_SPOT_FILE", r"D:\StockMonitor\data\spot.json"))


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return None if number != number else number
    except (TypeError, ValueError):
        return None


def normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:].zfill(6) if digits else text


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_daily (
                code TEXT NOT NULL,
                name TEXT,
                date TEXT NOT NULL,
                open REAL,
                close REAL,
                high REAL,
                low REAL,
                volume REAL,
                amount REAL,
                change_pct REAL,
                turnover REAL,
                updated_at TEXT DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (code, date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_info (
                code TEXT PRIMARY KEY,
                name TEXT,
                price REAL,
                total_mv REAL,
                circ_mv REAL,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_code ON stock_daily(code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_date ON stock_daily(date)")
        conn.commit()
    print(f"DB initialized: {db_path}")


def eastmoney_market_cap_rows(max_pages: int = 70, delay: float = 0.6) -> list[tuple[Any, ...]]:
    base_url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pz": "100",
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f6",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
        "fields": "f12,f14,f2,f21,f23",
    }
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com",
        }
    )

    rows: list[tuple[Any, ...]] = []
    empty_pages = 0
    for page in range(1, max_pages + 1):
        params["pn"] = str(page)
        response = session.get(base_url, params=params, timeout=15)
        response.raise_for_status()
        diff = ((response.json().get("data") or {}).get("diff") or [])
        if not diff:
            empty_pages += 1
            if empty_pages >= 2:
                break
            continue
        empty_pages = 0
        for item in diff:
            code = normalize_code(item.get("f12"))
            name = str(item.get("f14") or "")
            price = safe_float(item.get("f2"))
            circ_mv = safe_float(item.get("f21"))
            total_mv = safe_float(item.get("f23"))
            if code and total_mv:
                rows.append((code, name, price, total_mv, circ_mv, today()))
        if page % 10 == 0:
            print(f"  market-cap page {page}, rows={len(rows)}")
        time.sleep(delay)
    return rows


def update_market_cap(db_path: Path, max_pages: int, delay: float) -> None:
    init_db(db_path)
    rows = eastmoney_market_cap_rows(max_pages=max_pages, delay=delay)
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO stock_info
            (code, name, price, total_mv, circ_mv, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    print(f"market-cap rows saved: {len(rows)}")


def load_spot_rows(spot_file: Path) -> tuple[list[dict[str, Any]], str]:
    payload = json.loads(spot_file.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("data") or []
        updated_at = str(payload.get("updated_at") or today())
    elif isinstance(payload, list):
        rows = payload
        updated_at = today()
    else:
        rows = []
        updated_at = today()
    if not isinstance(rows, list):
        raise ValueError("spot.json data must be a list")
    return rows, updated_at[:10]


def collect_daily_from_spot(db_path: Path, spot_file: Path, date: str | None = None) -> None:
    init_db(db_path)
    rows, spot_date = load_spot_rows(spot_file)
    trade_date = date or spot_date or today()
    db_rows: list[tuple[Any, ...]] = []
    info_rows: list[tuple[Any, ...]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        code = normalize_code(item.get("code") or item.get("代码"))
        name = str(item.get("name") or item.get("名称") or "")
        close = safe_float(item.get("price") or item.get("最新价"))
        amount = safe_float(item.get("amount") or item.get("成交额"))
        if not code or close is None:
            continue
        db_rows.append(
            (
                code,
                name,
                trade_date,
                safe_float(item.get("open") or item.get("今开")),
                close,
                safe_float(item.get("high") or item.get("最高")),
                safe_float(item.get("low") or item.get("最低")),
                safe_float(item.get("volume") or item.get("成交量")),
                amount,
                safe_float(item.get("change_pct") or item.get("涨跌幅")),
                safe_float(item.get("turnover") or item.get("换手率")),
            )
        )
        total_mv = safe_float(item.get("total_mv") or item.get("总市值"))
        circ_mv = safe_float(item.get("circ_mv") or item.get("流通市值"))
        if total_mv or circ_mv:
            info_rows.append((code, name, close, total_mv, circ_mv, trade_date))

    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO stock_daily
            (code, name, date, open, close, high, low, volume, amount, change_pct, turnover)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            db_rows,
        )
        if info_rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO stock_info
                (code, name, price, total_mv, circ_mv, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                info_rows,
            )
        conn.commit()
    print(f"daily spot rows saved: {len(db_rows)} for {trade_date}")
    if info_rows:
        print(f"stock_info rows saved from spot: {len(info_rows)}")


def stock_list() -> list[tuple[str, str]]:
    df = ak.stock_info_a_code_name()
    code_col = "code" if "code" in df.columns else "代码"
    name_col = "name" if "name" in df.columns else "名称"
    return [(normalize_code(row[code_col]), str(row[name_col])) for _, row in df.iterrows()]


def existing_codes_with_recent_data(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT code
            FROM stock_daily
            GROUP BY code
            HAVING COUNT(*) > 20
            """
        ).fetchall()
    return {str(row[0]) for row in rows}


def backfill_history(
    db_path: Path,
    start: str,
    end: str,
    adjust: str,
    resume: bool,
    limit: int,
    delay: float,
) -> None:
    init_db(db_path)
    stocks = stock_list()
    if limit > 0:
        stocks = stocks[:limit]
    if resume:
        existing = existing_codes_with_recent_data(db_path)
        stocks = [(code, name) for code, name in stocks if code not in existing]
        print(f"resume mode: skipped {len(existing)} existing codes")

    total_rows = 0
    failed = 0
    for index, (code, name) in enumerate(stocks, start=1):
        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start,
                end_date=end,
                adjust=adjust,
            )
            if df is None or df.empty:
                print(f"[{index}/{len(stocks)}] {code} {name}: empty")
                time.sleep(delay)
                continue
            rows = []
            for _, row in df.iterrows():
                date_value = str(row.get("日期") or "")
                if not date_value:
                    continue
                rows.append(
                    (
                        code,
                        name,
                        date_value[:10],
                        safe_float(row.get("开盘")),
                        safe_float(row.get("收盘")),
                        safe_float(row.get("最高")),
                        safe_float(row.get("最低")),
                        safe_float(row.get("成交量")),
                        safe_float(row.get("成交额")),
                        safe_float(row.get("涨跌幅")),
                        safe_float(row.get("换手率")),
                    )
                )
            with connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO stock_daily
                    (code, name, date, open, close, high, low, volume, amount, change_pct, turnover)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()
            total_rows += len(rows)
            if index % 50 == 0 or index == len(stocks):
                print(f"[{index}/{len(stocks)}] total daily rows={total_rows}")
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(stocks)}] {code} {name}: {type(exc).__name__}: {exc}")
        time.sleep(delay)
    print(f"backfill finished: rows={total_rows}, failed={failed}, db={db_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build/update knowledge.db for the A-share dashboard")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    parser.add_argument("--init", action="store_true", help="Create tables/indexes")
    parser.add_argument("--market-cap", action="store_true", help="Fetch Eastmoney market-cap snapshot into stock_info")
    parser.add_argument("--market-cap-pages", type=int, default=70)
    parser.add_argument("--from-spot", nargs="?", const=str(DEFAULT_SPOT), help="Write today's stock_daily rows from StockMonitor spot.json")
    parser.add_argument("--date", help="Override trade date for --from-spot, YYYY-MM-DD")
    parser.add_argument("--backfill", action="store_true", help="Fetch historical daily bars for all A-shares")
    parser.add_argument("--resume", action="store_true", help="Skip codes that already have daily data")
    parser.add_argument("--start", default="19900101", help="Backfill start date, YYYYMMDD")
    parser.add_argument("--end", default=datetime.now().strftime("%Y%m%d"), help="Backfill end date, YYYYMMDD")
    parser.add_argument("--adjust", default="", help="AkShare adjust argument; empty string means unadjusted")
    parser.add_argument("--limit", type=int, default=0, help="Limit stock count for testing")
    parser.add_argument("--delay", type=float, default=0.4, help="Delay between external requests")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db)
    did_work = False
    if args.init:
        init_db(db_path)
        did_work = True
    if args.market_cap:
        update_market_cap(db_path, max_pages=args.market_cap_pages, delay=args.delay)
        did_work = True
    if args.from_spot:
        collect_daily_from_spot(db_path, Path(args.from_spot), date=args.date)
        did_work = True
    if args.backfill:
        backfill_history(
            db_path,
            start=args.start,
            end=args.end,
            adjust=args.adjust,
            resume=args.resume,
            limit=args.limit,
            delay=args.delay,
        )
        did_work = True
    if not did_work:
        init_db(db_path)
        print("No action selected. Use --market-cap, --from-spot, or --backfill.")


if __name__ == "__main__":
    main()
