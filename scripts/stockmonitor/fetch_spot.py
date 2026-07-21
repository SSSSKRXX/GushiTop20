#!/usr/bin/env python
"""Collect an A-share spot snapshot compatible with the dashboard."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import akshare as ak
import requests


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "data" / "stockmonitor" / "spot.json"
BATCH_SIZE = 200
REQUEST_DELAY_SECONDS = 0.1
SINA_URL = "http://hq.sinajs.cn/list={}"


def code_to_sina(code: Any) -> str:
    normalized = str(code).strip().zfill(6)
    if normalized.startswith(("60", "68", "9")):
        return f"sh{normalized}"
    if normalized.startswith(("8", "4")):
        return f"bj{normalized}"
    return f"sz{normalized}"


def parse_sina_line(line: str) -> dict[str, Any] | None:
    match = re.match(r'var hq_str_(\w+)="(.*)"', line.strip())
    if not match:
        return None

    sina_code = match.group(1)
    fields = match.group(2).split(",")
    if len(fields) < 10 or not fields[0]:
        return None

    try:
        previous_close = float(fields[2]) if fields[2] else 0.0
        price = float(fields[3]) if fields[3] else 0.0
        return {
            "code_sina": sina_code,
            "name": fields[0],
            "open": float(fields[1]) if fields[1] else 0.0,
            "prev_close": previous_close,
            "price": price,
            "high": float(fields[4]) if fields[4] else 0.0,
            "low": float(fields[5]) if fields[5] else 0.0,
            "volume": float(fields[8]) if fields[8] else 0.0,
            "amount": float(fields[9]) if fields[9] else 0.0,
            "code": re.sub(r"^[a-z]+", "", sina_code),
            "change_pct": round((price - previous_close) / previous_close * 100, 2)
            if previous_close > 0
            else 0.0,
        }
    except (ValueError, IndexError):
        return None


def load_stock_codes(cache_file: Path) -> list[str]:
    today = datetime.now().strftime("%Y%m%d")
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("date") == today and cached.get("codes"):
                print(f"[INFO] Using today's stock-list cache ({len(cached['codes'])} codes)")
                return [str(code).zfill(6) for code in cached["codes"]]
        except (OSError, ValueError, TypeError):
            pass

    try:
        print("[INFO] Fetching the A-share code list with AkShare")
        frame = ak.stock_info_a_code_name()
        codes = [str(code).zfill(6) for code in frame["code"].tolist()]
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(cache_file, {"date": today, "codes": codes})
        return codes
    except Exception:
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            codes = [str(code).zfill(6) for code in cached.get("codes", [])]
            if codes:
                print(f"[WARN] Stock-list refresh failed; using stale cache ({len(codes)} codes)")
                return codes
        raise


def fetch_quotes(codes: list[str], batch_size: int, delay: float) -> tuple[list[dict[str, Any]], int]:
    session = requests.Session()
    session.headers.update(
        {
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }
    )
    sina_codes = [code_to_sina(code) for code in codes]
    rows: list[dict[str, Any]] = []
    failures = 0

    for offset in range(0, len(sina_codes), batch_size):
        batch = sina_codes[offset : offset + batch_size]
        try:
            response = session.get(SINA_URL.format(",".join(batch)), timeout=15)
            response.raise_for_status()
            response.encoding = "gbk"
            for line in response.text.splitlines():
                parsed = parse_sina_line(line)
                if parsed:
                    rows.append(parsed)
        except requests.RequestException as exc:
            failures += 1
            print(f"[WARN] Batch {offset // batch_size + 1} failed: {type(exc).__name__}: {exc}")

        if offset + batch_size < len(sina_codes):
            time.sleep(delay)

    return rows, failures


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch an A-share spot snapshot from Sina")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stock-list-cache", type=Path)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY_SECONDS)
    parser.add_argument("--min-rows", type=int, default=3000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    stock_list_cache = args.stock_list_cache or output.parent / "stock_list.json"
    started_at = datetime.now()
    print(f"=== StockMonitor {started_at:%Y-%m-%d %H:%M:%S} ===")

    try:
        codes = load_stock_codes(stock_list_cache)
        rows, failures = fetch_quotes(codes, max(1, args.batch_size), max(0.0, args.delay))
    except Exception as exc:
        print(f"[FATAL] {type(exc).__name__}: {exc}")
        return 1

    if len(rows) < max(1, args.min_rows):
        print(f"[FATAL] Only {len(rows)} rows were returned; keeping the previous snapshot")
        return 2

    completed_at = datetime.now()
    payload = {
        "updated_at": completed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "StockMonitor/Sina",
        "data": rows,
    }
    atomic_write_json(output, payload)
    up_count = sum(float(row.get("change_pct") or 0) > 0 for row in rows)
    down_count = sum(float(row.get("change_pct") or 0) < 0 for row in rows)
    print(
        f"[OK] Wrote {len(rows)} rows to {output} in {(completed_at - started_at).total_seconds():.1f}s "
        f"(up={up_count}, down={down_count}, failed_batches={failures})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
