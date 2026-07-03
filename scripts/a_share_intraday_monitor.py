#!/usr/bin/env python
"""
A-share intraday monitor.

Runs one snapshot and prints a Chinese market-readout report for:
- Top 20 A-share stocks by turnover
- Main capital flow for those stocks
- A target board/concept, defaulting to "光纤"
- A 10-point score derived from the user's intraday rules
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import io
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    ZoneInfo = None

try:
    import akshare as ak
    import pandas as pd
    import requests
except ImportError as exc:
    print(
        "缺少依赖。请先运行: python -m pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


RAW_SCORE_MAX = 10
MIN_BOARD_SAMPLE_SIZE = 8
KS11_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EKS11?interval=1d&range=1mo"
_KS11_YAHOO_CACHE: dict[str, Any] = {"loaded_at": 0.0, "item": None, "error": None}
DEFAULT_SCORING_CONFIG = {
    "signals": {
        "bullishAbove": 6,
        "neutralMin": 4,
        "neutralMax": 6,
        "bullishText": "多头信号：择先买再卖，积极持有",
        "neutralText": "震荡信号：冲高兑现，积极做差价",
        "weakText": "偏弱信号：先卖再买，亦可延迟买入",
    },
    "criteria": {
        "market_index_resonance": {"enabled": True, "points": 2},
        "market_breadth": {"enabled": True, "points": 2},
        "market_turnover_core": {"enabled": True, "points": 2, "limit": 20},
        "market_fund_diffusion": {"enabled": True, "points": 2},
        "market_risk_control": {"enabled": True, "points": 2},
        "board_high_open": {"enabled": True, "points": 0.5, "threshold": 0.5},
        "board_pullback_recovery": {"enabled": True, "points": 1},
        "board_new_high": {"enabled": True, "points": 1},
        "board_main_fund_inflow": {"enabled": True, "points": 1.5},
        "board_red_green": {"enabled": True, "points": 2},
        "board_top_turnover_red": {"enabled": True, "points": 2, "limit": 5, "minRed": 3},
        "board_median_positive": {"enabled": True, "points": 1.5, "threshold": 0},
        "board_fund_breadth": {"enabled": True, "points": 1.5},
        "high_open": {"enabled": True, "points": 1, "threshold": 0.5},
        "pullback_recovery": {"enabled": True, "points": 1},
        "new_high": {"enabled": True, "points": 1},
        "main_fund_inflow": {"enabled": True, "points": 1},
        "top20_red_green": {"enabled": True, "points": 2},
    },
    "stockWeights": {"market": 0.30, "board": 0.30, "stock": 0.30, "risk": 0.10},
    "stockRules": {
        "redChangePoints": 2,
        "fundInflowPoints": 3,
        "highTurnoverPoints": 2,
        "highTurnoverThresholdYi": 100,
        "mediumTurnoverPoints": 1,
        "mediumTurnoverThresholdYi": 50,
        "highOpenPoints": 1,
        "boardMemberPoints": 2,
    },
    "boardThemeScoring": {
        "enabled": True,
        "precondition": {
            "indexCode": "KS11",
            "indexName": "韩国综合指数",
            "limitDownPct": -8,
            "triggerScore": 0,
            "signal": "韩国综合指数触及跌停，科技股日内全线看空，冲高卖出",
        },
        "signals": {
            "bullishAbove": 7,
            "neutralMin": 4,
            "neutralMax": 7,
            "bullishText": "积极看多，珍惜筹码，谨慎做空",
            "neutralText": "震荡市的一天，冲高减仓，不追加筹码，积极做T，延迟买入",
            "weakText": "科技股熊市的一天，积极卖出，延迟买入，尾盘买回（或底仓）/次日早盘杀跌抄底",
        },
        "groups": [
            {
                "name": "光纤三剑客",
                "theme": "光纤",
                "stocks": [
                    {"code": "601869", "name": "长飞光纤"},
                    {"code": "600487", "name": "亨通光电"},
                    {"code": "600522", "name": "中天科技"},
                ],
            },
            {
                "name": "科技三剑客",
                "theme": "科技核心股",
                "stocks": [
                    {"code": "300308", "name": "中际旭创"},
                    {"code": "300502", "name": "新易盛"},
                    {"code": "688256", "name": "寒武纪"},
                ],
            },
        ],
    },
}

STOCK_FLOW_MODELS = {
    "10CM": [
        (0.0, 1.5, 0.02, "有承接，属于低位健康蓄势", "资金不足则小心弱反抽", "可持有观察；不急卖"),
        (1.5, 3.0, 0.04, "健康上涨，资金与涨幅匹配", "资金不足则上涨质量一般", "持有为主；低吸需看承接"),
        (3.0, 4.5, 0.06, "主动进攻中，资金开始确认", "资金不足则容易冲高回落", "继续持有；可小幅做T"),
        (4.5, 6.0, 0.08, "强进攻分时，进入高抛观察区", "资金不足则优先高抛T仓", "达标持有；不达标冲高减仓"),
        (6.0, 8.0, 0.12, "强势加速，需资金持续配合", "资金不足则高位震荡风险大", "达标继续看；不达标减T仓"),
        (8.0, None, 0.175, "接近涨停/冲板博弈，必须强资金确认", "资金不足则容易炸板或回落", "看联动与封板强度；不达标减仓"),
    ],
    "20CM": [
        (0.0, 2.0, 0.02, "低位承接，有蓄势可能", "资金不足则只是弱反弹", "可观察持有"),
        (2.0, 4.0, 0.04, "健康上涨，符合20CM正常波动", "资金不足则上涨质量一般", "持有观察"),
        (4.0, 6.0, 0.06, "主动走强区，不必过早高抛", "资金不足则可能被带回落", "达标持有；不达标轻减T仓"),
        (6.0, 8.0, 0.08, "进入强势区，需要资金确认", "资金不足则冲高回落概率上升", "观察盘口和联动"),
        (8.0, 10.0, 0.11, "20CM高抛观察区", "资金不足则优先高抛T仓", "达标继续看；不达标减仓"),
        (10.0, 13.0, 0.14, "加速区，资金必须强", "资金不足则大概率震荡回落", "不达标不追；达标持有"),
        (13.0, 16.0, 0.18, "强加速区，进入冲板前奏", "资金不足则风险很大", "只看极强；不达标减仓"),
        (16.0, None, 0.26, "极强冲板/封板状态", "资金不足则容易炸板", "看封单/联动；不达标减仓"),
    ],
}

BC_LINKAGE_STATES = [
    (None, -0.05, 2.0, "BC重度流出", "板块拖累极强，A必须极强才有效", "达标：极强龙头强攻，观察BC是否止跌回流；未达标：禁止追买，冲高减仓，买点延后至尾盘/次日"),
    (-0.05, -0.02, 1.5, "BC中度流出", "A面对明显拖累，需要强攻确认", "达标：A有带队能力，积极观察做多；未达标：孤军冲高，冲高减T仓，不急低吸"),
    (-0.02, -0.01, 1.2, "BC轻度流出", "板块轻度拖累，A需高于普通标准", "达标：上涨有效，可持有；未达标：上涨质量一般，轻减T仓"),
    (-0.01, 0.01, 1.0, "BC中性震荡", "板块无明显拖累，按普通标准", "达标：走势健康；未达标：等待承接确认"),
    (0.01, 0.02, 0.9, "BC轻度流入", "板块轻度托举，A标准小幅下调", "达标：板块托举，持有观察；未达标：A弱于板块，不优先做A"),
    (0.02, 0.05, 0.8, "BC中度流入", "板块明显共振，A可享受托举", "达标：共振进攻，积极持有；未达标：A相对弱，优先看BC核心"),
    (0.05, None, 0.7, "BC强流入", "板块强共振，A有补涨和继续冲高可能", "达标：强共振，积极做多，不轻易高抛；未达标：A严重落后，谨慎换强不换弱"),
]

THEME_STOCK_NAMES = {
    "光纤": [
        "长飞光纤",
        "亨通光电",
        "烽火通信",
        "永鼎股份",
        "通鼎互联",
        "特发信息",
        "中天科技",
        "富通信息",
        "汇源通信",
        "光迅科技",
        "博创科技",
        "太辰光",
        "天孚通信",
        "中际旭创",
        "新易盛",
        "源杰科技",
        "剑桥科技",
        "华工科技",
        "德科立",
        "联特科技",
        "仕佳光子",
        "光库科技",
        "铭普光磁",
        "罗博特科",
    ],
    "CPO": [
        "中际旭创",
        "新易盛",
        "天孚通信",
        "太辰光",
        "源杰科技",
        "剑桥科技",
        "华工科技",
        "联特科技",
        "德科立",
        "博创科技",
        "光迅科技",
        "光库科技",
        "仕佳光子",
        "罗博特科",
    ],
    "通信设备": [
        "中际旭创",
        "新易盛",
        "天孚通信",
        "光迅科技",
        "烽火通信",
        "中兴通讯",
        "亨通光电",
        "中天科技",
        "长飞光纤",
        "华工科技",
    ],
    "芯片": [
        "寒武纪",
        "海光信息",
        "中芯国际",
        "华虹公司",
        "兆易创新",
        "澜起科技",
        "佰维存储",
        "江波龙",
        "德明利",
        "香农芯创",
        "北方华创",
        "中微公司",
        "韦尔股份",
        "长电科技",
        "通富微电",
        "紫光国微",
        "北京君正",
        "卓胜微",
        "圣邦股份",
        "晶晨股份",
        "中颖电子",
        "全志科技",
        "富瀚微",
        "国科微",
        "景嘉微",
        "龙芯中科",
        "士兰微",
        "斯达半导",
        "纳芯微",
        "芯原股份",
        "复旦微电",
        "华海清科",
        "拓荆科技",
        "盛美上海",
        "沪硅产业",
        "晶合集成",
        "东芯股份",
        "艾为电子",
        "翱捷科技",
        "安路科技",
        "概伦电子",
    ],
    "半导体": [
        "寒武纪",
        "海光信息",
        "中芯国际",
        "华虹公司",
        "兆易创新",
        "澜起科技",
        "佰维存储",
        "江波龙",
        "德明利",
        "香农芯创",
        "北方华创",
        "中微公司",
        "韦尔股份",
        "长电科技",
        "通富微电",
        "紫光国微",
        "北京君正",
        "卓胜微",
        "圣邦股份",
        "晶晨股份",
        "中颖电子",
        "全志科技",
        "富瀚微",
        "国科微",
        "景嘉微",
        "龙芯中科",
        "士兰微",
        "斯达半导",
        "纳芯微",
        "芯原股份",
        "复旦微电",
        "华海清科",
        "拓荆科技",
        "盛美上海",
        "沪硅产业",
        "晶合集成",
        "东芯股份",
        "艾为电子",
        "翱捷科技",
        "安路科技",
        "概伦电子",
    ],
}


def disable_requests_env_proxy() -> None:
    """Avoid stale system proxies breaking Eastmoney requests."""
    for key in [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]:
        os.environ.pop(key, None)

    original_session = requests.Session

    def session_without_env_proxy(*args: Any, **kwargs: Any) -> requests.Session:
        session = original_session(*args, **kwargs)
        session.trust_env = False
        return session

    requests.Session = session_without_env_proxy  # type: ignore[assignment]


@dataclasses.dataclass
class ScoreItem:
    name: str
    max_points: float
    points: float
    hit: bool
    evidence: str


def deep_merge_dict(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    merged = json.loads(json.dumps(base, ensure_ascii=False))
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def active_scoring_config(args: argparse.Namespace) -> dict[str, Any]:
    return deep_merge_dict(DEFAULT_SCORING_CONFIG, getattr(args, "scoring_config", None))


def criterion_config(config: dict[str, Any], key: str) -> dict[str, Any]:
    criteria = config.get("criteria") if isinstance(config.get("criteria"), dict) else {}
    item = criteria.get(key) if isinstance(criteria.get(key), dict) else {}
    return item


def criterion_float(config: dict[str, Any], key: str, field: str, default: float) -> float:
    value = criterion_config(config, key).get(field, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def criterion_int(config: dict[str, Any], key: str, field: str, default: int) -> int:
    value = criterion_config(config, key).get(field, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def configured_score_item(
    config: dict[str, Any],
    key: str,
    name: str,
    hit: bool,
    evidence: str,
) -> ScoreItem | None:
    item_config = criterion_config(config, key)
    if item_config.get("enabled", True) is False:
        return None
    points = criterion_float(config, key, "points", 0)
    points = max(0.0, points)
    return ScoreItem(name, points, points if hit else 0.0, hit, evidence)


def configured_score_item_ratio(
    config: dict[str, Any],
    key: str,
    name: str,
    ratio: float,
    evidence: str,
) -> ScoreItem | None:
    item_config = criterion_config(config, key)
    if item_config.get("enabled", True) is False:
        return None
    max_points = max(0.0, criterion_float(config, key, "points", 0))
    bounded = max(0.0, min(1.0, ratio))
    return ScoreItem(name, max_points, round(max_points * bounded, 2), bounded >= 0.75, evidence)


def score_group(items: list[ScoreItem], signal_config: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_score = round(sum(item.points for item in items), 2)
    score_max = round(sum(item.max_points for item in items), 2)
    score_10 = round((raw_score / score_max * RAW_SCORE_MAX) if score_max else 0, 2)
    return {
        "raw_score": raw_score,
        "score_max": score_max,
        "score_10": score_10,
        "signal": score_signal(score_10, signal_config),
        "items": [dataclasses.asdict(item) for item in items],
    }


def score_weights(config: dict[str, Any]) -> dict[str, float]:
    weights = config.get("stockWeights") if isinstance(config.get("stockWeights"), dict) else {}
    market = max(0.0, float(weights.get("market", 0.30) or 0.0))
    board = max(0.0, float(weights.get("board", 0.30) or 0.0))
    stock = max(0.0, float(weights.get("stock", 0.30) or 0.0))
    risk = max(0.0, float(weights.get("risk", 0.10) or 0.0))
    total = market + board + stock + risk
    if total <= 0:
        return {"market": 0.30, "board": 0.30, "stock": 0.30, "risk": 0.10}
    if total <= 1.000001:
        return {"market": market, "board": board, "stock": stock, "risk": risk + max(0.0, 1.0 - total)}
    return {"market": market / total, "board": board / total, "stock": stock / total, "risk": risk / total}


def now_cn() -> datetime:
    if ZoneInfo is None:
        return datetime.now()
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def to_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def first_existing_column(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def column_contains(df: pd.DataFrame, *needles: str) -> str | None:
    for col in df.columns:
        if all(needle in col for needle in needles):
            return col
    return None


def safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        if math.isnan(result):
            return None
        return result
    except (TypeError, ValueError):
        return None


def normalize_code(value: Any) -> str:
    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:].zfill(6) if digits else text


def money_yi(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return "-"
    return f"{number / 100_000_000:.2f}亿"


def pct(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return "-"
    return f"{number:.2f}%"


def stock_market(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("0", "2", "3")):
        return "sz"
    return "bj"


def fetch_spot() -> pd.DataFrame:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        df = ak.stock_zh_a_spot()

    for col in ["成交额", "涨跌幅", "最新价", "今开", "昨收", "最高", "最低"]:
        if col in df.columns:
            df[col] = to_num(df[col])
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    df["代码"] = df["代码"].map(normalize_code)
    df["行情来源"] = "新浪"

    try:
        em_df = ak.stock_zh_a_spot_em()
    except Exception:
        return df

    if em_df.empty or "代码" not in em_df.columns:
        return df

    em_df["代码"] = em_df["代码"].map(normalize_code)
    supplement_cols = [col for col in ["代码", "总市值", "流通市值", "换手率", "市盈率-动态"] if col in em_df.columns]
    if len(supplement_cols) > 1:
        df = df.merge(em_df[supplement_cols], on="代码", how="left")
        df["行情来源"] = "新浪+东方财富补充"
    return df


def fetch_individual_fund_rank() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []

    try:
        df = ak.stock_individual_fund_flow_rank(indicator="今日")
        if "代码" in df.columns:
            df["代码"] = df["代码"].map(normalize_code)
            df["资金流来源"] = "东方财富"
            frames.append(df)
    except Exception:
        pass

    try:
        tx_fund = fetch_tencent_fund_rank_by_turnover()
        if not tx_fund.empty:
            frames.append(tx_fund)
    except Exception:
        pass

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    for col in df.columns:
        if "净流入" in col or "涨跌幅" in col:
            df[col] = to_num(df[col])
    flow_col = find_main_flow_column(df)
    if flow_col:
        df["_has_flow"] = df[flow_col].notna()
        df["_source_rank"] = df["资金流来源"].map({"东方财富": 0, "腾讯": 1, "腾讯成交额榜": 1}).fillna(9)
        df = df.sort_values(["代码", "_has_flow", "_source_rank"], ascending=[True, False, True])
        df = df.drop_duplicates(subset=["代码"], keep="first").drop(columns=["_has_flow", "_source_rank"])
    return df


def fetch_tencent_fund_rank_by_turnover() -> pd.DataFrame:
    url = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
    pages = max(1, int(safe_float(os.environ.get("TENCENT_FUND_PAGES")) or 3))
    count = 200
    rows: list[dict[str, Any]] = []
    for page in range(pages):
        params = {
            "_appver": "11.17.0",
            "board_code": "aStock",
            "sort_type": "turnover",
            "direct": "down",
            "offset": str(page * count),
            "count": str(count),
        }
        response = requests.get(url, params=params, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        payload = response.json()
        rank_list = ((payload.get("data") or {}).get("rank_list") or [])
        if not isinstance(rank_list, list) or not rank_list:
            break
        rows.extend(item for item in rank_list if isinstance(item, dict))

    if not rows:
        return pd.DataFrame()

    tx_df = pd.DataFrame(rows)
    if "code" not in tx_df.columns or "zljlr" not in tx_df.columns:
        return pd.DataFrame()
    result = pd.DataFrame(
        {
            "代码": tx_df["code"].map(normalize_code),
            "名称": tx_df["name"] if "name" in tx_df.columns else "",
            "今日主力净流入-净额": to_num(tx_df["zljlr"]) * 10_000,
            "资金流来源": "腾讯成交额榜",
        }
    )
    return result.drop_duplicates(subset=["代码"])


def fetch_sector_fund_rank(kind: str) -> pd.DataFrame:
    try:
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type=kind)
    except Exception:
        return pd.DataFrame()
    for col in df.columns:
        if "净流入" in col or "涨跌幅" in col:
            df[col] = to_num(df[col])
    return df


def fetch_board_names() -> list[dict[str, Any]]:
    boards: list[dict[str, Any]] = []
    sources = [
        ("行业", ak.stock_board_industry_name_em),
        ("概念", ak.stock_board_concept_name_em),
    ]
    for source_name, func in sources:
        try:
            df = func()
        except Exception:
            continue
        name_col = first_existing_column(df, ["板块名称", "名称"])
        if not name_col:
            continue
        for _, row in df.iterrows():
            item = row.to_dict()
            item["_source"] = source_name
            item["_name"] = str(row[name_col])
            boards.append(item)
    return boards


def choose_board(keyword: str) -> dict[str, Any] | None:
    boards = fetch_board_names()
    matches = [item for item in boards if keyword in item.get("_name", "")]
    if not matches:
        return {"_source": "自定义", "_name": keyword, "_fallback": True}

    def rank(item: dict[str, Any]) -> tuple[int, float]:
        name = item.get("_name", "")
        exact_bonus = 2 if name == keyword else 0
        contains_bonus = 1 if name.startswith(keyword) else 0
        amount = safe_float(item.get("成交额")) or 0.0
        return exact_bonus + contains_bonus, amount

    return sorted(matches, key=rank, reverse=True)[0]


def fetch_board_constituents(board: dict[str, Any]) -> pd.DataFrame:
    source = board["_source"]
    name = board["_name"]
    if source == "自定义":
        names = THEME_STOCK_NAMES.get(name, [])
        return pd.DataFrame({"名称": names})
    if source == "行业":
        df = ak.stock_board_industry_cons_em(symbol=name)
    else:
        df = ak.stock_board_concept_cons_em(symbol=name)
    if "代码" in df.columns:
        df["代码"] = df["代码"].map(normalize_code)
    return df


def fallback_board_spot(keyword: str, constituents: pd.DataFrame, spot: pd.DataFrame) -> pd.DataFrame:
    if not constituents.empty and "代码" in constituents.columns:
        return constituents[["代码"]].merge(spot, on="代码", how="inner")

    frames: list[pd.DataFrame] = []
    if not constituents.empty and "名称" in constituents.columns:
        names = constituents["名称"].dropna().astype(str).tolist()
        frames.append(spot[spot["名称"].astype(str).isin(names)])

    if keyword:
        frames.append(spot[spot["名称"].astype(str).str.contains(keyword, regex=False, na=False)])
        for sector_col in ["板块", "sector", "行业", "概念"]:
            if sector_col in spot.columns:
                frames.append(spot[spot[sector_col].astype(str).str.contains(keyword, regex=False, na=False)])

    for theme, names in THEME_STOCK_NAMES.items():
        if keyword in theme or theme in keyword:
            frames.append(spot[spot["名称"].astype(str).isin(names)])

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["代码"])


def board_sample_too_small(board_spot: pd.DataFrame) -> bool:
    return len(board_spot) < MIN_BOARD_SAMPLE_SIZE


def sample_size_evidence(board_spot: pd.DataFrame) -> str:
    return f"板块样本仅 {len(board_spot)} 只，低于 {MIN_BOARD_SAMPLE_SIZE} 只阈值，暂不触发该条件"


def merge_spot_with_funds(spot: pd.DataFrame, fund: pd.DataFrame) -> pd.DataFrame:
    if fund.empty or "代码" not in fund.columns:
        return spot.copy()

    fund_cols = ["代码"]
    for col in fund.columns:
        if col != "代码" and ("主力净流入" in col or "超大单净流入" in col or col == "资金流来源"):
            fund_cols.append(col)

    return spot.merge(fund[fund_cols], on="代码", how="left")


def top_turnover_table(spot: pd.DataFrame, fund: pd.DataFrame, limit: int) -> pd.DataFrame:
    top = spot.sort_values("成交额", ascending=False).head(limit).copy()
    return merge_spot_with_funds(top, fund)


def find_main_flow_column(df: pd.DataFrame) -> str | None:
    candidates = [
        "今日主力净流入-净额",
        "主力净流入-净额",
        "主力净流入净额",
    ]
    return first_existing_column(df, candidates) or column_contains(df, "主力", "净流入", "净额")


def board_main_flow(board: dict[str, Any]) -> tuple[float | None, str]:
    if board["_source"] == "自定义":
        return None, "自定义主题池未取东方财富板块资金流"

    sector_type = "行业资金流" if board["_source"] == "行业" else "概念资金流"
    df = fetch_sector_fund_rank(sector_type)
    if df.empty:
        return None, "未取到板块资金流"

    name_col = first_existing_column(df, ["名称", "板块名称"])
    flow_col = find_main_flow_column(df)
    if not name_col or not flow_col:
        return None, "板块资金流字段缺失"

    match = df[df[name_col].astype(str) == board["_name"]]
    if match.empty:
        fuzzy = df[df[name_col].astype(str).str.contains(board["_name"], regex=False, na=False)]
        match = fuzzy
    if match.empty:
        return None, f"资金流榜未匹配到 {board['_name']}"

    value = safe_float(match.iloc[0][flow_col])
    return value, f"{board['_source']}板块 {board['_name']} 主力净流入 {money_yi(value)}"


def aggregate_board_fund_flow(board_spot: pd.DataFrame, fund: pd.DataFrame) -> tuple[float | None, str]:
    if board_sample_too_small(board_spot):
        return None, sample_size_evidence(board_spot)

    if board_spot.empty or fund.empty or "代码" not in fund.columns:
        return None, "未取到可聚合的成分股主力资金流"

    flow_col = find_main_flow_column(fund)
    if not flow_col:
        return None, "个股资金流字段缺失"

    merged = board_spot[["代码", "名称"]].merge(fund[["代码", flow_col]], on="代码", how="left")
    merged[flow_col] = to_num(merged[flow_col])
    valid = merged.dropna(subset=[flow_col])
    if valid.empty:
        return None, "板块成分未覆盖到个股主力资金流"

    total = float(valid[flow_col].sum())
    top = valid.sort_values(flow_col, ascending=False).head(3)
    leaders = "，".join(f"{row['名称']} {money_yi(row[flow_col])}" for _, row in top.iterrows())
    return total, f"成分股主力净流入合计 {money_yi(total)}（覆盖 {len(valid)}/{len(board_spot)} 只）；流入靠前: {leaders}"


def board_red_green_score(board_spot: pd.DataFrame) -> tuple[bool, str]:
    if board_sample_too_small(board_spot):
        return False, sample_size_evidence(board_spot)
    if "涨跌幅" not in board_spot.columns:
        return False, "板块成分缺少涨跌幅字段"

    valid = board_spot.dropna(subset=["涨跌幅"])
    red = int((valid["涨跌幅"] > 0).sum())
    green = int((valid["涨跌幅"] < 0).sum())
    flat = int((valid["涨跌幅"] == 0).sum())
    hit = red > green
    return hit, f"板块红盘 {red} 只，绿盘 {green} 只，平盘 {flat} 只"


def board_top_turnover_red_score(board_spot: pd.DataFrame, limit: int = 5, min_red: int = 3) -> tuple[bool, str]:
    if board_sample_too_small(board_spot):
        return False, sample_size_evidence(board_spot)
    required = {"成交额", "涨跌幅"}
    if not required.issubset(board_spot.columns):
        return False, "板块成分缺少成交额/涨跌幅字段"

    sample = board_spot.dropna(subset=["成交额", "涨跌幅"]).sort_values("成交额", ascending=False).head(limit)
    if len(sample) < limit:
        return False, f"板块高成交样本不足 {limit} 只"

    red = int((sample["涨跌幅"] > 0).sum())
    names = "、".join(str(name) for name in sample.loc[sample["涨跌幅"] > 0, "名称"].head(3))
    hit = red >= min_red
    suffix = f"；红盘代表: {names}" if names else ""
    return hit, f"板块成交额前{limit}中红盘 {red} 只（阈值 {min_red} 只）{suffix}"


def board_median_change_score(board_spot: pd.DataFrame, threshold: float = 0) -> tuple[bool, str]:
    if board_sample_too_small(board_spot):
        return False, sample_size_evidence(board_spot)
    if "涨跌幅" not in board_spot.columns:
        return False, "板块成分缺少涨跌幅字段"

    valid = board_spot.dropna(subset=["涨跌幅"])
    if valid.empty:
        return False, "板块成分涨跌幅为空"

    median = safe_float(valid["涨跌幅"].median())
    hit = median is not None and median > threshold
    return hit, f"板块涨幅中位数 {pct(median)}（阈值 {pct(threshold)}，有效样本 {len(valid)} 只）"


def fund_flow_breadth_score(board_spot: pd.DataFrame, fund: pd.DataFrame) -> tuple[bool, str]:
    if board_sample_too_small(board_spot):
        return False, sample_size_evidence(board_spot)
    if board_spot.empty or fund.empty or "代码" not in fund.columns:
        return False, "未取到可统计的成分股主力资金流"

    flow_col = find_main_flow_column(fund)
    if not flow_col:
        return False, "个股资金流字段缺失"

    merged = board_spot[["代码", "名称"]].merge(fund[["代码", flow_col]], on="代码", how="left")
    merged[flow_col] = to_num(merged[flow_col])
    valid = merged.dropna(subset=[flow_col])
    min_coverage = max(5, min(MIN_BOARD_SAMPLE_SIZE, math.ceil(len(board_spot) * 0.3)))
    if len(valid) < min_coverage:
        return False, f"资金流覆盖不足，仅覆盖 {len(valid)}/{len(board_spot)} 只（最低 {min_coverage} 只）"

    inflow = int((valid[flow_col] > 0).sum())
    outflow = int((valid[flow_col] < 0).sum())
    flat = int((valid[flow_col] == 0).sum())
    hit = inflow > outflow
    return hit, f"主力净流入 {inflow} 只，净流出 {outflow} 只，持平 {flat} 只（覆盖 {len(valid)}/{len(board_spot)} 只）"


def high_open_score(board_spot: pd.DataFrame, threshold: float) -> tuple[bool, str, float]:
    if board_sample_too_small(board_spot):
        return False, sample_size_evidence(board_spot), 0.0

    required = {"今开", "昨收"}
    if board_spot.empty or not required.issubset(board_spot.columns):
        return False, "未取到板块成分开盘价/昨收", 0.0
    valid = board_spot.dropna(subset=["今开", "昨收"])
    if valid.empty:
        return False, "板块成分开盘价/昨收为空", 0.0
    ratio = float((valid["今开"] > valid["昨收"]).mean())
    hit = ratio >= threshold
    evidence = f"成分股高开占比 {ratio:.0%}（有效样本 {len(valid)} 只，阈值 {threshold:.0%}）"
    return hit, evidence, ratio


def fetch_minutes(code: str, trade_date: datetime) -> pd.DataFrame:
    start = trade_date.strftime("%Y-%m-%d 09:30:00")
    end = trade_date.strftime("%Y-%m-%d 15:00:00")
    df = ak.stock_zh_a_hist_min_em(
        symbol=code,
        period="1",
        start_date=start,
        end_date=end,
        adjust="",
    )
    for col in ["开盘", "收盘", "最高", "最低"]:
        if col in df.columns:
            df[col] = to_num(df[col])
    return df


def detect_pullback_recovery(row: pd.Series, trade_date: datetime) -> tuple[bool, str]:
    code = str(row["代码"]).zfill(6)
    name = str(row.get("名称", code))
    open_price = safe_float(row.get("今开"))
    prev_close = safe_float(row.get("昨收"))
    if open_price is None or prev_close is None or open_price <= prev_close:
        return False, ""

    try:
        minute = fetch_minutes(code, trade_date)
    except Exception:
        return False, ""

    if minute.empty or "收盘" not in minute.columns:
        return False, ""

    time_col = first_existing_column(minute, ["时间", "日期"])
    if time_col:
        minute[time_col] = pd.to_datetime(minute[time_col], errors="coerce")
        first_10 = minute[minute[time_col].dt.time <= datetime.strptime("09:40", "%H:%M").time()]
    else:
        first_10 = minute.head(10)

    if first_10.empty:
        return False, ""

    low_col = "最低" if "最低" in minute.columns else "收盘"
    first_low = safe_float(first_10[low_col].min())
    latest = safe_float(minute["收盘"].dropna().iloc[-1]) if not minute["收盘"].dropna().empty else None
    if first_low is None or latest is None:
        return False, ""

    pulled_back = first_low < open_price
    recovered = latest > open_price and latest > first_low * 1.005
    if pulled_back and recovered:
        return True, f"{name}({code}) 高开后10分钟内回落至 {first_low:.2f}，现价回到 {latest:.2f}"
    return False, ""


def pullback_recovery_score(board_spot: pd.DataFrame, trade_date: datetime, sample_size: int) -> tuple[bool, str]:
    if board_sample_too_small(board_spot):
        return False, sample_size_evidence(board_spot)

    if board_spot.empty:
        return False, "未取到板块成分股"

    sample = board_spot.sort_values("成交额", ascending=False).head(sample_size)
    evidences: list[str] = []
    for _, row in sample.iterrows():
        hit, evidence = detect_pullback_recovery(row, trade_date)
        if hit and evidence:
            evidences.append(evidence)
        time.sleep(0.15)

    if evidences:
        return True, "；".join(evidences[:3])
    return False, f"抽样 {len(sample)} 只高成交成分股，暂未识别到高开低走后回流"


def fetch_daily(code: str, end_date: datetime) -> pd.DataFrame:
    start_date = (end_date - timedelta(days=140)).strftime("%Y%m%d")
    end = end_date.strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_date,
        end_date=end,
        adjust="",
    )
    if "日期" in df.columns:
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    for col in ["最高", "收盘"]:
        if col in df.columns:
            df[col] = to_num(df[col])
    return df


def is_new_high(row: pd.Series, trade_date: datetime, window: int) -> tuple[bool, str]:
    code = str(row["代码"]).zfill(6)
    name = str(row.get("名称", code))
    latest_high = safe_float(row.get("最高")) or safe_float(row.get("最新价"))
    if latest_high is None:
        return False, ""
    try:
        daily = fetch_daily(code, trade_date)
    except Exception:
        return False, ""

    if daily.empty or "最高" not in daily.columns:
        return False, ""

    if "日期" in daily.columns:
        daily = daily[daily["日期"].dt.date < trade_date.date()]
    daily = daily.dropna(subset=["最高"]).tail(window)
    if daily.empty:
        return False, ""

    previous_high = safe_float(daily["最高"].max())
    if previous_high is not None and latest_high >= previous_high:
        return True, f"{name}({code}) 盘中最高 {latest_high:.2f} >= 近{window}日高点 {previous_high:.2f}"
    return False, ""


def new_high_score(board_spot: pd.DataFrame, trade_date: datetime, sample_size: int, window: int) -> tuple[bool, str]:
    if board_sample_too_small(board_spot):
        return False, sample_size_evidence(board_spot)

    if board_spot.empty:
        return False, "未取到板块成分股"

    sample = board_spot.sort_values("成交额", ascending=False).head(sample_size)
    evidences: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(is_new_high, row, trade_date, window) for _, row in sample.iterrows()]
        for future in concurrent.futures.as_completed(futures):
            hit, evidence = future.result()
            if hit and evidence:
                evidences.append(evidence)

    if evidences:
        return True, "；".join(evidences[:5])
    return False, f"抽样 {len(sample)} 只高成交成分股，未发现近{window}日新高"


def format_table(df: pd.DataFrame, cols: list[str], max_rows: int = 20) -> str:
    available = [col for col in cols if col in df.columns]
    if not available:
        return "无可展示字段"
    view = df[available].head(max_rows).copy()
    for col in view.columns:
        if col in ["成交额"] or "净流入" in col:
            view[col] = view[col].map(money_yi)
        elif col in ["涨跌幅", "5分钟涨跌"]:
            view[col] = view[col].map(pct)
        elif col in ["最新价"]:
            view[col] = view[col].map(lambda x: "-" if safe_float(x) is None else f"{float(x):.2f}")
    return view.to_markdown(index=False)


def records_for_web(df: pd.DataFrame, cols: list[str], max_rows: int = 20) -> list[dict[str, Any]]:
    available = [col for col in cols if col in df.columns]
    if not available:
        return []

    records: list[dict[str, Any]] = []
    for _, row in df[available].head(max_rows).iterrows():
        item: dict[str, Any] = {}
        for col in available:
            value = row[col]
            if pd.isna(value):
                raw_value: Any = None
            elif hasattr(value, "item"):
                raw_value = value.item()
            else:
                raw_value = value

            item[col] = raw_value
            if "净流入" in col:
                item[f"{col}_display"] = "未覆盖" if raw_value is None else money_yi(raw_value)
            elif col == "成交额":
                item[f"{col}_display"] = money_yi(raw_value)
            elif col == "涨跌幅":
                item[f"{col}_display"] = pct(raw_value)
            elif col in ["最新价", "今开", "昨收", "最高", "最低"]:
                number = safe_float(raw_value)
                item[f"{col}_display"] = "-" if number is None else f"{number:.2f}"
        records.append(item)
    return records


def market_derived_tables(spot: pd.DataFrame, fund: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    merged = merge_spot_with_funds(spot, fund)
    flow_col = find_main_flow_column(merged)
    cols = ["代码", "名称", "今开", "最新价", "涨跌幅", "成交额"]
    if flow_col:
        cols.append(flow_col)
    if "资金流来源" in merged.columns:
        cols.append("资金流来源")

    if "成交额" in merged.columns:
        merged = merged.copy()
        merged["成交额"] = to_num(merged["成交额"])
    if "涨跌幅" in merged.columns:
        merged = merged.copy()
        merged["涨跌幅"] = to_num(merged["涨跌幅"])
    if flow_col:
        merged = merged.copy()
        merged[flow_col] = to_num(merged[flow_col])
        flow_ready = merged.dropna(subset=[flow_col])
    else:
        flow_ready = merged.iloc[0:0].copy()

    amount_ready = merged.dropna(subset=["成交额"]) if "成交额" in merged.columns else merged.iloc[0:0].copy()
    amount_50y = 5_000_000_000

    inflow = flow_ready[flow_ready[flow_col] > 0].sort_values(flow_col, ascending=False) if flow_col else flow_ready
    outflow = flow_ready[flow_ready[flow_col] < 0].sort_values(flow_col, ascending=True) if flow_col else flow_ready
    inflow_big_turnover = (
        inflow[inflow["成交额"] > amount_50y].sort_values(flow_col, ascending=False)
        if flow_col and "成交额" in inflow.columns
        else inflow.iloc[0:0]
    )
    big_turnover_drop = (
        amount_ready[(amount_ready["成交额"] > amount_50y) & (amount_ready["涨跌幅"] < -5)].sort_values("成交额", ascending=False)
        if "涨跌幅" in amount_ready.columns
        else amount_ready.iloc[0:0]
    )

    return {
        "fund_in_top15": records_for_web(inflow, cols, 15),
        "fund_out_top15": records_for_web(outflow, cols, 15),
        "fund_in_big_turnover_top10": records_for_web(inflow_big_turnover, cols, 10),
        "fund_out_top10": records_for_web(outflow, cols, 10),
        "big_turnover_drop5": records_for_web(big_turnover_drop, cols, 30),
    }


def score_signal(score_10: float, signal_config: dict[str, Any] | None = None) -> str:
    signal_config = signal_config or DEFAULT_SCORING_CONFIG["signals"]
    bullish_above = safe_float(signal_config.get("bullishAbove")) or 6
    neutral_min = safe_float(signal_config.get("neutralMin")) or 4
    neutral_max = safe_float(signal_config.get("neutralMax")) or 6
    if score_10 > bullish_above:
        return str(signal_config.get("bullishText") or DEFAULT_SCORING_CONFIG["signals"]["bullishText"])
    if neutral_min <= score_10 <= neutral_max:
        return str(signal_config.get("neutralText") or DEFAULT_SCORING_CONFIG["signals"]["neutralText"])
    return str(signal_config.get("weakText") or DEFAULT_SCORING_CONFIG["signals"]["weakText"])


def theme_score_signal(score_10: float, config: dict[str, Any]) -> str:
    theme_config = config.get("boardThemeScoring") if isinstance(config.get("boardThemeScoring"), dict) else {}
    signal_config = theme_config.get("signals") if isinstance(theme_config.get("signals"), dict) else {}
    return score_signal(score_10, signal_config or DEFAULT_SCORING_CONFIG["boardThemeScoring"]["signals"])


def fund_coverage(df: pd.DataFrame) -> tuple[int, int]:
    flow_col = find_main_flow_column(df)
    if not flow_col:
        return 0, len(df)
    return int(to_num(df[flow_col]).notna().sum()), len(df)


def fund_breadth_from_table(df: pd.DataFrame) -> tuple[bool, str]:
    flow_col = find_main_flow_column(df)
    if not flow_col:
        return False, "资金流字段缺失"
    valid = df.dropna(subset=[flow_col]).copy()
    if valid.empty:
        return False, "资金流无有效覆盖"
    valid[flow_col] = to_num(valid[flow_col])
    inflow = int((valid[flow_col] > 0).sum())
    outflow = int((valid[flow_col] < 0).sum())
    flat = int((valid[flow_col] == 0).sum())
    return inflow > outflow, f"主力净流入 {inflow} 只，净流出 {outflow} 只，持平 {flat} 只（覆盖 {len(valid)}/{len(df)} 只）"


def top_turnover_red_score(df: pd.DataFrame, limit: int, min_red: int) -> tuple[bool, str]:
    if df.empty or "涨跌幅" not in df.columns:
        return False, "缺少涨跌幅字段"
    sample = df.head(limit)
    red = int((sample["涨跌幅"] > 0).sum())
    names = "、".join(str(name) for name in sample.loc[sample["涨跌幅"] > 0, "名称"].head(3))
    suffix = f"；红盘代表: {names}" if names else ""
    return red >= min_red, f"成交额前{limit}中红盘 {red} 只（阈值 {min_red} 只）{suffix}"


def load_local_indices(args: argparse.Namespace) -> list[dict[str, Any]]:
    provided = getattr(args, "indices", None)
    if isinstance(provided, list):
        return [item for item in provided if isinstance(item, dict)]
    path = Path(os.environ.get("RAW_INDICES_FILE", r"D:\Agent_Prooogram\QQGG\data\raw_indices.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        diff = data.get("diff")
        if isinstance(diff, list):
            return [item for item in diff if isinstance(item, dict)]
        inner = data.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("diff"), list):
            return [item for item in inner["diff"] if isinstance(item, dict)]
    return []


def market_index_resonance_ratio(indices: list[dict[str, Any]]) -> tuple[float, str]:
    if not indices:
        return 0.5, "未读取到指数快照，按中性处理"
    rows: list[tuple[str, float]] = []
    index_names = {
        "000001": "上证指数",
        "399001": "深证成指",
        "399006": "创业板指",
        "000688": "科创50",
        "000300": "沪深300",
        "000905": "中证500",
        "899050": "北证50",
    }
    for item in indices:
        code = str(item.get("f12") or item.get("code") or item.get("代码") or "")
        name = index_names.get(code) or str(item.get("f14") or item.get("name") or item.get("名称") or code)
        change = safe_float(item.get("f3") or item.get("change_pct") or item.get("涨跌幅"))
        if change is not None:
            rows.append((name, change))
    if not rows:
        return 0.5, "指数快照缺少涨跌幅，按中性处理"
    red = sum(1 for _, change in rows if change > 0)
    green = sum(1 for _, change in rows if change < 0)
    avg = sum(change for _, change in rows) / len(rows)
    leaders = "、".join(f"{name}{change:.2f}%" for name, change in sorted(rows, key=lambda x: x[1], reverse=True)[:3])
    if red >= max(3, math.ceil(len(rows) * 0.6)) and avg > 0:
        ratio = 1.0
    elif red >= green and avg >= -0.2:
        ratio = 0.5
    else:
        ratio = 0.0
    return ratio, f"指数红 {red} 个、绿 {green} 个，平均 {avg:.2f}%；强势: {leaders}"


def market_breadth_ratio(spot: pd.DataFrame) -> tuple[float, str]:
    if spot.empty or "涨跌幅" not in spot.columns:
        return 0.0, "缺少全市场涨跌幅"
    changes = to_num(spot["涨跌幅"]).dropna()
    if changes.empty:
        return 0.0, "全市场涨跌幅无有效数据"
    red = int((changes > 0).sum())
    green = int((changes < 0).sum())
    flat = int((changes == 0).sum())
    up5 = int((changes >= 5).sum())
    down5 = int((changes <= -5).sum())
    limit_up = int((changes >= 9.8).sum())
    limit_down = int((changes <= -9.8).sum())
    red_ratio = red / len(changes)
    if red_ratio >= 0.55 and up5 >= down5:
        ratio = 1.0
    elif red_ratio >= 0.48 and up5 >= down5 * 0.7:
        ratio = 0.5
    else:
        ratio = 0.0
    return ratio, (
        f"红盘 {red} 只、绿盘 {green} 只、平盘 {flat} 只，红盘占比 {red_ratio:.1%}；"
        f"涨超5% {up5} 只、跌超5% {down5} 只，涨停约 {limit_up} 只、跌停约 {limit_down} 只"
    )


def amount_weighted_change(df: pd.DataFrame) -> float | None:
    if df.empty or "涨跌幅" not in df.columns or "成交额" not in df.columns:
        return None
    view = df[["涨跌幅", "成交额"]].copy()
    view["涨跌幅"] = to_num(view["涨跌幅"])
    view["成交额"] = to_num(view["成交额"])
    view = view.dropna()
    total = float(view["成交额"].sum())
    if total <= 0:
        return None
    return float((view["涨跌幅"] * view["成交额"]).sum() / total)


def market_turnover_core_ratio(top20: pd.DataFrame, top50: pd.DataFrame) -> tuple[float, str]:
    if top20.empty or "涨跌幅" not in top20.columns:
        return 0.0, "缺少成交核心涨跌幅"
    top20_change = to_num(top20["涨跌幅"]).dropna()
    top50_change = to_num(top50["涨跌幅"]).dropna() if not top50.empty and "涨跌幅" in top50.columns else top20_change
    top20_red_ratio = float((top20_change > 0).sum() / len(top20_change)) if len(top20_change) else 0.0
    top50_red_ratio = float((top50_change > 0).sum() / len(top50_change)) if len(top50_change) else 0.0
    weighted = amount_weighted_change(top20)
    deep_down = int((top20_change <= -5).sum())
    if top20_red_ratio >= 0.55 and (weighted or 0) > 0 and deep_down <= 3:
        ratio = 1.0
    elif top20_red_ratio >= 0.45 or (weighted is not None and weighted > 0):
        ratio = 0.5
    else:
        ratio = 0.0
    weighted_text = "-" if weighted is None else f"{weighted:.2f}%"
    return ratio, f"前20红盘占比 {top20_red_ratio:.1%}，前50红盘占比 {top50_red_ratio:.1%}，前20成交额加权涨跌 {weighted_text}，前20跌超5% {deep_down} 只"


def market_fund_diffusion_ratio(top20: pd.DataFrame) -> tuple[float, str]:
    flow_col = find_main_flow_column(top20)
    if not flow_col:
        return 0.5, "成交核心资金流未覆盖，按中性处理"
    valid = top20.dropna(subset=[flow_col]).copy()
    if valid.empty:
        return 0.5, "成交核心资金流无有效覆盖，按中性处理"
    valid[flow_col] = to_num(valid[flow_col])
    inflow = int((valid[flow_col] > 0).sum())
    outflow = int((valid[flow_col] < 0).sum())
    net_sum = float(valid[flow_col].sum())
    positive_sum = float(valid.loc[valid[flow_col] > 0, flow_col].sum())
    max_inflow = float(valid.loc[valid[flow_col] > 0, flow_col].max() or 0)
    concentration = max_inflow / positive_sum if positive_sum > 0 else 1.0
    if inflow > outflow and net_sum > 0 and concentration <= 0.7:
        ratio = 1.0
    elif inflow >= outflow or net_sum > 0:
        ratio = 0.5
    else:
        ratio = 0.0
    return ratio, (
        f"前20主力净流入 {inflow} 只、净流出 {outflow} 只，合计 {money_yi(net_sum)}，"
        f"最大流入集中度 {concentration:.0%}（覆盖 {len(valid)}/{len(top20)} 只）"
    )


def market_risk_control_ratio(spot: pd.DataFrame, top20: pd.DataFrame) -> tuple[float, str]:
    if spot.empty or "涨跌幅" not in spot.columns:
        return 0.0, "缺少风险扩散数据"
    changes = to_num(spot["涨跌幅"]).dropna()
    top_changes = to_num(top20["涨跌幅"]).dropna() if "涨跌幅" in top20.columns else pd.Series(dtype=float)
    up5 = int((changes >= 5).sum())
    down5 = int((changes <= -5).sum())
    limit_up = int((changes >= 9.8).sum())
    limit_down = int((changes <= -9.8).sum())
    core_down = int((top_changes <= -5).sum())
    core_green = int((top_changes < 0).sum())
    if down5 <= up5 and limit_down <= max(limit_up, 1) and core_down <= 2:
        ratio = 1.0
    elif down5 <= up5 * 1.5 and core_down <= 4 and core_green <= len(top_changes) * 0.65:
        ratio = 0.5
    else:
        ratio = 0.0
    return ratio, f"跌超5% {down5} 只、涨超5% {up5} 只，跌停约 {limit_down} 只、涨停约 {limit_up} 只，成交前20跌超5% {core_down} 只"


def board_theme_config(config: dict[str, Any]) -> dict[str, Any]:
    theme_config = config.get("boardThemeScoring") if isinstance(config.get("boardThemeScoring"), dict) else {}
    return deep_merge_dict(DEFAULT_SCORING_CONFIG["boardThemeScoring"], theme_config)


def board_theme_enabled(config: dict[str, Any]) -> bool:
    return board_theme_config(config).get("enabled", True) is not False


def normalize_theme_groups(config: dict[str, Any]) -> list[dict[str, Any]]:
    groups = board_theme_config(config).get("groups")
    if not isinstance(groups, list):
        groups = DEFAULT_SCORING_CONFIG["boardThemeScoring"]["groups"]
    normalized: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        stocks = group.get("stocks")
        if not isinstance(stocks, list):
            continue
        stock_items: list[dict[str, str]] = []
        for stock in stocks:
            if isinstance(stock, dict):
                code = normalize_code(stock.get("code") or stock.get("代码") or "")
                name = str(stock.get("name") or stock.get("名称") or code).strip()
            else:
                code = ""
                name = str(stock).strip()
            if code or name:
                stock_items.append({"code": code, "name": name})
        if stock_items:
            normalized.append(
                {
                    "name": str(group.get("name") or group.get("theme") or "自定义组合").strip(),
                    "theme": str(group.get("theme") or group.get("name") or "自定义组合").strip(),
                    "stocks": stock_items,
                }
            )
    return normalized


def index_change_value(item: dict[str, Any]) -> float | None:
    for key in ["f3", "change_pct", "涨跌幅", "changePercent", "pct_chg"]:
        value = safe_float(item.get(key))
        if value is not None:
            return value
    return None


def first_number(*values: Any) -> float | None:
    for value in values:
        number = safe_float(value)
        if number is not None:
            return number
    return None


def yahoo_daily_points(result: dict[str, Any]) -> list[dict[str, Any]]:
    timestamps = result.get("timestamp") if isinstance(result.get("timestamp"), list) else []
    quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
    if not isinstance(quote, dict):
        return []
    closes = quote.get("close") if isinstance(quote.get("close"), list) else []
    lows = quote.get("low") if isinstance(quote.get("low"), list) else []
    points: list[dict[str, Any]] = []
    for index, close_value in enumerate(closes):
        close = safe_float(close_value)
        if close is None:
            continue
        low = safe_float(lows[index]) if index < len(lows) else None
        timestamp = timestamps[index] if index < len(timestamps) else None
        points.append({"timestamp": timestamp, "close": close, "low": low})
    return points


def fetch_ks11_from_yahoo() -> tuple[dict[str, Any] | None, str | None]:
    ttl = max(30.0, safe_float(os.environ.get("KS11_YAHOO_CACHE_SECONDS")) or 900.0)
    current = time.time()
    cached_item = _KS11_YAHOO_CACHE.get("item")
    if cached_item and current - float(_KS11_YAHOO_CACHE.get("loaded_at") or 0) < ttl:
        return cached_item, _KS11_YAHOO_CACHE.get("error")

    try:
        response = requests.get(
            KS11_YAHOO_URL,
            timeout=float(os.environ.get("KS11_YAHOO_TIMEOUT_SECONDS", "8")),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        payload = response.json()
        result = (((payload.get("chart") or {}).get("result") or [None])[0]) or {}
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
        points = yahoo_daily_points(result)
        latest_point = points[-1] if points else {}
        previous_point = points[-2] if len(points) >= 2 else {}
        price = first_number(meta.get("regularMarketPrice"), latest_point.get("close"))
        # With range=1mo, Yahoo's chartPreviousClose is the close before the range,
        # not the previous trading day's close. Derive the prior daily close first.
        meta_previous_close = first_number(meta.get("regularMarketPreviousClose"), meta.get("previousClose"))
        derived_previous_close = first_number(previous_point.get("close"))
        if meta_previous_close is not None:
            prev_close = meta_previous_close
            previous_close_source = "meta_previous_close"
        elif derived_previous_close is not None:
            prev_close = derived_previous_close
            previous_close_source = "daily_quote_previous_close"
        else:
            prev_close = first_number(meta.get("chartPreviousClose"))
            previous_close_source = "meta_chart_previous_close"
        day_low = first_number(meta.get("regularMarketDayLow"), latest_point.get("low"))
        change = ((price / prev_close - 1) * 100) if price is not None and prev_close else None
        day_low_change = ((day_low / prev_close - 1) * 100) if day_low is not None and prev_close else None
        item = {
            "code": "KS11",
            "name": str(meta.get("longName") or meta.get("shortName") or "韩国综合指数"),
            "source": "Yahoo Finance",
            "regularMarketPrice": price,
            "previousClose": prev_close,
            "dayLow": day_low,
            "change_pct": change,
            "day_low_change_pct": day_low_change,
            "regularMarketTime": meta.get("regularMarketTime"),
            "previousCloseSource": previous_close_source,
        }
        _KS11_YAHOO_CACHE.update({"loaded_at": current, "item": item, "error": None})
        return item, None
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _KS11_YAHOO_CACHE.update({"loaded_at": current, "item": cached_item, "error": error})
        return cached_item if isinstance(cached_item, dict) else None, error


def yahoo_time_display(value: Any) -> str:
    timestamp = safe_float(value)
    if timestamp is None:
        return "-"
    try:
        tz = ZoneInfo("Asia/Shanghai") if ZoneInfo is not None else None
        return datetime.fromtimestamp(timestamp, tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "-"


def ks11_status(indices: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    theme_config = board_theme_config(config)
    precondition = theme_config.get("precondition") if isinstance(theme_config.get("precondition"), dict) else {}
    code = str(precondition.get("indexCode") or "KS11").upper()
    name = str(precondition.get("indexName") or "韩国综合指数")
    raw_threshold = safe_float(precondition.get("limitDownPct"))
    threshold = -abs(raw_threshold if raw_threshold is not None else 8.0)

    matched: dict[str, Any] | None = None
    yahoo_error: str | None = None
    if code == "KS11":
        matched, yahoo_error = fetch_ks11_from_yahoo()
    if matched is None:
        for item in indices:
            item_code = str(item.get("f12") or item.get("code") or item.get("代码") or "").upper()
            item_name = str(item.get("f14") or item.get("name") or item.get("名称") or "")
            if item_code == code or code in item_code or name in item_name or "KOSPI" in item_name.upper():
                matched = item
                break

    if matched is None:
        suffix = f"；Yahoo错误: {yahoo_error}" if yahoo_error else ""
        evidence = f"未读取到{name}（{code}），前置条件本轮不触发{suffix}"
        return {
            "code": code,
            "name": name,
            "source": "Yahoo Finance" if code == "KS11" else "本地指数快照",
            "available": False,
            "triggered": False,
            "threshold_pct": threshold,
            "threshold_pct_display": pct(threshold),
            "status": "未读取",
            "evidence": evidence,
            "error": yahoo_error,
        }

    change = index_change_value(matched)
    day_low_change = safe_float(matched.get("day_low_change_pct"))
    price = safe_float(matched.get("regularMarketPrice") or matched.get("最新价") or matched.get("f2"))
    prev_close = safe_float(matched.get("previousClose") or matched.get("昨收") or matched.get("f18"))
    day_low = safe_float(matched.get("dayLow") or matched.get("regularMarketDayLow") or matched.get("最低") or matched.get("f16"))
    if day_low_change is None and day_low is not None and prev_close:
        day_low_change = (day_low / prev_close - 1) * 100
    limit_flag = bool(matched.get("limit_down") or matched.get("跌停") or matched.get("is_limit_down"))
    triggered = bool(limit_flag or (change is not None and change <= threshold) or (day_low_change is not None and day_low_change <= threshold))

    change_text = "-" if change is None else pct(change)
    low_text = "-" if day_low_change is None else pct(day_low_change)
    source = str(matched.get("source") or "本地指数快照")
    if triggered:
        evidence = f"{name}（{code}）涨跌幅 {change_text}，日内低点 {low_text}，触及跌停/阈值 {threshold:.2f}%（{source}），科技股日内全线看空"
        status = "触发看空前置"
    else:
        evidence = f"{name}（{code}）涨跌幅 {change_text}，日内低点 {low_text}，未触发跌停前置条件（阈值 {threshold:.2f}%，{source}）"
        status = "未触发"

    return {
        "code": code,
        "name": name,
        "source": source,
        "available": True,
        "triggered": triggered,
        "status": status,
        "price": price,
        "price_display": "-" if price is None else f"{price:.2f}",
        "previous_close": prev_close,
        "previous_close_display": "-" if prev_close is None else f"{prev_close:.2f}",
        "day_low": day_low,
        "day_low_display": "-" if day_low is None else f"{day_low:.2f}",
        "change_pct": change,
        "change_pct_display": change_text,
        "day_low_change_pct": day_low_change,
        "day_low_change_pct_display": low_text,
        "threshold_pct": threshold,
        "threshold_pct_display": pct(threshold),
        "updated_at": yahoo_time_display(matched.get("regularMarketTime")),
        "evidence": evidence,
        "error": yahoo_error,
    }


def korean_limit_down_precondition(indices: list[dict[str, Any]], config: dict[str, Any]) -> tuple[bool, str]:
    status = ks11_status(indices, config)
    return bool(status.get("triggered")), str(status.get("evidence") or "")


def theme_stock_table(spot: pd.DataFrame, fund: pd.DataFrame, groups: list[dict[str, Any]]) -> pd.DataFrame:
    if spot.empty or "代码" not in spot.columns:
        return pd.DataFrame()

    spot_by_code = {normalize_code(row.get("代码")): row for _, row in spot.iterrows()}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for group_index, group in enumerate(groups):
        group_name = str(group.get("name") or "自定义组合")
        for stock_index, stock in enumerate(group.get("stocks") or []):
            code = normalize_code(stock.get("code"))
            if not code or code not in spot_by_code:
                continue
            key = (group_name, code)
            if key in seen:
                continue
            seen.add(key)
            item = spot_by_code[code].to_dict()
            item["代码"] = code
            item["名称"] = item.get("名称") or stock.get("name") or code
            item["主题组合"] = group_name
            item["来源"] = "板块组合"
            item["_theme_order"] = group_index * 100 + stock_index
            rows.append(item)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = merge_spot_with_funds(df, fund)
    return df.sort_values("_theme_order")


def theme_group_rows(theme_df: pd.DataFrame, group_name: str) -> pd.DataFrame:
    if theme_df.empty or "主题组合" not in theme_df.columns:
        return pd.DataFrame()
    return theme_df.loc[theme_df["主题组合"] == group_name].copy()


def theme_missing_names(group: dict[str, Any], rows: pd.DataFrame) -> list[str]:
    present = set(rows["代码"].map(normalize_code)) if not rows.empty and "代码" in rows.columns else set()
    missing: list[str] = []
    for stock in group.get("stocks") or []:
        code = normalize_code(stock.get("code"))
        if code and code not in present:
            missing.append(str(stock.get("name") or code))
    return missing


def theme_condition_points(raw_delta: int) -> float:
    return float(raw_delta)


def theme_red_score_item(group: dict[str, Any], rows: pd.DataFrame) -> ScoreItem:
    group_name = str(group.get("name") or "自定义组合")
    theme = str(group.get("theme") or group_name)
    expected = len(group.get("stocks") or [])
    if rows.empty or "涨跌幅" not in rows.columns:
        return ScoreItem(f"{group_name}红盘数量", 2, 0, False, f"{theme}核心票未读取到涨跌幅，按中性处理")

    valid = rows.dropna(subset=["涨跌幅"]).copy()
    valid["涨跌幅"] = to_num(valid["涨跌幅"])
    red_rows = valid.loc[valid["涨跌幅"] > 0]
    green_rows = valid.loc[valid["涨跌幅"] < 0]
    red = len(red_rows)
    green = len(green_rows)
    missing = theme_missing_names(group, rows)
    if red >= 2:
        raw_delta = 2
        verdict = f"{theme}资金积极做多"
    elif red == 1:
        raw_delta = 1
        verdict = f"{theme}资金冲高减仓"
    elif len(valid) >= expected and red == 0:
        raw_delta = -2
        verdict = f"{theme}资金今天承压，延迟买入"
    else:
        raw_delta = 0
        verdict = f"{theme}红绿覆盖不足，暂不加减分"
    points = theme_condition_points(raw_delta)
    names = "、".join(str(name) for name in red_rows["名称"].head(3))
    green_names = "、".join(str(name) for name in green_rows["名称"].head(3))
    missing_text = f"；未读取: {'、'.join(missing)}" if missing else ""
    evidence = f"{group_name}红盘 {red} 只、绿盘 {green} 只；红盘: {names or '-'}；绿盘: {green_names or '-'}；{verdict}{missing_text}"
    return ScoreItem(f"{group_name}红盘数量", 2, points, raw_delta > 0, evidence)


def theme_fund_score_item(group: dict[str, Any], rows: pd.DataFrame) -> ScoreItem:
    group_name = str(group.get("name") or "自定义组合")
    theme = str(group.get("theme") or group_name)
    if rows.empty:
        return ScoreItem(f"{group_name}资金流", 2, 0, False, f"{theme}核心票未读取到行情，按中性处理")
    flow_col = find_main_flow_column(rows)
    if not flow_col:
        return ScoreItem(f"{group_name}资金流", 2, 0, False, f"{theme}核心票主力资金流未覆盖，按中性处理")

    valid = rows.dropna(subset=[flow_col]).copy()
    valid[flow_col] = to_num(valid[flow_col])
    inflow_rows = valid.loc[valid[flow_col] > 0]
    outflow_rows = valid.loc[valid[flow_col] < 0]
    inflow = len(inflow_rows)
    outflow = len(outflow_rows)
    if inflow >= 2:
        raw_delta = 2
        verdict = f"{theme}资金积极做多"
    elif inflow == 1:
        raw_delta = 1
        verdict = f"{theme}资金冲高减仓"
    elif len(valid) >= len(group.get("stocks") or []) and inflow == 0:
        raw_delta = -2
        verdict = f"{theme}资金今天承压，延迟买入"
    else:
        raw_delta = 0
        verdict = f"{theme}资金覆盖不足，暂不按全流出扣分"
    points = theme_condition_points(raw_delta)
    leaders = "、".join(f"{row['名称']}{money_yi(row[flow_col])}" for _, row in inflow_rows.head(3).iterrows())
    laggards = "、".join(f"{row['名称']}{money_yi(row[flow_col])}" for _, row in outflow_rows.head(3).iterrows())
    evidence = f"{group_name}主力净流入 {inflow} 只、净流出 {outflow} 只（覆盖 {len(valid)}/{len(group.get('stocks') or [])}）；流入: {leaders or '-'}；流出: {laggards or '-'}；{verdict}"
    return ScoreItem(f"{group_name}资金流", 2, points, raw_delta > 0, evidence)


def market_top20_red_score_item(top20: pd.DataFrame) -> ScoreItem:
    if top20.empty or "涨跌幅" not in top20.columns:
        return ScoreItem("市场成交额前20红盘数量", 2, 0, False, "未读取到成交额前20涨跌幅，按中性处理")
    changes = to_num(top20["涨跌幅"]).dropna()
    red = int((changes > 0).sum())
    green = int((changes < 0).sum())
    if red > 10:
        points = 2
        verdict = "今天市场环境不错，积极做多日"
    elif red >= 7:
        points = 1
        verdict = "今天市场环境一般，冲高减仓日"
    else:
        points = -2
        verdict = "今天市场环境很差，冲高大幅减仓日，延迟买入点"
    return ScoreItem(
        "市场成交额前20红盘数量",
        2,
        float(points),
        points > 0,
        f"成交额前20红盘 {red} 只、绿盘 {green} 只；{verdict}",
    )


def build_combined_board_theme_score(
    spot: pd.DataFrame,
    fund: pd.DataFrame,
    indices: list[dict[str, Any]],
    top20: pd.DataFrame,
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[ScoreItem], pd.DataFrame, dict[str, dict[str, Any]]]:
    groups = normalize_theme_groups(config)
    theme_df = theme_stock_table(spot, fund, groups)
    precondition_hit, precondition_evidence = korean_limit_down_precondition(indices, config)
    if precondition_hit:
        theme_config = board_theme_config(config)
        signal = str((theme_config.get("precondition") or {}).get("signal") or DEFAULT_SCORING_CONFIG["boardThemeScoring"]["precondition"]["signal"])
        items = [ScoreItem("前置条件：韩国综合指数", 10, 0, False, precondition_evidence)]
        score = {
            "name": "科技板块综合评分",
            "theme": "科技股",
            "raw_score": 0,
            "score_max": 10,
            "score_10": 0,
            "signal": signal,
            "items": [dataclasses.asdict(item) for item in items],
            "mode": "theme_groups_combined",
        }
        return score, items, theme_df, {}

    items: list[ScoreItem] = []
    score_by_code: dict[str, dict[str, Any]] = {}
    for group in groups:
        rows = theme_group_rows(theme_df, str(group.get("name") or "自定义组合"))
        items.append(theme_red_score_item(group, rows))
        items.append(theme_fund_score_item(group, rows))
    items.append(market_top20_red_score_item(top20))

    raw = round(sum(item.points for item in items), 2)
    score_10 = round(max(-10.0, min(10.0, raw)), 2)
    score = {
        "name": "科技板块综合评分",
        "theme": "科技股",
        "raw_score": score_10,
        "score_max": 10,
        "score_10": score_10,
        "signal": theme_score_signal(score_10, config),
        "items": [dataclasses.asdict(item) for item in items],
        "mode": "theme_groups_combined",
    }
    for group in groups:
        for stock in group.get("stocks") or []:
            code = normalize_code(stock.get("code"))
            if code:
                score_by_code[code] = score
    return score, items, theme_df, score_by_code


def build_theme_group_score(
    group: dict[str, Any],
    rows: pd.DataFrame,
    precondition_hit: bool,
    precondition_evidence: str,
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[ScoreItem]]:
    group_name = str(group.get("name") or "自定义组合")
    theme = str(group.get("theme") or group_name)
    theme_config = board_theme_config(config)
    if precondition_hit:
        signal = str((theme_config.get("precondition") or {}).get("signal") or DEFAULT_SCORING_CONFIG["boardThemeScoring"]["precondition"]["signal"])
        items = [ScoreItem("前置条件：韩国综合指数", 10, 0, False, precondition_evidence)]
        return {
            "name": group_name,
            "theme": theme,
            "raw_score": 0,
            "score_max": 10,
            "score_10": 0,
            "signal": signal,
            "items": [dataclasses.asdict(item) for item in items],
            "mode": "theme_group",
        }, items

    items = [
        theme_red_score_item(group, rows),
        theme_fund_score_item(group, rows),
    ]
    raw = round(sum(item.points for item in items), 2)
    score_10 = round(max(-10.0, min(10.0, raw)), 2)
    return {
        "name": group_name,
        "theme": theme,
        "raw_score": score_10,
        "score_max": 10,
        "score_10": score_10,
        "signal": theme_score_signal(score_10, config),
        "items": [dataclasses.asdict(item) for item in items],
        "mode": "theme_group",
    }, items


def choose_legacy_board_score(board_scores: list[dict[str, Any]], board_keyword: str) -> dict[str, Any]:
    if not board_scores:
        return score_group([])
    keyword = str(board_keyword or "").strip().lower()
    for score in board_scores:
        name = str(score.get("name") or "").lower()
        theme = str(score.get("theme") or "").lower()
        if keyword and (keyword in name or keyword in theme or name in keyword or theme in keyword):
            return score
    return board_scores[0]


def build_board_theme_scores(
    spot: pd.DataFrame,
    fund: pd.DataFrame,
    indices: list[dict[str, Any]],
    config: dict[str, Any],
    board_keyword: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]], list[ScoreItem], pd.DataFrame, dict[str, dict[str, Any]]]:
    groups = normalize_theme_groups(config)
    theme_df = theme_stock_table(spot, fund, groups)
    precondition_hit, precondition_evidence = korean_limit_down_precondition(indices, config)
    board_scores: list[dict[str, Any]] = []
    all_items: list[ScoreItem] = []
    score_by_code: dict[str, dict[str, Any]] = {}
    for group in groups:
        rows = theme_group_rows(theme_df, str(group.get("name") or "自定义组合"))
        score, items = build_theme_group_score(group, rows, precondition_hit, precondition_evidence, config)
        board_scores.append(score)
        all_items.extend(items)
        for stock in group.get("stocks") or []:
            code = normalize_code(stock.get("code"))
            if code:
                score_by_code[code] = score

    legacy_score = choose_legacy_board_score(board_scores, board_keyword)
    return legacy_score, board_scores, all_items, theme_df, score_by_code


def ratio_pct(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return "-"
    return f"{number * 100:.2f}%"


def row_main_flow_value(row: dict[str, Any]) -> float | None:
    flow_col = next((key for key in row if "净流入" in str(key) and not str(key).endswith("_display")), None)
    return safe_float(row.get(flow_col)) if flow_col else None


def row_fund_ratio(row: dict[str, Any]) -> float | None:
    flow = row_main_flow_value(row)
    amount = safe_float(row.get("成交额"))
    if flow is None or amount is None or amount <= 0:
        return None
    return flow / amount


def stock_limit_model(code: str, name: Any) -> tuple[str | None, str]:
    name_text = str(name or "").strip()
    if name_text.startswith("N"):
        return None, "新股首日涨跌幅异常，暂不套用资金匹配模型"
    code = normalize_code(code)
    if code.startswith(("300", "301", "688")):
        return "20CM", ""
    if code.startswith(("000", "001", "002", "600", "601", "603", "605")):
        return "10CM", ""
    return None, "暂未识别涨跌幅制度，资金匹配仅作观察"


def stock_flow_rule(change_pct: float, model_name: str) -> dict[str, Any] | None:
    rules = STOCK_FLOW_MODELS.get(model_name) or []
    for lower, upper, required, verdict, risk, action in rules:
        if change_pct >= lower and (upper is None or change_pct < upper):
            return {
                "min": lower,
                "max": upper,
                "required_ratio": required,
                "verdict": verdict,
                "risk": risk,
                "action": action,
            }
    return None


def bc_linkage_state(avg_ratio: float) -> dict[str, Any]:
    if avg_ratio < -0.05:
        _, _, coefficient, state, verdict, action = BC_LINKAGE_STATES[0]
    elif avg_ratio < -0.02:
        _, _, coefficient, state, verdict, action = BC_LINKAGE_STATES[1]
    elif avg_ratio < -0.01:
        _, _, coefficient, state, verdict, action = BC_LINKAGE_STATES[2]
    elif avg_ratio <= 0.01:
        _, _, coefficient, state, verdict, action = BC_LINKAGE_STATES[3]
    elif avg_ratio <= 0.02:
        _, _, coefficient, state, verdict, action = BC_LINKAGE_STATES[4]
    elif avg_ratio <= 0.05:
        _, _, coefficient, state, verdict, action = BC_LINKAGE_STATES[5]
    else:
        _, _, coefficient, state, verdict, action = BC_LINKAGE_STATES[6]
    return {
        "state": state,
        "coefficient": coefficient,
        "verdict": verdict,
        "action": action,
    }


def build_theme_peer_context(board_rows: pd.DataFrame, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if board_rows.empty or "代码" not in board_rows.columns:
        return {}

    row_by_code: dict[str, dict[str, Any]] = {}
    for _, row in board_rows.iterrows():
        code = normalize_code(row.get("代码"))
        if code:
            row_by_code[code] = row.to_dict()

    contexts: dict[str, dict[str, Any]] = {}
    for group in normalize_theme_groups(config):
        group_name = str(group.get("name") or "自定义组合")
        group_rows: list[dict[str, Any]] = []
        for stock in group.get("stocks") or []:
            code = normalize_code(stock.get("code"))
            if code in row_by_code:
                group_rows.append(row_by_code[code])
        for stock in group.get("stocks") or []:
            code = normalize_code(stock.get("code"))
            if not code:
                continue
            peers = [item for item in group_rows if normalize_code(item.get("代码")) != code]
            contexts[code] = {"group_name": group_name, "peers": peers, "peer_total": max(0, len(group.get("stocks") or []) - 1)}
    return contexts


def stock_fund_match_profile(row: dict[str, Any], peer_context: dict[str, dict[str, Any]]) -> dict[str, Any]:
    code = normalize_code(row.get("代码"))
    name = row.get("名称", "-")
    change = safe_float(row.get("涨跌幅")) or 0.0
    ratio = row_fund_ratio(row)
    model_name, unsupported_reason = stock_limit_model(code, name)
    base = None
    adjusted = None
    bc_avg = None
    coefficient = 1.0
    bc_state = "无三剑客联动"
    bc_verdict = "不在固定三剑客组合内，按个股基础资金要求判断"
    bc_action = ""
    status = "未覆盖"
    conclusion = "资金数据未覆盖，暂不判断涨幅与资金是否匹配"
    score_adjustment = 0.0
    score_cap = None
    rule = None

    if not model_name:
        return {
            "资金模型": "-",
            "资金匹配": "不适用",
            "资金净流入比例": ratio,
            "资金净流入比例_display": ratio_pct(ratio),
            "基础资金要求": None,
            "基础资金要求_display": "-",
            "BC联动状态": "-",
            "BC平均资金净流入比例": None,
            "BC平均资金净流入比例_display": "-",
            "联动系数": None,
            "联动系数_display": "-",
            "修正后要求": None,
            "修正后要求_display": "-",
            "资金结论": unsupported_reason,
            "资金模型判断": unsupported_reason,
            "资金模型风险": unsupported_reason,
            "资金模型操作": "观察为主",
            "资金模型调分": score_adjustment,
            "资金模型分数上限": score_cap,
        }

    if change <= 0:
        return {
            "资金模型": model_name,
            "资金匹配": "不适用",
            "资金净流入比例": ratio,
            "资金净流入比例_display": ratio_pct(ratio),
            "基础资金要求": None,
            "基础资金要求_display": "-",
            "BC联动状态": "-",
            "BC平均资金净流入比例": None,
            "BC平均资金净流入比例_display": "-",
            "联动系数": None,
            "联动系数_display": "-",
            "修正后要求": None,
            "修正后要求_display": "-",
            "资金结论": "绿盘或平盘不套用上涨资金匹配模型",
            "资金模型判断": "非上涨状态",
            "资金模型风险": "观察是否重新转强",
            "资金模型操作": "先观察承接，不按上涨模型追买",
            "资金模型调分": 0.0,
            "资金模型分数上限": None,
        }

    rule = stock_flow_rule(change, model_name)
    if rule is None:
        return {
            "资金模型": model_name,
            "资金匹配": "未覆盖",
            "资金净流入比例": ratio,
            "资金净流入比例_display": ratio_pct(ratio),
            "基础资金要求": None,
            "基础资金要求_display": "-",
            "BC联动状态": "-",
            "BC平均资金净流入比例": None,
            "BC平均资金净流入比例_display": "-",
            "联动系数": None,
            "联动系数_display": "-",
            "修正后要求": None,
            "修正后要求_display": "-",
            "资金结论": f"{model_name} 未匹配到涨幅 {pct(change)} 对应区间",
            "资金模型判断": "区间未覆盖",
            "资金模型风险": "暂不使用资金匹配调分",
            "资金模型操作": "观察为主",
            "资金模型调分": 0.0,
            "资金模型分数上限": None,
        }

    base = float(rule["required_ratio"])
    ctx = peer_context.get(code) or {}
    peers = ctx.get("peers") if isinstance(ctx.get("peers"), list) else []
    peer_ratios = [value for value in (row_fund_ratio(peer) for peer in peers) if value is not None]
    peer_total = int(ctx.get("peer_total") or len(peers) or 0)
    if peer_total and len(peer_ratios) >= peer_total:
        bc_avg = sum(peer_ratios) / len(peer_ratios)
        bc = bc_linkage_state(bc_avg)
        coefficient = float(bc["coefficient"])
        bc_state = str(bc["state"])
        bc_verdict = str(bc["verdict"])
        bc_action = str(bc["action"])
    elif peer_total:
        bc_state = "BC资金覆盖不足"
        bc_verdict = f"B/C 资金覆盖 {len(peer_ratios)}/{peer_total}，暂按普通标准"
        bc_action = "等待下一轮资金覆盖后再判断联动"

    adjusted = base * coefficient
    if ratio is None:
        conclusion = "主力净流入或成交额未覆盖，暂不判断资金匹配"
        status = "未覆盖"
    elif ratio >= adjusted:
        status = "达标"
        conclusion = f"资金达标：净流入比例 {ratio_pct(ratio)} ≥ 修正要求 {ratio_pct(adjusted)}；{rule['verdict']}；{bc_verdict}"
        score_adjustment = 0.3
    else:
        status = "不达标"
        gap = adjusted - ratio
        severity = gap / adjusted if adjusted > 0 else 0.0
        if severity >= 0.5:
            score_adjustment = -1.0
        elif severity >= 0.25:
            score_adjustment = -0.7
        else:
            score_adjustment = -0.4
        if change >= 6:
            score_adjustment = min(score_adjustment, -0.8)
        if change >= 8:
            score_cap = 5.8
        elif change >= 4.5:
            score_cap = 6.4
        elif severity >= 0.5:
            score_cap = 6.8
        conclusion = f"资金不达标：净流入比例 {ratio_pct(ratio)} < 修正要求 {ratio_pct(adjusted)}；{rule['risk']}；{bc_action or bc_verdict}"

    return {
        "资金模型": model_name,
        "资金匹配": status,
        "资金净流入比例": ratio,
        "资金净流入比例_display": ratio_pct(ratio),
        "基础资金要求": base,
        "基础资金要求_display": ratio_pct(base),
        "BC联动状态": bc_state,
        "BC平均资金净流入比例": bc_avg,
        "BC平均资金净流入比例_display": ratio_pct(bc_avg),
        "联动系数": coefficient,
        "联动系数_display": f"{coefficient:.2f}",
        "修正后要求": adjusted,
        "修正后要求_display": ratio_pct(adjusted),
        "资金结论": conclusion,
        "资金模型判断": str(rule["verdict"]),
        "资金模型风险": str(rule["risk"]),
        "资金模型操作": str(rule["action"]),
        "资金模型调分": score_adjustment,
        "资金模型分数上限": score_cap,
    }


def append_unique_phrase(text: str, phrase: str) -> str:
    text = str(text or "").strip()
    phrase = str(phrase or "").strip()
    if not phrase:
        return text
    if not text:
        return phrase
    if phrase in text:
        return text
    return f"{text}；{phrase}"


def stock_action(score: float) -> str:
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


def stock_self_score(row: dict[str, Any], source: str, config: dict[str, Any]) -> tuple[float, str]:
    rules = config.get("stockRules") if isinstance(config.get("stockRules"), dict) else {}

    def rule_float(key: str, default: float) -> float:
        try:
            return float(rules.get(key, default))
        except (TypeError, ValueError):
            return default

    score = 0.0
    reasons: list[str] = []
    change = safe_float(row.get("涨跌幅")) or 0.0
    if change > 0:
        score += max(0.0, rule_float("redChangePoints", 2))
        reasons.append(f"红盘 {pct(change)}")
    elif change < 0:
        reasons.append(f"绿盘 {pct(change)}")

    flow_col = next((key for key in row if "净流入" in key and not str(key).endswith("_display")), None)
    flow = safe_float(row.get(flow_col)) if flow_col else None
    if flow is not None and flow > 0:
        score += max(0.0, rule_float("fundInflowPoints", 3))
        reasons.append(f"主力净流入 {money_yi(flow)}")
    elif flow is not None and flow < 0:
        reasons.append(f"主力净流出 {money_yi(abs(flow))}")

    amount = safe_float(row.get("成交额")) or 0.0
    high_turnover = rule_float("highTurnoverThresholdYi", 100) * 100_000_000
    medium_turnover = rule_float("mediumTurnoverThresholdYi", 50) * 100_000_000
    if amount >= high_turnover:
        score += max(0.0, rule_float("highTurnoverPoints", 2))
        reasons.append(f"成交额 {money_yi(amount)}")
    elif amount >= medium_turnover:
        score += max(0.0, rule_float("mediumTurnoverPoints", 1))
        reasons.append(f"成交额 {money_yi(amount)}")

    open_price = safe_float(row.get("今开"))
    prev_close = safe_float(row.get("昨收"))
    if open_price is not None and prev_close is not None and open_price > prev_close:
        score += max(0.0, rule_float("highOpenPoints", 1))
        reasons.append("高开")

    if "板块" in source:
        score += max(0.0, rule_float("boardMemberPoints", 2))
        reasons.append("属于目标板块高成交成分")

    return min(score, 10.0), "；".join(reasons) if reasons else "个股快照未出现明显优势"


def stock_risk_profile(row: dict[str, Any], market_score: dict[str, Any], board_score: dict[str, Any]) -> tuple[float, str, str]:
    risk_ratio = 1.0
    risks: list[str] = []
    watches: list[str] = []

    change = safe_float(row.get("涨跌幅")) or 0.0
    if change <= -5:
        risk_ratio -= 0.45
        risks.append(f"跌幅较大 {pct(change)}")
    elif change < 0:
        risk_ratio -= 0.18
        risks.append(f"绿盘 {pct(change)}")
    elif change >= 8:
        risk_ratio -= 0.15
        risks.append(f"涨幅较高 {pct(change)}，追高风险增加")

    flow_col = next((key for key in row if "净流入" in key and not str(key).endswith("_display")), None)
    flow = safe_float(row.get(flow_col)) if flow_col else None
    amount = safe_float(row.get("成交额")) or 0.0
    if flow is not None and flow < 0:
        risk_ratio -= 0.22
        risks.append(f"主力净流出 {money_yi(abs(flow))}")
    if amount > 0 and flow is not None and flow < 0 and abs(flow) / amount >= 0.05:
        risk_ratio -= 0.18
        risks.append("流出占成交额偏高")

    market_value = safe_float(market_score.get("score_10")) or 0.0
    board_value = safe_float(board_score.get("score_10")) or 0.0
    if market_value < 4:
        risk_ratio -= 0.2
        risks.append(f"市场分偏低 {market_value:.2f}")
    if board_value < 4:
        risk_ratio -= 0.2
        risks.append(f"板块分偏低 {board_value:.2f}")

    open_price = safe_float(row.get("今开"))
    prev_close = safe_float(row.get("昨收"))
    if open_price is not None and prev_close is not None and prev_close > 0:
        open_change = (open_price / prev_close - 1) * 100
        if open_change > 3 and change < open_change - 2:
            risk_ratio -= 0.18
            risks.append("高开后承接不足")
            watches.append("观察能否重新站回开盘价上方")

    if flow is None:
        watches.append("等待下一轮主力资金覆盖")
    if change > 0 and flow is not None and flow > 0:
        watches.append("观察红盘和主力流入能否同步保持")
    if amount >= 10_000_000_000:
        watches.append("观察高成交额是否继续维持在前排")
    if "板块" in str(row.get("来源", "")):
        watches.append("观察目标板块高成交股扩散是否延续")

    risk_ratio = max(0.0, min(1.0, risk_ratio))
    risk_text = "风险较低" if not risks else "；".join(risks)
    watch_text = "；".join(dict.fromkeys(watches)) if watches else "观察价格、成交额和资金流是否同向"
    return risk_ratio, risk_text, watch_text


def build_stock_scores(
    top20: pd.DataFrame,
    board_sorted: pd.DataFrame,
    market_score: dict[str, Any],
    board_score: dict[str, Any],
    config: dict[str, Any],
    board_score_by_code: dict[str, dict[str, Any]] | None = None,
    limit: int = 35,
) -> list[dict[str, Any]]:
    weights = score_weights(config)
    board_score_by_code = board_score_by_code or {}
    peer_context = build_theme_peer_context(board_sorted, config)
    candidates: dict[str, dict[str, Any]] = {}

    def add_rows(df: pd.DataFrame, source: str) -> None:
        if df.empty or "代码" not in df.columns:
            return
        for _, row in df.iterrows():
            code = normalize_code(row.get("代码"))
            item = row.to_dict()
            if code in candidates:
                old_source = candidates[code]["来源"]
                candidates[code].update(item)
                if source not in old_source:
                    candidates[code]["来源"] = f"{old_source}+{source}"
            else:
                item["来源"] = source
                candidates[code] = item

    add_rows(top20, "两市前20")
    add_rows(board_sorted.head(15), "板块高成交")

    records: list[dict[str, Any]] = []
    for row in candidates.values():
        code = normalize_code(row.get("代码"))
        row_board_score = board_score_by_code.get(code, board_score)
        self_score, reason = stock_self_score(row, str(row.get("来源", "")), config)
        risk_ratio, risk_text, watch_text = stock_risk_profile(row, market_score, row_board_score)
        market_part = round(market_score["score_10"] * weights["market"], 2)
        board_part = round(row_board_score["score_10"] * weights["board"], 2)
        self_part = round(self_score * weights["stock"], 2)
        risk_part = round(RAW_SCORE_MAX * weights["risk"] * risk_ratio, 2)
        risk_deduction = round(RAW_SCORE_MAX * weights["risk"] * (1 - risk_ratio), 2)
        rule_score = round(max(0.0, min(10.0, market_part + board_part + self_part + risk_part)), 2)
        fund_profile = stock_fund_match_profile(row, peer_context)
        final_score = rule_score + float(fund_profile.get("资金模型调分") or 0.0)
        score_cap = fund_profile.get("资金模型分数上限")
        cap_value = safe_float(score_cap)
        if cap_value is not None:
            final_score = min(final_score, cap_value)
        final_score = round(max(0.0, min(10.0, final_score)), 2)
        flow = row_main_flow_value(row)
        match_status = str(fund_profile.get("资金匹配") or "")
        fund_conclusion = str(fund_profile.get("资金结论") or "")
        if match_status == "达标":
            reason = append_unique_phrase(reason, fund_conclusion)
        elif match_status == "不达标":
            risk_text = append_unique_phrase(risk_text, fund_conclusion)
        elif match_status in {"未覆盖", "不适用"}:
            watch_text = append_unique_phrase(watch_text, fund_conclusion)
        records.append(
            {
                "建议": stock_action(final_score),
                "综合分": final_score,
                "综合分_display": f"{final_score:.2f}",
                "规则分": rule_score,
                "规则分_display": f"{rule_score:.2f}",
                "名称": row.get("名称", "-"),
                "代码": code,
                "来源": row.get("来源", "-"),
                "市场映射": market_part,
                "市场映射_display": f"{market_part:.2f}",
                "板块映射": board_part,
                "板块映射_display": f"{board_part:.2f}",
                "个股基础": self_part,
                "个股基础_display": f"{self_part:.2f}",
                "风险缓冲": risk_part,
                "风险缓冲_display": f"{risk_part:.2f}",
                "风险扣分": risk_deduction,
                "风险扣分_display": f"-{risk_deduction:.2f}",
                "主力净流入": flow,
                "主力净流入_display": "未覆盖" if flow is None else money_yi(flow),
                "成交额": row.get("成交额"),
                "成交额_display": money_yi(row.get("成交额")),
                "理由": reason,
                "风险": risk_text,
                "观察点": watch_text,
                **fund_profile,
            }
        )
    records.sort(key=lambda item: item["综合分"], reverse=True)
    return records[:limit]


def market_phase(timestamp: datetime) -> str:
    hhmm = timestamp.strftime("%H:%M")
    if hhmm < "09:30":
        return "盘前未开盘"
    if "09:30" <= hhmm <= "11:30":
        return "上午交易中"
    if "11:30" < hhmm < "13:00":
        return "午间休市"
    if "13:00" <= hhmm <= "15:00":
        return "下午交易中"
    return "收盘后"


def has_valid_turnover(df: pd.DataFrame) -> bool:
    if df.empty or "成交额" not in df.columns:
        return False
    return bool((to_num(df["成交额"]) > 0).any())


def indicative_price(row: pd.Series) -> float | None:
    for col in ["今开", "最新价"]:
        value = safe_float(row.get(col))
        if value is not None and value > 0:
            return value

    bid = safe_float(row.get("买入"))
    ask = safe_float(row.get("卖出"))
    if bid is not None and bid > 0 and ask is not None and ask > 0:
        return (bid + ask) / 2
    if bid is not None and bid > 0:
        return bid
    if ask is not None and ask > 0:
        return ask
    return None


def auction_preview_records(df: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    if df.empty or "昨收" not in df.columns:
        return []

    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        reference = indicative_price(row)
        prev_close = safe_float(row.get("昨收"))
        if reference is None or prev_close is None or prev_close <= 0:
            continue

        change_pct = (reference - prev_close) / prev_close * 100
        item = {
            "代码": normalize_code(row.get("代码")),
            "名称": row.get("名称"),
            "最新价": reference,
            "最新价_display": f"{reference:.2f}",
            "涨跌幅": change_pct,
            "涨跌幅_display": pct(change_pct),
            "成交额": 0,
            "成交额_display": "集合竞价",
            "主力净流入_display": "-",
        }
        rows.append(item)

    return sorted(rows, key=lambda item: abs(float(item["涨跌幅"])), reverse=True)[:limit]


def empty_market_report(
    args: argparse.Namespace,
    timestamp: datetime,
    top20: pd.DataFrame,
    spot: pd.DataFrame,
    phase: str,
) -> dict[str, Any]:
    top20_cols = ["代码", "名称", "今开", "最新价", "涨跌幅", "成交额"]
    auction_records = auction_preview_records(spot, args.top_n)
    if auction_records:
        evidence = f"{phase}：尚无正式成交额，当前显示集合竞价参考价/买卖盘观察，09:25 后更接近竞价结果，09:30 后切换正式盘中成交额"
    else:
        evidence = f"{phase}：行情源暂未返回有效竞价/成交数据，09:25 后可尝试读取集合竞价结果，09:30 后读取正式盘中数据"

    score_items = [
        ScoreItem("行情数据状态", RAW_SCORE_MAX, 0, False, evidence),
    ]
    empty_group = score_group(score_items)
    return {
        "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "board": {"_source": "等待行情", "_name": args.board_keyword},
        "raw_score": 0,
        "score_10": 0,
        "signal": evidence,
        "score_items": [dataclasses.asdict(item) for item in score_items],
        "market_score": empty_group,
        "board_score": score_group([]),
        "board_scores": [],
        "ks11_status": None,
        "stock_scores": [],
        "top20_records": auction_records,
        "market_tables": {
            "fund_in_top15": [],
            "fund_out_top15": [],
            "fund_in_big_turnover_top10": [],
            "fund_out_top10": [],
            "big_turnover_drop5": [],
        },
        "board_records": [],
        "top20_table": format_table(top20, top20_cols, args.top_n),
        "board_table": evidence,
        "meta": {
            "board_keyword": args.board_keyword,
            "board_match": "等待行情",
            "high_open_ratio": 0.0,
            "top_n": args.top_n,
            "market_phase": phase,
            "data_ready": False,
            "auction_ready": bool(auction_records),
            "primary_table_title": "集合竞价观察（非正式成交额排名）" if auction_records else "两市成交额前20",
            "top20_fund_covered": 0,
            "top20_fund_total": len(auction_records),
            "fund_source_policy": "集合竞价阶段不读取主力资金流",
        },
    }


def build_report_from_snapshot(
    args: argparse.Namespace,
    spot: pd.DataFrame,
    fund: pd.DataFrame,
    *,
    timestamp: datetime | None = None,
    allow_external_board: bool = True,
    allow_external_checks: bool = True,
) -> dict[str, Any]:
    timestamp = timestamp or now_cn()
    phase = market_phase(timestamp)
    scoring_config = active_scoring_config(args)
    use_theme_board_score = board_theme_enabled(scoring_config)
    top20 = top_turnover_table(spot, fund, args.top_n)

    if not has_valid_turnover(top20):
        return empty_market_report(args, timestamp, top20, spot, phase)

    board = {"_source": "自定义组合", "_name": "板块组合", "_fallback": True} if use_theme_board_score else (
        choose_board(args.board_keyword) if allow_external_board else {
            "_source": "自定义",
            "_name": args.board_keyword,
            "_fallback": True,
        }
    )
    board_constituents = pd.DataFrame()
    board_spot = pd.DataFrame()
    board_flow_value = None
    board_flow_evidence = "未匹配到板块"

    if board and not use_theme_board_score:
        try:
            board_constituents = fetch_board_constituents(board)
            board_spot = fallback_board_spot(args.board_keyword, board_constituents, spot)
        except Exception as exc:
            board_flow_evidence = f"板块成分获取失败: {exc}"
        if allow_external_board:
            board_flow_value, board_flow_evidence = board_main_flow(board)
        if board_flow_value is None:
            board_flow_value, board_flow_evidence = aggregate_board_fund_flow(board_spot, fund)

    red_count = int((top20["涨跌幅"] > 0).sum()) if "涨跌幅" in top20 else 0
    green_count = int((top20["涨跌幅"] < 0).sum()) if "涨跌幅" in top20 else 0
    top50 = top_turnover_table(spot, fund, 50)
    local_indices = load_local_indices(args)
    index_ratio, index_evidence = market_index_resonance_ratio(local_indices)
    breadth_ratio, breadth_evidence = market_breadth_ratio(spot)
    turnover_core_ratio, turnover_core_evidence = market_turnover_core_ratio(top20, top50)
    fund_diffusion_ratio, fund_diffusion_evidence = market_fund_diffusion_ratio(top20)
    risk_control_ratio, risk_control_evidence = market_risk_control_ratio(spot, top20)

    high_open_hit, high_open_evidence, high_open_ratio = high_open_score(
        board_spot,
        criterion_float(scoring_config, "board_high_open", "threshold", args.high_open_ratio),
    )
    if allow_external_checks:
        pullback_hit, pullback_evidence = pullback_recovery_score(
            board_spot,
            timestamp,
            args.minute_sample_size,
        )
        new_high_hit, new_high_evidence = new_high_score(
            board_spot,
            timestamp,
            args.new_high_sample_size,
            args.new_high_window,
        )
    else:
        pullback_hit = False
        pullback_evidence = "板块切换使用上次全盘缓存，分钟线回流等待下一次全盘刷新更新"
        new_high_hit = False
        new_high_evidence = "板块切换使用上次全盘缓存，60日新高等待下一次全盘刷新更新"
    flow_hit = board_flow_value is not None and board_flow_value > 0
    board_red_green_hit, board_red_green_evidence = board_red_green_score(board_spot)
    top_turnover_limit = max(1, criterion_int(scoring_config, "board_top_turnover_red", "limit", 5))
    top_turnover_min_red = max(1, criterion_int(scoring_config, "board_top_turnover_red", "minRed", 3))
    board_top5_red_hit, board_top5_red_evidence = board_top_turnover_red_score(
        board_spot,
        top_turnover_limit,
        top_turnover_min_red,
    )
    board_median_hit, board_median_evidence = board_median_change_score(
        board_spot,
        criterion_float(scoring_config, "board_median_positive", "threshold", 0),
    )
    fund_breadth_hit, fund_breadth_evidence = fund_flow_breadth_score(board_spot, fund)

    market_items = [
        configured_score_item_ratio(
            scoring_config,
            "market_index_resonance",
            "指数共振",
            index_ratio,
            index_evidence,
        ),
        configured_score_item_ratio(
            scoring_config,
            "market_breadth",
            "全市场广度",
            breadth_ratio,
            breadth_evidence,
        ),
        configured_score_item_ratio(
            scoring_config,
            "market_turnover_core",
            "成交核心强度",
            turnover_core_ratio,
            turnover_core_evidence,
        ),
        configured_score_item_ratio(
            scoring_config,
            "market_fund_diffusion",
            "资金扩散",
            fund_diffusion_ratio,
            fund_diffusion_evidence,
        ),
        configured_score_item_ratio(
            scoring_config,
            "market_risk_control",
            "风险可控",
            risk_control_ratio,
            risk_control_evidence,
        ),
    ]
    market_items = [item for item in market_items if item is not None]
    market_score = score_group(market_items, scoring_config.get("signals"))

    theme_board_df = pd.DataFrame()
    board_scores: list[dict[str, Any]] = []
    board_score_by_code: dict[str, dict[str, Any]] = {}
    ks11_info: dict[str, Any] | None = None
    if use_theme_board_score:
        board_score, board_items, theme_board_df, board_score_by_code = build_combined_board_theme_score(
            spot,
            fund,
            local_indices,
            top20,
            scoring_config,
        )
        board_scores = [board_score]
        ks11_info = ks11_status(local_indices, scoring_config)
        market_score = {
            "raw_score": board_score["score_10"],
            "score_max": 10,
            "score_10": board_score["score_10"],
            "signal": "市场条件已并入板块综合评分",
            "items": [],
        }
    else:
        board_items = [
            configured_score_item(scoring_config, "board_high_open", "高开", high_open_hit, high_open_evidence),
            configured_score_item(scoring_config, "board_pullback_recovery", "高开低走10分钟后回流", pullback_hit, pullback_evidence),
            configured_score_item(scoring_config, "board_new_high", f"{args.board_keyword}板块个股创新高", new_high_hit, new_high_evidence),
            configured_score_item(scoring_config, "board_main_fund_inflow", "主力资金流入", flow_hit, board_flow_evidence),
            configured_score_item(scoring_config, "board_red_green", "板块红盘数大于绿盘数", board_red_green_hit, board_red_green_evidence),
            configured_score_item(scoring_config, "board_top_turnover_red", f"板块成交额前{top_turnover_limit}红盘不少于{top_turnover_min_red}只", board_top5_red_hit, board_top5_red_evidence),
            configured_score_item(scoring_config, "board_median_positive", "板块涨幅中位数大于阈值", board_median_hit, board_median_evidence),
            configured_score_item(scoring_config, "board_fund_breadth", "板块主力净流入家数大于净流出家数", fund_breadth_hit, fund_breadth_evidence),
        ]
        board_items = [item for item in board_items if item is not None]
        board_score = score_group(board_items, scoring_config.get("signals"))
        board_scores = [dict(board_score, name=args.board_keyword, theme=args.board_keyword, mode="board_constituents")]

    if use_theme_board_score:
        score_items = board_items
        score_max = 10
        raw_score = board_score["score_10"]
        score_10 = board_score["score_10"]
        combined_signal = board_score["signal"]
    else:
        score_items = market_items + board_items
        score_max = 20
        raw_score = round((market_score.get("score_10") or 0) + (board_score.get("score_10") or 0), 2)
        score_10 = round(raw_score / 2, 2)
        combined_signal = score_signal(score_10, scoring_config.get("signals"))

    main_flow_col = find_main_flow_column(top20)
    top20_cols = ["代码", "名称", "今开", "最新价", "涨跌幅", "成交额"]
    if main_flow_col:
        top20_cols.append(main_flow_col)
    if "资金流来源" in top20.columns:
        top20_cols.append("资金流来源")
    top20_fund_covered, top20_fund_total = fund_coverage(top20)

    board_sorted = board_spot.sort_values("成交额", ascending=False) if not board_spot.empty else board_spot
    board_display = theme_board_df if use_theme_board_score and not theme_board_df.empty else board_sorted
    board_flow_col = find_main_flow_column(board_display) if not board_display.empty else None
    board_cols = ["主题组合", "代码", "名称", "最新价", "涨跌幅", "成交额", "今开", "昨收"]
    if board_flow_col:
        board_cols.append(board_flow_col)
    if "资金流来源" in board_display.columns:
        board_cols.append("资金流来源")
    stock_scores = build_stock_scores(
        top20,
        board_display,
        market_score,
        board_score,
        scoring_config,
        board_score_by_code=board_score_by_code,
    )

    return {
        "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "board": board,
        "raw_score": raw_score,
        "score_10": round(score_10, 2),
        "signal": combined_signal,
        "score_items": [dataclasses.asdict(item) for item in score_items],
        "market_score": market_score,
        "board_score": board_score,
        "board_scores": board_scores,
        "ks11_status": ks11_info,
        "stock_scores": stock_scores,
        "top20_records": records_for_web(top20, top20_cols, args.top_n),
        "market_tables": market_derived_tables(spot, fund),
        "board_records": records_for_web(board_display, board_cols, 15),
        "top20_table": format_table(top20, top20_cols, args.top_n),
        "board_table": format_table(board_display, board_cols, 15)
        if not board_display.empty
        else "未取到板块成分行情",
        "meta": {
            "board_keyword": args.board_keyword,
            "board_match": f"{board['_source']}:{board['_name']}" if board else None,
            "board_scoring_mode": "theme_groups" if use_theme_board_score else "board_constituents",
            "board_table_title": "板块组合监控股票" if use_theme_board_score else f"{args.board_keyword}高成交成分",
            "high_open_ratio": high_open_ratio,
            "top_n": args.top_n,
            "market_phase": phase,
            "data_ready": True,
            "auction_ready": False,
            "primary_table_title": "两市成交额前20",
            "top20_fund_covered": top20_fund_covered,
            "top20_fund_total": top20_fund_total,
            "fund_source_policy": "东方财富主源，腾讯成交额榜分页兜底，仍缺失则未覆盖",
            "snapshot_mode": "外部刷新" if allow_external_checks else "缓存板块切换",
            "score_max": score_max,
            "score_normalized_10": score_10,
        },
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    timestamp = now_cn()
    phase = market_phase(timestamp)
    spot = fetch_spot()
    top20 = top_turnover_table(spot, pd.DataFrame(), args.top_n)

    if not has_valid_turnover(top20):
        return empty_market_report(args, timestamp, top20, spot, phase)

    fund = fetch_individual_fund_rank()
    return build_report_from_snapshot(
        args,
        spot,
        fund,
        timestamp=timestamp,
        allow_external_board=True,
        allow_external_checks=True,
    )


def render_markdown(report: dict[str, Any]) -> str:
    board_match = report["meta"].get("board_match") or "未匹配"
    lines = [
        f"# A股盘中动态解读 {report['timestamp']}",
        "",
        f"- 目标板块: {report['meta']['board_keyword']}（匹配: {board_match}）",
        f"- 评分: {report['score_10']}/10（原始 {report['raw_score']}/{RAW_SCORE_MAX}）",
        f"- 结论: {report['signal']}",
        "",
        "## 打分明细",
        "",
        "| 项目 | 得分 | 是否触发 | 证据 |",
        "|---|---:|---|---|",
    ]
    for item in report["score_items"]:
        hit = "是" if item["hit"] else "否"
        evidence = str(item["evidence"]).replace("|", "/")
        lines.append(f"| {item['name']} | {item['points']}/{item['max_points']} | {hit} | {evidence} |")

    lines.extend(
        [
            "",
            "## 两市成交额前20",
            "",
            report["top20_table"],
            "",
            f"## {report['meta']['board_keyword']}板块高成交成分",
            "",
            report["board_table"],
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A-share intraday market monitor")
    parser.add_argument("--board-keyword", default="光纤", help="板块/概念关键词，例如 光纤、CPO、通信设备")
    parser.add_argument("--top-n", type=int, default=20, help="两市成交额排名数量")
    parser.add_argument("--high-open-ratio", type=float, default=0.5, help="板块高开成分股占比阈值")
    parser.add_argument("--minute-sample-size", type=int, default=8, help="用于识别高开低走回流的板块高成交样本数")
    parser.add_argument("--new-high-sample-size", type=int, default=20, help="用于识别新高的板块高成交样本数")
    parser.add_argument("--new-high-window", type=int, default=60, help="动态新高回看交易日数量")
    parser.add_argument("--use-env-proxy", action="store_true", help="使用系统/环境代理；默认关闭以避免本地代理中断行情请求")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.use_env_proxy:
        disable_requests_env_proxy()
    try:
        report = build_report(args)
    except Exception as exc:
        print(
            "行情数据获取失败。请检查网络、东方财富接口可达性，或改用 --use-env-proxy 走系统代理。\n"
            f"错误: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
