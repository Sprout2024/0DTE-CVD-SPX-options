"""生成某交易日"是否开仓"决策文件到 data/。

基于该交易日前一交易日的 1 分钟 bar（从 state_store 或 ES/SPY 历史数据），
用策略的前一天趋势过滤规则判断当日是否适合开 Iron Condor：
- 前一天 15 分钟线斜率 |slope| <= prev_day_trend_filter => 当日可开仓
- 否则当日不开仓

用法:
    .venv/bin/python trade_decision.py                # 默认判断今天 (用昨天数据)
    .venv/bin/python trade_decision.py --date 2026-08-19   # 判断 08-19 (用 08-18 数据)
    .venv/bin/python trade_decision.py --days 10      # 保留最近10个交易日决策 (默认)
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta
from statistics import linear_regression
from zoneinfo import ZoneInfo

from config import Config

ET = ZoneInfo("America/New_York")


def last_trading_day(d: date) -> date:
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def slope_15m(closes, window_min: int) -> float:
    """Last-window 15-min close slope (points/15min)."""
    window = closes[-window_min:]
    fifteen = [window[i] for i in range(len(window)) if i % 15 == 14]
    if len(fifteen) < 3:
        return 0.0
    slope, _ = linear_regression(list(range(len(fifteen))), fifteen)
    return slope


def load_from_state(cfg: Config, day: date):
    from state_store import CvdStore

    store = CvdStore(cfg.state_file)
    bars, _ = store.load(day)
    if bars:
        closes = []
        for b in bars:
            t = b.ts
            if t.tzinfo is not None:
                t = t.astimezone(ET)
            if t.time() <= cfg.session_end:
                closes.append(b.close)
        if closes:
            return closes
    return None


def load_from_spy(cfg: Config, day: date):
    import pickle

    path = "data/spy_1y.pkl"
    if not os.path.exists(path):
        return None
    cache = pickle.load(open(path, "rb"))
    key = day.isoformat()
    if key not in cache:
        return None
    closes = [tup[4] * cfg.spot_scale for tup in cache[key]]
    return closes


def compute_decision(cfg: Config, trade_day: date) -> dict:
    """Compute the open/skip decision for ``trade_day`` based on the prior trading day."""
    prev = last_trading_day(trade_day)

    closes = None
    source = ""
    for loader, name in ((load_from_state, "state_store"),
                         (load_from_spy, "SPY_history")):
        c = loader(cfg, prev)
        if c is not None:
            closes = c
            source = name
            break

    if closes is None or len(closes) < 60:
        decision = "unknown"
        slope = None
        reason = f"no data for {prev} (needed >=60 bars)"
    else:
        slope = slope_15m(closes, cfg.prev_day_trend_window_minutes)
        threshold = cfg.prev_day_trend_filter
        decision = "open" if abs(slope) <= threshold else "skip"
        reason = (f"|prior-day 15m slope|={abs(slope):.2f} "
                  f"{'<=' if abs(slope)<=threshold else '>'} threshold {threshold}")

    return {
        "generated_at": datetime.now(ET).isoformat(),
        "trade_day": trade_day.isoformat(),
        "based_on_prev_day": prev.isoformat(),
        "data_source": source,
        "decision": decision,
        "reason": reason,
        "prev_day_15m_slope": slope,
        "prev_day_trend_filter": cfg.prev_day_trend_filter,
    }


def load_history(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        data = json.load(open(path))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return data
    # backward compat: old single-record file -> wrap into list
    if isinstance(data, dict) and "trade_day" in data:
        return [data]
    return []


def main(args) -> int:
    cfg = Config()
    trade_day = args.date           # 要判断"是否开仓"的那一天
    rec = compute_decision(cfg, trade_day)

    # maintain a rolling history of the last N trade-day decisions
    out = os.path.join("data", "trade_decision.json")
    history = load_history(out)
    history = [h for h in history if h.get("trade_day") != rec["trade_day"]]
    history.append(rec)
    history = history[-args.days:]

    os.makedirs("data", exist_ok=True)
    with open(out, "w") as f:
        json.dump(history, f, indent=2)
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    print(f"\nhistory: {len(history)} days written -> {out}")
    return 0


def parse_args():
    p = argparse.ArgumentParser(description="生成某交易日是否开仓决策文件（保留最近N天）")
    p.add_argument("--date", type=lambda s: date.fromisoformat(s),
                   default=date.today(), help="要判断是否开仓的交易日（用其前一天数据）")
    p.add_argument("--days", type=int, default=10, help="保留最近几个交易日的决策")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(main(parse_args()))