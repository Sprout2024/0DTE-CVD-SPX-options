"""一年期 SPY 回测：用缓存 SPY 1 分钟数据跑震荡铁鹰策略。

SPY 价格 ~770，映射到 SPX 期权用 spot_scale=10。
策略规则：30 分钟窗口 |斜率|<=0.15 点/分 = range -> 开 Iron Condor（偏移30/翼25,
TP=70%权利金, SL=1.5x权利金, 4腿成本6.40）。
入场：range + 双过滤通过，每笔交易后 cooldown 10 分钟，可重复入场直到
最大持仓 max_position（默认5）；每个持仓独立管理 TP/SL/EOD，当日收盘平仓。

用法:
    .venv/bin/python run_spy_year.py --cache data/spy_1y.pkl --detail
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
from datetime import datetime, time as dtime
from statistics import linear_regression
from zoneinfo import ZoneInfo

from backtest import OptionPricer
from config import Config
from cvd_engine import Bar

ET = ZoneInfo("America/New_York")
SE = dtime(16, 0)
START = dtime(9, 30)
COST = 6.40
IV = 0.246
MIN_CREDIT = 1.00
MAX_CREDIT = 30.00
SPOT_SCALE = 10.0

_CFG = Config()
WINDOW = _CFG.regime_min_window
THR = _CFG.regime_min_slope
OFFSET = _CFG.iron_condor_offset
WING = _CFG.iron_condor_wing
TP_PCT = _CFG.iron_condor_take_profit_pct
SL_PCT = _CFG.iron_condor_stop_loss_pct
OPEN_VOL = _CFG.open_vol_filter
OPEN_VOL_WIN = _CFG.open_vol_window_minutes
PREV_TREND = _CFG.prev_day_trend_filter
PREV_WIN = _CFG.prev_day_trend_window_minutes
MAX_POSITION = _CFG.max_position
COOLDOWN = _CFG.cooldown_seconds
MAX_DAILY_SL = _CFG.max_daily_sl



def snap5(v: float) -> int:
    return int(round(v / 5.0)) * 5


def regime_1min(closes, lb: int, thr: float) -> str:
    w = closes[-lb:]
    if len(w) < lb:
        return "none"
    slope, _ = linear_regression(list(range(len(w))), w)
    if abs(slope) <= thr:
        return "range"
    return "up" if slope > 0 else "down"


def condor_value(pricer, spot, ts, sc, lc, sp, lp) -> float:
    return round(pricer.spread_mid("bear", sc, lc, spot, ts)
                 + pricer.spread_mid("bull", sp, lp, spot, ts), 2)


def _build_pos(entry_ts, spot):
    """Build a condor position dict at (entry_ts, spot). Returns None if the
    credit is outside the acceptable range."""
    pricer = OptionPricer(iv=IV)
    sc = snap5(spot + OFFSET)
    lc = snap5(spot + OFFSET + WING)
    sp = snap5(spot - OFFSET)
    lp = snap5(spot - OFFSET - WING)
    credit = condor_value(pricer, spot, entry_ts, sc, lc, sp, lp)
    if credit < MIN_CREDIT or credit > MAX_CREDIT:
        return None
    return {
        "entry_ts": entry_ts,
        "entry_spot": spot,
        "credit": credit,
        "tp": round((1 - TP_PCT) * credit, 2),
        "sl": round((1 + SL_PCT) * credit, 2),
        "sc": sc, "lc": lc, "sp": sp, "lp": lp,
    }


def _manage_exits(bar, open_pos, trades, sl_counter):
    """Check TP/SL exits for all open positions on ``bar``. Returns True if any
    position exited (so the caller re-checks remaining positions on the same bar).
    ``sl_counter`` is a list [n] used to accumulate the day's SL count."""
    pricer = OptionPricer(iv=IV)
    any_exit = False
    for pos in list(open_pos):
        val = condor_value(pricer, bar.close, bar.ts, pos["sc"], pos["lc"], pos["sp"], pos["lp"])
        kind = None
        close_v = None
        if val >= pos["sl"]:
            kind, close_v = "SL", val
            sl_counter[0] += 1
        elif val <= pos["tp"]:
            kind, close_v = "TP", val
        if kind is not None:
            pnl = round((pos["credit"] - close_v) * 100 - COST, 2)
            trades.append({"kind": kind, "credit": pos["credit"], "exit": bar.ts,
                           "exit_v": close_v,
                           "held": round((bar.ts - pos["entry_ts"]).total_seconds(), 0),
                           "pnl": pnl, "entry": pos["entry_ts"]})
            open_pos.remove(pos)
            any_exit = True
    return any_exit


def run_day(raw):
    bars = build_bars(raw)
    if not bars:
        return None
    closes = [b.close for b in bars]
    open_pos = []
    trades = []
    last_entry_ts = None
    pricer = OptionPricer(iv=IV)
    sl_counter = [0]

    for i, b in enumerate(bars):
        t = b.ts.time()
        # 1. manage exits (TP/SL) for all open positions on this bar
        while _manage_exits(b, open_pos, trades, sl_counter):
            pass
        # 2. allow new entries only within the entry window (before 15:30)
        if t < START or t > dtime(15, 30):
            continue
        # 3. entry gating: max position, daily SL limit, cooldown, range gate
        if len(open_pos) >= MAX_POSITION:
            continue
        if MAX_DAILY_SL > 0 and sl_counter[0] >= MAX_DAILY_SL:
            continue
        if last_entry_ts is not None and (b.ts - last_entry_ts).total_seconds() < COOLDOWN:
            continue
        if regime_1min(closes[:i + 1], WINDOW, THR) != "range":
            continue
        pos = _build_pos(b.ts, b.close)
        if pos is None:
            continue
        open_pos.append(pos)
        last_entry_ts = b.ts

    # EOD: close all remaining open positions at the last RTH bar
    last = [x for x in bars if x.ts.time() <= SE]
    if last:
        eod_bar = last[-1]
        for pos in list(open_pos):
            val = condor_value(pricer, eod_bar.close, eod_bar.ts, pos["sc"], pos["lc"], pos["sp"], pos["lp"])
            pnl = round((pos["credit"] - val) * 100 - COST, 2)
            trades.append({"kind": "EOD", "credit": pos["credit"], "exit": eod_bar.ts,
                           "exit_v": val,
                           "held": round((eod_bar.ts - pos["entry_ts"]).total_seconds(), 0),
                           "pnl": pnl, "entry": pos["entry_ts"]})
            open_pos.remove(pos)

    if not trades:
        return {"kind": "NOTRADE", "credit": 0.0, "exit": None,
                "exit_v": 0.0, "held": 0.0, "pnl": 0.0, "entry": None, "trades": []}
    return {
        "kind": f"{len(trades)}x",
        "day_trades": trades,
        "day_net": round(sum(x["pnl"] for x in trades), 2),
        "day_count": len(trades),
    }


def build_bars(raw):
    bars = []
    total = 0.0
    for ts, o, h, l, c, v in raw:
        if ts.time() < START:
            continue
        if ts.time() > SE:
            break
        # SPY -> SPX 刻度映射 (0.01 的 SPY 相当于 0.1 的 SPX)
        o2, h2, l2, c2 = o * SPOT_SCALE, h * SPOT_SCALE, l * SPOT_SCALE, c * SPOT_SCALE
        dp = v * (2.0 * (c2 - o2) / (h2 - l2)) if h2 > l2 else 0.0
        total += dp
        bar = Bar(ts=ts, open=o2, high=h2, low=l2, close=c2, volume=v,
                  ticks=1, cvd_open=round(total - dp, 4),
                  cvd_high=round(max(total - dp, total), 4),
                  cvd_low=round(min(total - dp, total), 4),
                  cvd_close=round(total, 4), cvd_delta=round(dp, 4))
        bars.append(bar)
    return bars


def open_vol_allows(raw):
    """Open-30min volatility filter: skip day if first N minutes' range > threshold."""
    if OPEN_VOL <= 0:
        return True
    bars = build_bars(raw)
    from datetime import timedelta as _td
    base = datetime(2000, 1, 1, 9, 30)
    cutoff = (base + _td(minutes=OPEN_VOL_WIN)).time()
    early = [b for b in bars if b.ts.time() <= cutoff]
    if not early:
        return True
    return (max(b.high for b in early) - min(b.low for b in early)) / SPOT_SCALE <= OPEN_VOL


def prev_day_trend_allows(raw):
    """Prior-day trend filter: skip if last 15-min close slope exceeds threshold."""
    if PREV_TREND <= 0:
        return True
    bars = build_bars(raw)
    closes = [b.close for b in bars if b.ts.time() <= SE]
    if len(closes) < 60:
        return True
    window = closes[-PREV_WIN:]
    fifteen = [window[i] for i in range(len(window)) if i % 15 == 14]
    if len(fifteen) < 3:
        return True
    slope, _ = linear_regression(list(range(len(fifteen))), fifteen)
    return abs(slope) <= PREV_TREND


def set_filters(open_vol, prev_trend):
    global OPEN_VOL, PREV_TREND
    OPEN_VOL = open_vol
    PREV_TREND = prev_trend


def main(args):
    set_filters(args.open_vol, args.prev_trend)
    cache = pickle.load(open(args.cache, "rb"))
    days = sorted(cache.keys())
    day_stats = []
    for idx, day in enumerate(days):
        # prior-day trend filter uses the previous trading day's raw data
        prev_raw = cache[days[idx - 1]] if idx > 0 else None
        if prev_raw is not None and not prev_day_trend_allows(prev_raw):
            day_stats.append({"day": day, "kind": "SKIPPED_PREV", "trades": []})
            continue
        if not open_vol_allows(cache[day]):
            day_stats.append({"day": day, "kind": "SKIPPED_OPENVOL", "trades": []})
            continue
        r = run_day(cache[day])
        if r is None:
            day_stats.append({"day": day, "kind": "NODATA", "trades": []})
            continue
        day_trades = r.get("day_trades") or []
        for t in day_trades:
            t["day"] = day
        day_stats.append({"day": day, "kind": r["kind"], "trades": day_trades,
                          "day_net": r.get("day_net", 0.0)})

    # flatten all individual trades
    trades = []
    for ds in day_stats:
        trades.extend(ds["trades"])
    wins = [r for r in trades if r["pnl"] > 0]
    losses = [r for r in trades if r["pnl"] <= 0]
    net = sum(r["pnl"] for r in trades)
    nodata = [r for r in day_stats if r["kind"] == "NODATA"]
    skip_prev = [r for r in day_stats if r["kind"] == "SKIPPED_PREV"]
    skip_ov = [r for r in day_stats if r["kind"] == "SKIPPED_OPENVOL"]
    active_days = [r for r in day_stats if r["kind"] not in ("SKIPPED_PREV", "SKIPPED_OPENVOL", "NODATA")]
    avg_holds = sum(t["held"] for t in trades) / max(len(trades), 1)

    print("\n===== SPY 一年期回测报告 =====")
    print(f"交易日      : {len(day_stats)}  (无数据 {len(nodata)})")
    print(f"过滤跳过    : 前一天趋势 {len(skip_prev)}  开盘波动 {len(skip_ov)}")
    print(f"参与日      : {len(active_days)}  (含 {sum(1 for d in active_days if d['kind']=='NOTRADE')} 日无成交)")
    print(f"开仓总笔数  : {len(trades)}  平均 {len(trades)/max(len(active_days),1):.1f} 笔/日")
    print(f"胜/负       : {len(wins)} / {len(losses)}  (胜率 {len(wins)/max(len(trades),1)*100:.0f}%)")
    print(f"净盈亏      : {net:.2f} USD")
    print(f"平均/笔     : {net/max(len(trades),1):.2f}")
    print(f"TP/SL/EOD   : {sum(1 for r in trades if r['kind']=='TP')}/"
          f"{sum(1 for r in trades if r['kind']=='SL')}/"
          f"{sum(1 for r in trades if r['kind']=='EOD')}")
    print("==============================")

    if args.detail:
        print("\n#   day         entry    exit     kind    credit  exit_v  held(s)    pnl")
        for i, r in enumerate(trades, 1):
            et = r["entry"].isoformat()[11:16] if r["entry"] else "--:--"
            xt = r["exit"].isoformat()[11:16] if r["exit"] else "--:--"
            print(f"{i:>2}  {r['day']}  {et}  {xt}  {r['kind']:<7} "
                  f"{r['credit']:>6.2f} {r['exit_v']:>6.2f} {r['held']:>7.0f} {r['pnl']:>8.2f}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(day_stats, f, indent=2, default=str)
        print("results ->", args.json)
    return 0


def parse_args():
    p = argparse.ArgumentParser(description="SPY 一年期震荡铁鹰回测")
    p.add_argument("--cache", default="data/spy_1y.pkl")
    p.add_argument("--detail", action="store_true")
    p.add_argument("--json", default="")
    p.add_argument("--open-vol", type=float, default=OPEN_VOL,
                   help="open-30min volatility filter threshold (points); 0=off")
    p.add_argument("--prev-trend", type=float, default=PREV_TREND,
                   help="prior-day 15min slope filter threshold; 0=off")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(main(parse_args()))