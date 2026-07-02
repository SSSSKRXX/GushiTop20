#!/usr/bin/env python
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import akshare as ak
import requests
from flask import Flask, jsonify, request, send_from_directory

from scripts.a_share_intraday_monitor import (
    DEFAULT_SCORING_CONFIG,
    build_report_from_snapshot,
    deep_merge_dict,
    disable_requests_env_proxy,
    fetch_individual_fund_rank,
    fetch_spot,
    has_valid_turnover,
    money_yi,
    now_cn,
    normalize_code,
    score_signal,
    to_num,
    top_turnover_table,
)


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
NOTES_FILE = DATA_DIR / "notes.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
STOCKMONITOR_SPOT_FILE = Path(os.environ.get("STOCKMONITOR_SPOT_FILE", r"D:\StockMonitor\data\spot.json"))
SECTOR_CACHE_FILE = Path(os.environ.get("SECTOR_CACHE_FILE", r"D:\Agent_Prooogram\QQGG\data\sector_cache.json"))
MARKET_CAP_CACHE_FILE = Path(os.environ.get("MARKET_CAP_CACHE_FILE", r"D:\Agent_Prooogram\QQGG\data\market_cap_cache.json"))


def default_knowledge_db_file() -> Path:
    local_db = DATA_DIR / "knowledge.db"
    legacy_db = Path(r"D:\Agent_Prooogram\stock-knowledge-base\knowledge.db")
    return local_db if local_db.exists() or not legacy_db.exists() else legacy_db


KNOWLEDGE_DB_FILE = Path(os.environ.get("KNOWLEDGE_DB_FILE", str(default_knowledge_db_file())))
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
REFRESH_SECONDS = 15 * 60
AUCTION_REFRESH_SECONDS = 60
STOCKMONITOR_READ_DELAY_SECONDS = int(os.environ.get("STOCKMONITOR_READ_DELAY_SECONDS", "60"))

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")

_cache_lock = threading.Lock()
_notes_lock = threading.Lock()
_settings_lock = threading.Lock()
_breakout_lock = threading.Lock()
_market_cache: dict[str, Any] = {}
_report_cache: dict[str, dict[str, Any]] = {}
_breakout_cache: dict[str, Any] = {}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_SETTINGS = {
    "scoring": DEFAULT_SCORING_CONFIG,
    "llmScoring": {
        "enabled": False,
        "maxAdjustment": 1.0,
        "standard": (
            "请按当天盘面强弱给出0-10分。重点参考：两市成交额前20红绿比例、目标板块高成交股表现、"
            "主力资金流向、板块内部分化、评分明细证据。输出要偏盘中交易决策，避免过度乐观。"
        ),
    },
    "llm": {
        "baseUrl": DEFAULT_LLM_BASE_URL,
        "model": DEFAULT_LLM_MODEL,
        "apiKey": "",
    },
    "breakout": {
        "days": 3,
        "minAmountYi": 10,
        "minMarketCapYi": 70,
        "maxMarketCapYi": 700,
    },
}


def monitor_args(board_keyword: str, scoring_config: dict[str, Any] | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        board_keyword=board_keyword,
        top_n=20,
        high_open_ratio=0.5,
        minute_sample_size=8,
        new_high_sample_size=20,
        new_high_window=60,
        use_env_proxy=False,
        format="json",
        scoring_config=scoring_config,
    )


def next_weekday_auction_start(current: datetime) -> datetime:
    target = datetime.combine(current.date(), clock_time(9, 15), tzinfo=current.tzinfo)
    if current < target and current.weekday() < 5:
        return target

    days = 1
    while True:
        next_day = current.date() + timedelta(days=days)
        if next_day.weekday() < 5:
            return datetime.combine(next_day, clock_time(9, 15), tzinfo=current.tzinfo)
        days += 1


def seconds_until(current: datetime, hour: int, minute: int) -> int:
    target = datetime.combine(current.date(), clock_time(hour, minute), tzinfo=current.tzinfo)
    return max(1, int((target - current).total_seconds()))


def seconds_until_next_stockmonitor_read(current: datetime) -> int:
    start = datetime.combine(current.date(), clock_time(9, 30), tzinfo=current.tzinfo)
    end = datetime.combine(current.date(), clock_time(15, 0), tzinfo=current.tzinfo)
    delayed_start = start + timedelta(seconds=STOCKMONITOR_READ_DELAY_SECONDS)
    if current < delayed_start:
        return max(1, int((delayed_start - current).total_seconds()))

    elapsed = int((current - start).total_seconds())
    interval = REFRESH_SECONDS
    slot_seconds = (elapsed // interval) * interval
    target = start + timedelta(seconds=slot_seconds + STOCKMONITOR_READ_DELAY_SECONDS)
    if current >= target:
        target = start + timedelta(seconds=slot_seconds + interval + STOCKMONITOR_READ_DELAY_SECONDS)
    last_target = end + timedelta(seconds=STOCKMONITOR_READ_DELAY_SECONDS)
    if target <= last_target:
        return max(1, int((target - current).total_seconds()))
    return max(1, int((next_weekday_auction_start(current) - current).total_seconds()))


def dynamic_refresh_seconds(current: datetime | None = None) -> int:
    current = current or now_cn()
    current_time = current.time()

    if current.weekday() >= 5:
        return max(1, int((next_weekday_auction_start(current) - current).total_seconds()))

    if current_time < clock_time(9, 15):
        return seconds_until(current, 9, 15)

    if clock_time(9, 15) <= current_time < clock_time(9, 25):
        return seconds_until(current, 9, 25)

    if clock_time(9, 25) <= current_time < clock_time(9, 30):
        return AUCTION_REFRESH_SECONDS

    if clock_time(9, 30) <= current_time <= clock_time(15, 0):
        return seconds_until_next_stockmonitor_read(current)

    return max(1, int((next_weekday_auction_start(current) - current).total_seconds()))


def is_auction_fetch_window(current: datetime | None = None) -> bool:
    current = current or now_cn()
    return current.weekday() < 5 and clock_time(9, 25) <= current.time() < clock_time(9, 30)


def parse_cn_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    tzinfo = now_cn().tzinfo
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=tzinfo)
        except ValueError:
            continue
    return now_cn()


def local_stockmonitor_snapshot_without_api(current: datetime) -> dict[str, Any]:
    spot, timestamp, source = read_stockmonitor_spot()
    top20_probe = top_turnover_table(spot, pd.DataFrame(), 20)
    if not has_valid_turnover(top20_probe):
        return {}
    return {
        "loaded_at": time.time(),
        "refresh_seconds": dynamic_refresh_seconds(current),
        "timestamp": timestamp,
        "spot": spot,
        "fund": pd.DataFrame(),
        "source": f"{source}+local_only",
        "allow_external_checks": False,
        "fund_error": "本次为本地快照回退，未触发外部主力资金流抓取",
        "local_spot_only": True,
    }


def load_sector_mapping() -> dict[str, str]:
    try:
        data = json.loads(SECTOR_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    sectors = data.get("sectors") if isinstance(data, dict) else {}
    if not isinstance(sectors, dict):
        return {}
    return {normalize_code(code): str(sector) for code, sector in sectors.items()}


def read_stockmonitor_spot() -> tuple[pd.DataFrame, datetime, str]:
    if not STOCKMONITOR_SPOT_FILE.exists():
        raise FileNotFoundError(f"StockMonitor快照不存在: {STOCKMONITOR_SPOT_FILE}")
    payload = json.loads(STOCKMONITOR_SPOT_FILE.read_text(encoding="utf-8"))
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("StockMonitor快照格式错误：data不是列表")

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("StockMonitor快照为空")

    rename_map = {
        "code": "代码",
        "name": "名称",
        "open": "今开",
        "prev_close": "昨收",
        "price": "最新价",
        "high": "最高",
        "low": "最低",
        "volume": "成交量",
        "amount": "成交额",
        "change_pct": "涨跌幅",
    }
    df = df.rename(columns={key: value for key, value in rename_map.items() if key in df.columns})
    if "代码" not in df.columns or "名称" not in df.columns:
        raise ValueError("StockMonitor快照缺少代码或名称字段")

    df["代码"] = df["代码"].map(normalize_code)
    for col in ["成交额", "涨跌幅", "最新价", "今开", "昨收", "最高", "最低", "成交量"]:
        if col in df.columns:
            df[col] = to_num(df[col])
    df["行情来源"] = "StockMonitor"

    sector_mapping = load_sector_mapping()
    if sector_mapping:
        df["板块"] = df["代码"].map(sector_mapping)

    updated_at = parse_cn_datetime(payload.get("updated_at") if isinstance(payload, dict) else None)
    source = str(payload.get("source") or "StockMonitor") if isinstance(payload, dict) else "StockMonitor"
    return df, updated_at, source


def market_cache_expired(now: float) -> bool:
    cached_at = _market_cache.get("loaded_at")
    refresh_seconds = _market_cache.get("refresh_seconds", 60)
    return not cached_at or now - cached_at >= refresh_seconds


def get_market_snapshot(force: bool = False) -> tuple[dict[str, Any], bool]:
    now = time.time()
    current = now_cn()
    with _cache_lock:
        if not force and _market_cache:
            return dict(_market_cache), False

    if not force:
        try:
            snapshot = local_stockmonitor_snapshot_without_api(current)
        except Exception:
            return {}, False
        if snapshot:
            with _cache_lock:
                _market_cache.clear()
                _market_cache.update(snapshot)
            return dict(snapshot), False
        return {}, False

    fund_error = None
    if is_auction_fetch_window(current):
        spot = fetch_spot()
        top20_probe = top_turnover_table(spot, pd.DataFrame(), 20)
        fund = fetch_individual_fund_rank() if has_valid_turnover(top20_probe) else pd.DataFrame()
        timestamp = current
        source = "auction_external"
        allow_external_checks = True
    else:
        spot, timestamp, source = read_stockmonitor_spot()
        top20_probe = top_turnover_table(spot, pd.DataFrame(), 20)
        fund_error = None
        if has_valid_turnover(top20_probe):
            try:
                fund = fetch_individual_fund_rank()
                source = f"{source}+fund_flow"
            except Exception as exc:
                fund = pd.DataFrame()
                fund_error = f"{type(exc).__name__}: {exc}"
        else:
            fund = pd.DataFrame()
        allow_external_checks = False

    refresh_seconds = dynamic_refresh_seconds(current)
    snapshot = {
        "loaded_at": now,
        "refresh_seconds": refresh_seconds,
        "timestamp": timestamp,
        "spot": spot,
        "fund": fund,
        "source": source,
        "allow_external_checks": allow_external_checks,
        "fund_error": fund_error,
    }
    with _cache_lock:
        _market_cache.clear()
        _market_cache.update(snapshot)
        _report_cache.clear()
    return dict(snapshot), True


def empty_report_payload(board_keyword: str) -> dict[str, Any]:
    current = now_cn()
    report = {
        "timestamp": current.strftime("%Y-%m-%d %H:%M:%S CST"),
        "score_10": 0,
        "raw_score": 0,
        "signal": "等待缓存",
        "score_items": [],
        "market_score": {"score_10": 0, "signal": "等待缓存", "items": []},
        "board_score": {"score_10": 0, "signal": "等待缓存", "items": []},
        "board_scores": [],
        "stock_scores": [],
        "top20_records": [],
        "board_records": [],
        "meta": {
            "board_keyword": board_keyword,
            "board_match": "暂无缓存，请等待15分钟自动刷新或点击手动刷新全盘",
            "score_max": 10,
            "primary_table_title": "两市成交额前20",
            "top20_fund_covered": 0,
            "top20_fund_total": 0,
            "cache_empty": True,
        },
    }
    return {
        "ok": True,
        "refreshSeconds": dynamic_refresh_seconds(),
        "loadedAtEpoch": int(time.time()),
        "cacheMode": "empty",
        "report": report,
    }


def get_report(board_keyword: str, force: bool = False) -> dict[str, Any]:
    cache_key = board_keyword.strip() or "光纤"
    with _settings_lock:
        settings = read_settings_unlocked()
    if not force:
        with _cache_lock:
            cached_payload = _report_cache.get(cache_key)
            if cached_payload:
                payload = dict(cached_payload)
                payload["refreshSeconds"] = dynamic_refresh_seconds()
                payload["loadedAtEpoch"] = int(time.time())
                payload["cacheMode"] = "report_cache"
                return payload

    snapshot, refreshed = get_market_snapshot(force=force)
    if not snapshot:
        return empty_report_payload(cache_key)

    allow_external_checks = bool(snapshot.get("allow_external_checks"))
    report = build_report_from_snapshot(
        monitor_args(cache_key, settings.get("scoring")),
        snapshot["spot"],
        snapshot["fund"],
        timestamp=snapshot["timestamp"],
        allow_external_board=allow_external_checks,
        allow_external_checks=allow_external_checks,
    )
    report.setdefault("meta", {})
    report["meta"]["snapshot_source"] = snapshot.get("source", "unknown")
    report["meta"]["spot_updated_at"] = snapshot["timestamp"].strftime("%Y-%m-%d %H:%M:%S %Z")
    report["meta"]["external_api_allowed"] = allow_external_checks
    report["meta"]["fund_flow_mode"] = "project_fetch_once" if refreshed else ("local_spot_no_fund" if snapshot.get("local_spot_only") else "cached")
    report["meta"]["fund_flow_error"] = snapshot.get("fund_error")
    if refreshed:
        report = maybe_apply_llm_scoring(report, settings)
    cache_mode = "market_snapshot"
    if refreshed:
        cache_mode = "auction_external" if allow_external_checks else "stockmonitor_snapshot"
    payload = {
        "ok": True,
        "refreshSeconds": dynamic_refresh_seconds(),
        "loadedAtEpoch": int(time.time()),
        "cacheMode": cache_mode,
        "report": report,
    }
    with _cache_lock:
        _report_cache[cache_key] = payload
    return payload


def valid_note_date(value: str | None) -> str:
    note_date = (value or now_cn().strftime("%Y-%m-%d")).strip()
    if not DATE_RE.match(note_date):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    datetime.strptime(note_date, "%Y-%m-%d")
    return note_date


def note_entry(title: str, content: str, *, entry_id: str | None = None, created_at: str | None = None) -> dict[str, str]:
    current = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "id": entry_id or uuid.uuid4().hex,
        "title": title.strip() or current[11:16],
        "content": content,
        "createdAt": created_at or current,
        "updatedAt": current,
    }


def normalize_notes(data: Any) -> dict[str, list[dict[str, str]]]:
    if not isinstance(data, dict):
        return {}
    normalized: dict[str, list[dict[str, str]]] = {}
    for raw_date, value in data.items():
        note_date = str(raw_date)
        if isinstance(value, str):
            normalized[note_date] = [note_entry("历史点评", value, entry_id=f"legacy-{note_date}")] if value.strip() else []
        elif isinstance(value, list):
            entries: list[dict[str, str]] = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content", ""))
                entries.append(
                    note_entry(
                        str(item.get("title") or item.get("createdAt") or "点评"),
                        content,
                        entry_id=str(item.get("id") or uuid.uuid4().hex),
                        created_at=str(item.get("createdAt") or now_cn().strftime("%Y-%m-%d %H:%M:%S")),
                    )
                )
                entries[-1]["updatedAt"] = str(item.get("updatedAt") or entries[-1]["updatedAt"])
            normalized[note_date] = entries
    return normalized


def read_notes_unlocked() -> dict[str, list[dict[str, str]]]:
    if not NOTES_FILE.exists():
        return {}
    with NOTES_FILE.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    return normalize_notes(data)


def write_notes_unlocked(notes: dict[str, list[dict[str, str]]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = NOTES_FILE.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(notes, file, ensure_ascii=False, indent=2)
    temp_file.replace(NOTES_FILE)


def read_settings_unlocked() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return deep_merge_dict(DEFAULT_SETTINGS, {})
    with SETTINGS_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return deep_merge_dict(DEFAULT_SETTINGS, data if isinstance(data, dict) else {})


def write_settings_unlocked(settings: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = SETTINGS_FILE.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(settings, file, ensure_ascii=False, indent=2)
    temp_file.replace(SETTINGS_FILE)


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    result = deep_merge_dict(settings, {})
    llm = result.setdefault("llm", {})
    api_key = str(llm.pop("apiKey", "") or "")
    llm["apiKeySet"] = bool(api_key)
    llm["apiKeyMasked"] = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) >= 8 else ("已保存" if api_key else "")
    return result


def safe_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def compact_report_for_llm(report: dict[str, Any]) -> dict[str, Any]:
    top20 = report.get("top20_records") or []
    board_records = report.get("board_records") or []
    return {
        "timestamp": report.get("timestamp"),
        "signal": report.get("signal"),
        "score": report.get("score_10"),
        "board_keyword": (report.get("meta") or {}).get("board_keyword"),
        "board_match": (report.get("meta") or {}).get("board_match"),
        "board_scores": report.get("board_scores") or [],
        "market_breadth": {
            "red": sum(1 for row in top20 if safe_number(row.get("涨跌幅")) > 0),
            "green": sum(1 for row in top20 if safe_number(row.get("涨跌幅")) < 0),
        },
        "score_items": [
            {
                "name": item.get("name"),
                "points": item.get("points"),
                "max_points": item.get("max_points"),
                "evidence": item.get("evidence"),
            }
            for item in (report.get("score_items") or [])
        ],
        "top20": top20[:20],
        "board_top": board_records[:15],
        "stock_scores": (report.get("stock_scores") or [])[:35],
    }


def llm_settings() -> tuple[str, str, str]:
    with _settings_lock:
        settings = read_settings_unlocked()
    llm = settings.get("llm") if isinstance(settings.get("llm"), dict) else {}
    api_key = str(llm.get("apiKey") or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "")
    base_url = str(llm.get("baseUrl") or os.environ.get("LLM_BASE_URL") or DEFAULT_LLM_BASE_URL).rstrip("/")
    model = str(llm.get("model") or os.environ.get("LLM_MODEL") or DEFAULT_LLM_MODEL)
    if not api_key:
        raise RuntimeError("未配置 LLM API Key，请到设置页填写")
    return api_key, base_url, model


def response_json_utf8_sig(response: requests.Response) -> dict[str, Any]:
    text = response.content.decode("utf-8-sig", errors="replace").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM API 响应 JSON 解析失败：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("LLM 响应不是 JSON 对象")
    return data


def post_llm_chat(
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    json_mode: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "45"))
    response = requests.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    if json_mode and response.status_code in {400, 422} and "response_format" in response.text.lower():
        payload.pop("response_format", None)
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
    response.raise_for_status()
    return response_json_utf8_sig(response)


def friendly_llm_error(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in message.lower():
        return "LLM请求超时，已回退规则打分"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "LLM连接失败，已回退规则打分"
    if isinstance(exc, json.JSONDecodeError) or ("json" in message.lower() or "格式" in message):
        return "LLM返回格式异常，已回退规则打分"
    return "LLM暂不可用，已回退规则打分"


def friendly_llm_summary_error(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in message.lower():
        return "LLM总结请求超时，请稍后重试"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "LLM连接失败，请检查网络或接口配置"
    if isinstance(exc, json.JSONDecodeError) or ("json" in message.lower() or "格式" in message):
        return "LLM返回格式异常，请稍后重试"
    return f"LLM总结失败：{type(exc).__name__}"


def summarize_with_llm(note_date: str, content: str, report: dict[str, Any]) -> str:
    api_key, base_url, model = llm_settings()
    prompt = {
        "date": note_date,
        "manual_note": content,
        "market_report": compact_report_for_llm(report),
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是A股盘中交易复盘助手。基于用户笔记和盘面数据，输出中文盘中点评。"
                "不要编造未提供的数据，不给确定性收益承诺。"
                "必须输出纯文本，不要Markdown，不要###标题，不要**加粗，不要表格。"
                "用短段落输出，段落标签使用：盘面结论：主线强弱：风险点：操作计划：下一次观察："
            ),
        },
        {
            "role": "user",
            "content": json.dumps(prompt, ensure_ascii=False),
        },
    ]
    data = post_llm_chat(api_key, base_url, model, messages, temperature=0.2)
    return clean_llm_note(data["choices"][0]["message"]["content"])


def clean_llm_note(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.replace("**", "").replace("__", "")
    cleaned = re.sub(r"^\s*[-*]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def strip_json_fence(text: str) -> str:
    cleaned = str(text or "").replace("\ufeff", "").strip()
    fenced = re.match(r"^```(?:json|JSON)?\s*(.*?)\s*```$", cleaned, flags=re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    return cleaned


def first_balanced_json_fragment(text: str) -> str:
    cleaned = strip_json_fence(text)
    starts = [index for index in (cleaned.find("{"), cleaned.find("[")) if index >= 0]
    if not starts:
        raise ValueError("LLM 未返回 JSON 内容")
    start = min(starts)
    opener = cleaned[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return cleaned[start : index + 1]
    end = cleaned.rfind(closer)
    if end > start:
        return cleaned[start : end + 1]
    raise ValueError("LLM JSON 内容不完整")


def load_relaxed_json(text: str) -> Any:
    fragment = first_balanced_json_fragment(text)
    candidates = [
        fragment,
        re.sub(r",\s*([}\]])", r"\1", fragment),
    ]
    last_error: Exception | None = None
    for candidate in dict.fromkeys(candidates):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    for candidate in dict.fromkeys(candidates):
        try:
            return ast.literal_eval(candidate)
        except (SyntaxError, ValueError) as exc:
            last_error = exc
    raise ValueError(f"LLM 返回格式异常：{last_error}") from last_error


def extract_json_object(text: str, *, list_key: str | None = None) -> dict[str, Any]:
    data = load_relaxed_json(text)
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and list_key:
        return {list_key: data}
    raise ValueError("LLM 未返回 JSON 对象")


def clamp_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.0
    return max(0.0, min(10.0, round(score, 2)))


def score_item_from_llm(item: dict[str, Any]) -> dict[str, Any]:
    max_points = safe_number(item.get("max_points")) or 1
    points = max(0.0, min(max_points, safe_number(item.get("points"))))
    return {
        "name": str(item.get("name") or "LLM评分项"),
        "max_points": max_points,
        "points": round(points, 2),
        "hit": bool(item.get("hit", points > 0)),
        "evidence": str(item.get("evidence") or ""),
    }


def llm_score_report(report: dict[str, Any], standard: str) -> dict[str, Any]:
    api_key, base_url, model = llm_settings()
    messages = [
        {
            "role": "system",
            "content": (
                "你是A股盘中交易评分助手。只根据用户给出的盘面数据和评分标准打分，不编造额外行情。"
                "必须只输出JSON，不要输出Markdown。JSON字段：score_10(0-10数字)、signal(中文结论)、"
                "score_items(数组，每项含name,max_points,points,hit,evidence)。"
                "只允许输出一个合法JSON对象，不要代码块，不要解释文字，不要尾逗号，不要使用单引号。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "daily_standard": standard,
                    "market_report": compact_report_for_llm(report),
                    "deterministic_score": {
                        "score_10": report.get("score_10"),
                        "signal": report.get("signal"),
                        "score_items": report.get("score_items"),
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
    data = post_llm_chat(api_key, base_url, model, messages, temperature=0.1, json_mode=True)
    raw_text = data["choices"][0]["message"]["content"]
    scored = extract_json_object(raw_text)
    items = scored.get("score_items") if isinstance(scored.get("score_items"), list) else []
    score_items = [score_item_from_llm(item) for item in items if isinstance(item, dict)]
    score_10 = clamp_score(scored.get("score_10"))
    if not score_items:
        score_items = [
            {
                "name": "LLM综合评分",
                "max_points": 10,
                "points": score_10,
                "hit": score_10 > 0,
                "evidence": str(scored.get("signal") or "LLM按今日标准综合评分"),
            }
        ]
    result = dict(report)
    result["raw_score"] = score_10
    result["score_10"] = score_10
    result["signal"] = str(scored.get("signal") or score_signal(score_10))
    result["score_items"] = score_items
    meta = dict(result.get("meta") or {})
    meta["score_max"] = 10
    meta["score_normalized_10"] = score_10
    meta["scoring_mode"] = "llm"
    meta["llm_scoring_standard"] = standard
    result["meta"] = meta
    return result


def llm_adjust_stock_scores(report: dict[str, Any], standard: str, max_adjustment: float) -> dict[str, Any]:
    api_key, base_url, model = llm_settings()
    stock_scores = report.get("stock_scores") if isinstance(report.get("stock_scores"), list) else []
    if not stock_scores:
        return report
    messages = [
        {
            "role": "system",
            "content": (
                "你是A股盘中个股建议助手。规则分已经算好，你只能基于用户每日标准做有限微调。"
                "不要编造行情。必须只输出JSON，字段stocks为数组；每项含代码,final_score_10,action,reason,risk,watch_points。"
                "action只能是：积极持有、低吸优先、做T观察、冲高兑现、暂缓买入、减仓规避。"
                "只允许输出一个合法JSON对象，不要代码块，不要解释文字，不要尾逗号，不要使用单引号。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "daily_standard": standard,
                    "max_adjustment": max_adjustment,
                    "market_score": report.get("market_score"),
                    "board_score": report.get("board_score"),
                    "board_scores": report.get("board_scores") or [],
                    "stock_scores": stock_scores,
                },
                ensure_ascii=False,
            ),
        },
    ]
    data = post_llm_chat(api_key, base_url, model, messages, temperature=0.1, json_mode=True)
    adjusted = extract_json_object(data["choices"][0]["message"]["content"], list_key="stocks")
    llm_rows = adjusted.get("stocks") if isinstance(adjusted.get("stocks"), list) else []
    by_code = {str(item.get("代码", "")).zfill(6): item for item in llm_rows if isinstance(item, dict)}
    allowed_actions = {"积极持有", "低吸优先", "做T观察", "冲高兑现", "暂缓买入", "减仓规避"}
    next_scores = []
    for item in stock_scores:
        row = dict(item)
        code = str(row.get("代码", "")).zfill(6)
        llm_item = by_code.get(code)
        if llm_item:
            base = safe_number(row.get("综合分"))
            requested = clamp_score(llm_item.get("final_score_10"))
            lower = max(0.0, base - max_adjustment)
            upper = min(10.0, base + max_adjustment)
            final_score = max(lower, min(upper, requested))
            row["规则分"] = base
            row["规则分_display"] = f"{base:.2f}"
            row["综合分"] = round(final_score, 2)
            row["综合分_display"] = f"{final_score:.2f}"
            action = str(llm_item.get("action") or row.get("建议") or "")
            row["建议"] = action if action in allowed_actions else stock_action_label(final_score)
            row["理由"] = str(llm_item.get("reason") or row.get("理由") or "")
            row["风险"] = str(llm_item.get("risk") or "")
            watch = llm_item.get("watch_points")
            row["观察点"] = "；".join(str(x) for x in watch) if isinstance(watch, list) else str(watch or "")
            row["LLM调整"] = round(final_score - base, 2)
            row["LLM调整_display"] = f"{final_score - base:+.2f}"
        next_scores.append(row)
    next_scores.sort(key=lambda item: safe_number(item.get("综合分")), reverse=True)
    result = dict(report)
    result["stock_scores"] = next_scores
    meta = dict(result.get("meta") or {})
    meta["stock_scoring_mode"] = "llm_hybrid"
    meta["llm_stock_standard"] = standard
    meta["llm_stock_max_adjustment"] = max_adjustment
    result["meta"] = meta
    return result


def stock_action_label(score: float) -> str:
    if score >= 7.5:
        return "积极持有"
    if score >= 6.5:
        return "低吸优先"
    if score >= 5.0:
        return "做T观察"
    if score >= 4.0:
        return "冲高兑现"
    if score >= 3.0:
        return "暂缓买入"
    return "减仓规避"


def maybe_apply_llm_scoring(report: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    llm_scoring = settings.get("llmScoring") if isinstance(settings.get("llmScoring"), dict) else {}
    if not llm_scoring.get("enabled"):
        return report
    standard = str(llm_scoring.get("standard") or "").strip()
    if not standard:
        return report
    max_adjustment = max(0.0, safe_number(llm_scoring.get("maxAdjustment")) or 1.0)
    try:
        return llm_adjust_stock_scores(report, standard, max_adjustment)
    except Exception as exc:
        result = dict(report)
        meta = dict(result.get("meta") or {})
        meta["scoring_mode"] = "rules"
        meta["stock_scoring_mode"] = "rules"
        meta["llm_scoring_error"] = friendly_llm_error(exc)
        meta["llm_scoring_error_detail"] = f"{type(exc).__name__}: {exc}"[:500]
        result["meta"] = meta
        return result


def load_market_cap_mapping() -> dict[str, float]:
    try:
        payload = json.loads(MARKET_CAP_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = payload.get("data") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return {}
    mapping: dict[str, float] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        code = normalize_code(item.get("code") or item.get("代码"))
        market_cap = safe_number(item.get("market_cap") or item.get("总市值") or item.get("circ_mv"))
        if code and market_cap:
            mapping[code] = market_cap
    return mapping


def breakout_spot() -> pd.DataFrame:
    try:
        df, _, _ = read_stockmonitor_spot()
    except Exception:
        df = pd.DataFrame()
    if df.empty:
        return df

    cap_map = load_market_cap_mapping()
    if cap_map:
        df["总市值"] = df["代码"].map(cap_map)
        df["市值来源"] = "market_cap_cache"
    elif "总市值" not in df.columns:
        df["总市值"] = pd.NA
        df["市值来源"] = "missing"

    for col in ["成交额", "总市值", "最新价", "涨跌幅"]:
        if col in df.columns:
            df[col] = to_num(df[col])
    df["突破数据源"] = "StockMonitor+market_cap_cache"
    return df


def format_percent(value: Any) -> str:
    number = safe_number(value)
    return f"{number:.2f}%"


def latest_breakout_candidate(
    row: dict[str, Any],
    *,
    min_amount: float,
    days: int,
    end_date: str,
) -> dict[str, Any] | None:
    code = normalize_code(row.get("代码"))
    if not code:
        return None
    try:
        hist = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date="19900101",
            end_date=end_date,
            adjust="",
        )
    except Exception:
        return None
    if hist.empty or not {"日期", "收盘", "成交额"}.issubset(hist.columns):
        return None

    hist["日期"] = pd.to_datetime(hist["日期"], errors="coerce")
    hist["收盘"] = to_num(hist["收盘"])
    hist["成交额"] = to_num(hist["成交额"])
    if "涨跌幅" in hist.columns:
        hist["涨跌幅"] = to_num(hist["涨跌幅"])
    hist = hist.dropna(subset=["日期", "收盘", "成交额"]).sort_values("日期")
    if len(hist) <= days:
        return None

    last = hist.iloc[-1]
    last_days = hist.tail(days)
    if len(last_days) < days or not bool((last_days["成交额"] > min_amount).all()):
        return None

    previous = hist.iloc[:-1]
    previous_max_close = safe_number(previous["收盘"].max())
    latest_close = safe_number(last["收盘"])
    if latest_close <= previous_max_close:
        return None

    amount_values = [money_yi(value) for value in last_days["成交额"].tolist()]
    market_cap = safe_number(row.get("总市值"))
    latest_amount = safe_number(row.get("成交额"))
    change_pct = safe_number(row.get("涨跌幅"))
    latest_date = last["日期"].strftime("%Y-%m-%d")
    return {
        "代码": code,
        "名称": str(row.get("名称") or ""),
        "收盘价": latest_close,
        "收盘价_display": f"{latest_close:.2f}",
        "涨跌幅": change_pct,
        "涨跌幅_display": format_percent(change_pct),
        "总市值": market_cap,
        "总市值_display": money_yi(market_cap),
        "今日成交额": latest_amount,
        "今日成交额_display": money_yi(latest_amount),
        "近3日成交额": " / ".join(amount_values),
        "此前最高收盘": previous_max_close,
        "此前最高收盘_display": f"{previous_max_close:.2f}",
        "突破幅度": (latest_close / previous_max_close - 1) * 100 if previous_max_close else 0,
        "突破幅度_display": format_percent((latest_close / previous_max_close - 1) * 100 if previous_max_close else 0),
        "日期": latest_date,
    }


def breakout_param(settings: dict[str, Any], name: str, fallback: float) -> float:
    breakout = settings.get("breakout") if isinstance(settings.get("breakout"), dict) else {}
    request_value = request.args.get(name)
    value = safe_number(request_value) if request_value is not None else safe_number(breakout.get(name))
    return value if value is not None else fallback


def knowledge_latest_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
    return str(row[0]) if row and row[0] else None


def query_knowledge_breakouts(
    *,
    target_date: str,
    min_amount: float,
    min_market_cap: float,
    max_market_cap: float,
    days: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not KNOWLEDGE_DB_FILE.exists():
        raise FileNotFoundError(f"knowledge.db不存在: {KNOWLEDGE_DB_FILE}")

    conn = sqlite3.connect(f"file:{KNOWLEDGE_DB_FILE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        candidate_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM stock_daily t
            JOIN stock_info si ON si.code = t.code
            WHERE t.date = ?
              AND t.close > 0
              AND t.amount > ?
              AND si.circ_mv >= ?
              AND si.circ_mv <= ?
            """,
            (target_date, min_amount, min_market_cap, max_market_cap),
        ).fetchone()[0]

        rows = conn.execute(
            """
            WITH today AS (
                SELECT t.code, t.name, t.close, t.amount, t.change_pct, si.circ_mv AS market_cap
                FROM stock_daily t
                JOIN stock_info si ON si.code = t.code
                WHERE t.date = ?
                  AND t.close > 0
                  AND t.amount > ?
                  AND si.circ_mv >= ?
                  AND si.circ_mv <= ?
            ),
            prev AS (
                SELECT sd.code, MAX(sd.close) AS prev_max_close
                FROM stock_daily sd
                JOIN today t ON t.code = sd.code
                WHERE sd.date < ?
                GROUP BY sd.code
            ),
            listed AS (
                SELECT sd.code, COUNT(*) AS listed_days
                FROM stock_daily sd
                JOIN today t ON t.code = sd.code
                WHERE sd.date <= ?
                GROUP BY sd.code
            ),
            recent_ranked AS (
                SELECT sd.code, sd.date, sd.amount,
                       ROW_NUMBER() OVER (PARTITION BY sd.code ORDER BY sd.date DESC) AS rn
                FROM stock_daily sd
                JOIN today t ON t.code = sd.code
                WHERE sd.date <= ?
            ),
            recent AS (
                SELECT code, COUNT(*) AS recent_count, MIN(amount) AS recent_min_amount, AVG(amount) AS recent_avg_amount
                FROM recent_ranked
                WHERE rn <= ?
                GROUP BY code
            )
            SELECT
                t.code, t.name, t.close, t.amount, t.change_pct,
                p.prev_max_close, t.market_cap,
                l.listed_days, r.recent_count, r.recent_min_amount, r.recent_avg_amount
            FROM today t
            JOIN prev p ON p.code = t.code
            JOIN recent r ON r.code = t.code
            JOIN listed l ON l.code = t.code
            WHERE p.prev_max_close > 0
              AND t.close > p.prev_max_close
              AND r.recent_count >= ?
              AND r.recent_min_amount > ?
            ORDER BY t.amount DESC
            """,
            (
                target_date,
                min_amount,
                min_market_cap,
                max_market_cap,
                target_date,
                target_date,
                target_date,
                days,
                days,
                min_amount,
            ),
        ).fetchall()

        records: list[dict[str, Any]] = []
        codes = [row["code"] for row in rows]
        recent_details: dict[str, list[sqlite3.Row]] = {}
        if codes:
            placeholders = ",".join("?" for _ in codes)
            detail_rows = conn.execute(
                f"""
                WITH ranked AS (
                    SELECT code, date, amount,
                           ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                    FROM stock_daily
                    WHERE date <= ? AND code IN ({placeholders})
                )
                SELECT code, date, amount
                FROM ranked
                WHERE rn <= ?
                ORDER BY code, date DESC
                """,
                [target_date, *codes, days],
            ).fetchall()
            for detail in detail_rows:
                recent_details.setdefault(detail["code"], []).append(detail)

        for row in rows:
            close = safe_number(row["close"])
            previous_max = safe_number(row["prev_max_close"])
            amount_values = [money_yi(detail["amount"]) for detail in recent_details.get(row["code"], [])]
            breakout_pct = (close / previous_max - 1) * 100 if close and previous_max else 0
            record = {
                "代码": row["code"],
                "名称": row["name"],
                "收盘价": close,
                "收盘价_display": f"{close:.2f}" if close is not None else "-",
                "涨跌幅": safe_number(row["change_pct"]),
                "涨跌幅_display": format_percent(row["change_pct"]),
                "总市值": safe_number(row["market_cap"]),
                "总市值_display": money_yi(row["market_cap"]),
                "今日成交额": safe_number(row["amount"]),
                "今日成交额_display": money_yi(row["amount"]),
                "近3日成交额": " / ".join(amount_values),
                "近N日成交额": " / ".join(amount_values),
                "此前最高收盘": previous_max,
                "此前最高收盘_display": f"{previous_max:.2f}" if previous_max is not None else "-",
                "突破幅度": breakout_pct,
                "突破幅度_display": format_percent(breakout_pct),
                "上市交易日": int(row["listed_days"] or 0),
                "日期": target_date,
            }
            records.append(record)

        meta = {
            "candidateCount": int(candidate_count or 0),
            "scannedCount": int(candidate_count or 0),
            "resultCount": len(records),
            "targetDate": target_date,
            "knowledgeDb": str(KNOWLEDGE_DB_FILE),
            "candidateSource": "knowledge.db stock_daily + stock_info",
            "marketCapSource": "knowledge.db stock_info.circ_mv",
        }
        return records, meta
    finally:
        conn.close()


def get_breakout_results(force: bool = False) -> dict[str, Any]:
    with _settings_lock:
        settings = read_settings_unlocked()
    min_amount_yi = max(0.0, breakout_param(settings, "minAmountYi", 10.0))
    min_market_cap_yi = max(0.0, breakout_param(settings, "minMarketCapYi", 70.0))
    max_market_cap_yi = max(min_market_cap_yi, breakout_param(settings, "maxMarketCapYi", 700.0))
    days = max(1, int(breakout_param(settings, "days", 3.0)))

    requested_date = (request.args.get("date") or "").strip()
    if requested_date:
        if not DATE_RE.match(requested_date):
            raise ValueError("日期格式必须是 YYYY-MM-DD")
        target_date = requested_date
    else:
        if not KNOWLEDGE_DB_FILE.exists():
            raise FileNotFoundError(f"knowledge.db不存在: {KNOWLEDGE_DB_FILE}")
        conn = sqlite3.connect(f"file:{KNOWLEDGE_DB_FILE}?mode=ro", uri=True)
        try:
            target_date = knowledge_latest_date(conn)
        finally:
            conn.close()
        if not target_date:
            raise ValueError("knowledge.db 的 stock_daily 表没有可用日期")

    cache_key = f"kb:{target_date}:{min_amount_yi}:{min_market_cap_yi}:{max_market_cap_yi}:{days}"
    now = time.time()
    with _breakout_lock:
        cached = _breakout_cache.get(cache_key)
        if not force and cached and now - cached["loaded_at"] < 6 * 60 * 60:
            return cached["payload"]

    min_amount = min_amount_yi * 100_000_000
    min_market_cap = min_market_cap_yi * 100_000_000
    max_market_cap = max_market_cap_yi * 100_000_000
    records, db_meta = query_knowledge_breakouts(
        target_date=target_date,
        min_amount=min_amount,
        min_market_cap=min_market_cap,
        max_market_cap=max_market_cap,
        days=days,
    )
    records.sort(key=lambda item: item.get("今日成交额", 0), reverse=True)
    payload = {
        "ok": True,
        "records": records,
        "meta": {
            "loadedAt": now_cn().strftime("%Y-%m-%d %H:%M:%S %Z"),
            "candidateCount": db_meta.get("candidateCount", 0),
            "scannedCount": db_meta.get("scannedCount", 0),
            "resultCount": len(records),
            "minAmountYi": min_amount_yi,
            "minMarketCapYi": min_market_cap_yi,
            "maxMarketCapYi": max_market_cap_yi,
            "days": days,
            "targetDate": target_date,
            "rule": "今日收盘价 > 上市以来此前最高收盘价，且最近N个交易日每日成交额均大于阈值",
            **db_meta,
        },
    }
    with _breakout_lock:
        _breakout_cache[cache_key] = {"loaded_at": now, "payload": payload}
    return payload

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/settings")
def settings_page():
    return send_from_directory(STATIC_DIR, "settings.html")


@app.get("/breakouts")
def breakouts_page():
    return send_from_directory(STATIC_DIR, "breakouts.html")


@app.get("/api/report")
def api_report():
    board_keyword = request.args.get("board", "光纤").strip() or "光纤"
    force = request.args.get("force") == "1"
    try:
        return jsonify(get_report(board_keyword, force=force))
    except Exception as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "refreshSeconds": dynamic_refresh_seconds(),
                }
            ),
            500,
        )


@app.get("/api/settings")
def api_get_settings():
    with _settings_lock:
        settings = read_settings_unlocked()
    return jsonify({"ok": True, "settings": public_settings(settings)})


@app.post("/api/settings")
def api_save_settings():
    try:
        body = request.get_json(silent=True) or {}
        with _settings_lock:
            current = read_settings_unlocked()
            incoming = body.get("settings") if isinstance(body.get("settings"), dict) else {}
            next_settings = deep_merge_dict(current, incoming)
            incoming_llm = incoming.get("llm") if isinstance(incoming.get("llm"), dict) else {}
            if "apiKey" not in incoming_llm or str(incoming_llm.get("apiKey") or "") == "":
                next_settings["llm"]["apiKey"] = current.get("llm", {}).get("apiKey", "")
            elif incoming_llm.get("apiKey") == "__CLEAR__":
                next_settings["llm"]["apiKey"] = ""
            write_settings_unlocked(next_settings)
        with _cache_lock:
            _report_cache.clear()
        with _breakout_lock:
            _breakout_cache.clear()
        return jsonify({"ok": True, "settings": public_settings(next_settings)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400


@app.get("/api/breakouts")
def api_breakouts():
    force = request.args.get("force") == "1"
    try:
        return jsonify(get_breakout_results(force=force))
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@app.get("/api/notes")
def api_get_note():
    try:
        note_date = valid_note_date(request.args.get("date"))
        with _notes_lock:
            notes = read_notes_unlocked()
        entries = notes.get(note_date, [])
        return jsonify({"ok": True, "date": note_date, "entries": entries, "content": entries[0]["content"] if entries else ""})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400


@app.post("/api/notes")
def api_save_note():
    try:
        body = request.get_json(silent=True) or {}
        note_date = valid_note_date(body.get("date"))
        entry_id = str(body.get("id") or "").strip()
        title = str(body.get("title") or "").strip()
        content = str(body.get("content", ""))[:20000]
        with _notes_lock:
            notes = read_notes_unlocked()
            entries = notes.get(note_date, [])
            target = next((entry for entry in entries if entry.get("id") == entry_id), None)
            if target:
                target["title"] = title or target.get("title") or now_cn().strftime("%H:%M")
                target["content"] = content
                target["updatedAt"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
                saved = target
            else:
                saved = note_entry(title or now_cn().strftime("%H:%M"), content)
                entries.append(saved)
            notes[note_date] = entries
            write_notes_unlocked(notes)
        return jsonify({"ok": True, "date": note_date, "entry": saved, "entries": notes[note_date], "content": saved["content"]})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400


@app.post("/api/notes/summarize")
def api_summarize_note():
    try:
        body = request.get_json(silent=True) or {}
        note_date = valid_note_date(body.get("date"))
        content = str(body.get("content", ""))[:20000]
        report = body.get("report") if isinstance(body.get("report"), dict) else {}
        summary = summarize_with_llm(note_date, content, report)
        return jsonify({"ok": True, "date": note_date, "summary": summary})
    except Exception as exc:
        return jsonify({"ok": False, "error": friendly_llm_summary_error(exc)}), 400


def main() -> None:
    disable_requests_env_proxy()
    app.run(host="0.0.0.0", port=8765, debug=False)


if __name__ == "__main__":
    main()
