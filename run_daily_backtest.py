"""逐日回测脚本：每天独立拉取 IBKR 历史 1 分钟 bar，跑最优震荡铁鹰策略。

策略规则（当前最优参数）：
- 信号源：ES 期货 1 分钟 bar，用 30 分钟窗口线性回归斜率判断 regime
- |斜率| <= 0.15 点/分 -> range：开 Iron Condor（偏移 30，翼宽 25，TP=70% 权利金, SL=1.5 倍权利金）
- 数据不足/趋势 -> 继续等待；首个 range 出现即入场一次，当日不再入
- 0DTE 当日收盘（16:00 ET）前离场，逐日独立

用法:
    .venv/bin/python run_daily_backtest.py --start 2026-06-22 --end 2026-08-17 --cache data/daily_backtest.pkl
    .venv/bin/python run_daily_backtest.py --start 2025-06-01 --end 2026-08-17
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pickle
from datetime import date, datetime, time as dtime, timedelta
from statistics import linear_regression
from zoneinfo import ZoneInfo

from ib_insync import IB

from backtest import OptionPricer
from config import Config
from contracts import make_signal_contract
from cvd_engine import Bar

ET = ZoneInfo("America/New_York")
SE = dtime(16, 0)
START = dtime(9, 30)
COST = 6.40
IV = 0.246
WINDOW = 30  # 分钟
THR = 0.15  # 点/分
MIN_CREDIT = 1.00
MAX_CREDIT = 30.00
OFFSET = 30.0
WING = 25.0
TP_PCT = 0.70
SL_PCT = 0.50


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


def sim_exit(entry_ts, spot, bars):
    pricer = OptionPricer(iv=IV)
    sc = snap5(spot + OFFSET)
    lc = snap5(spot + OFFSET + WING)
    sp = snap5(spot - OFFSET)
    lp = snap5(spot - OFFSET - WING)
    credit = condor_value(pricer, spot, entry_ts, sc, lc, sp, lp)
    if credit < MIN_CREDIT or credit > MAX_CREDIT:
        return None
    tp = round((1 - TP_PCT) * credit, 2)
    sl = round((1 + SL_PCT) * credit, 2)
    for b in bars:
        if b.ts <= entry_ts:
            continue
        if b.ts.time() > SE:
            break
        val = condor_value(pricer, b.close, b.ts, sc, lc, sp, lp)
        if val >= sl:
            return (b.ts, "SL", val, credit)
        if val <= tp:
            return (b.ts, "TP", val, credit)
    last = [b for b in bars if b.ts > entry_ts and b.ts.time() <= SE]
    b = last[-1] if last else bars[-1]
    val = condor_value(pricer, b.close, b.ts, sc, lc, sp, lp)
    return (b.ts, "EOD", val, credit)


def build_bars(raw):
    bars = []
    total = 0.0
    for ts, o, h, l, c, v in raw:
        if ts.time() < START:
            continue
        dp = v * (2.0 * (c - o) / (h - l)) if h > l else 0.0
        total += dp
        bar = Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v,
                  ticks=1, cvd_open=round(total - dp, 4),
                  cvd_high=round(max(total - dp, total), 4),
                  cvd_low=round(min(total - dp, total), 4),
                  cvd_close=round(total, 4), cvd_delta=round(dp, 4))
        bars.append(bar)
    return bars


def run_day(raw):
    """跑单日策略，返回 (entry_info, pnl_info) 或 None。"""
    bars = build_bars(raw)
    if not bars:
        return None
    closes = [b.close for b in bars]
    for i, b in enumerate(bars):
        t = b.ts.time()
        if t < START or t > dtime(15, 30):
            continue
        r = regime_1min(closes[:i + 1], WINDOW, THR)
        if r != "range":
            continue
        r2 = sim_exit(b.ts, b.close, bars)
        if r2 is None:
            return {"entry": b.ts, "kind": "NOCRED", "credit": 0.0, "exit": None,
                    "exit_v": 0.0, "held": 0.0, "pnl": 0.0}
        ex_ts, kind, exit_v, credit = r2
        pnl = round((credit - exit_v) * 100 - COST, 2)
        return {
            "entry": b.ts, "kind": kind, "credit": credit,
            "exit": ex_ts, "exit_v": exit_v,
            "held": round((ex_ts - b.ts).total_seconds(), 0), "pnl": pnl,
        }
    return {"entry": None, "kind": "NOTRADE", "credit": 0.0, "exit": None,
            "exit_v": 0.0, "held": 0.0, "pnl": 0.0}


async def fetch_day(ib, sig, day: date, retries: int = 3) -> list:
    """拉取单日 1 分钟 bar（用 2 天窗口覆盖，避免时区边界）。"""
    end = datetime(day.year, day.month, day.day, 0, 0, tzinfo=ET) + timedelta(days=1)
    for attempt in range(retries):
        try:
            bars = await ib.reqHistoricalDataAsync(
                sig, end, "2 D", "1 min", "TRADES", True, formatDate=1, timeout=40
            )
            if bars:
                out = []
                for b in bars:
                    t = b.date.astimezone(ET).replace(tzinfo=None)
                    if t.date() != day or t.time() > SE:
                        continue
                    out.append((t.replace(second=0, microsecond=0),
                                float(b.open), float(b.high), float(b.low),
                                float(b.close), float(b.volume)))
                if out:
                    return sorted(out)
        except Exception as e:
            logging.warning("day %s attempt %d failed: %s", day, attempt + 1, e)
        await asyncio.sleep(2.0)
    return []


def trading_days(start: date, end: date) -> list:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


async def main(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = Config()
    os.makedirs("data", exist_ok=True)
    cache = {}
    if args.cache and os.path.exists(args.cache):
        cache = pickle.load(open(args.cache, "rb"))
        logging.info("loaded %d cached days from %s", len(cache), args.cache)

    days = trading_days(args.start, args.end)
    logging.info("%d trading days from %s to %s", len(days), args.start, args.end)

    ib = IB()
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=20)
    sig = make_signal_contract(cfg)
    await ib.qualifyContractsAsync(sig)

    results = []
    try:
        for i, day in enumerate(days, 1):
            key = day.isoformat()
            if key in cache:
                r = cache[key]
            else:
                raw = await fetch_day(ib, sig, day)
                if not raw:
                    r = {"day": key, "kind": "NODATA", "pnl": 0.0}
                else:
                    r = run_day(raw)
                    r["day"] = key
                    r["entry_ts"] = r["entry"].isoformat() if r["entry"] else None
                    r["exit_ts"] = r["exit"].isoformat() if r["exit"] else None
                    r.pop("entry", None)
                    r.pop("exit", None)
                cache[key] = r
                if args.cache:
                    pickle.dump(cache, open(args.cache, "wb"))
            results.append(r)
            if i % 5 == 0 or i == len(days):
                traded = sum(1 for r in results if r["kind"] not in ("NOTRADE", "NODATA"))
                tot = sum(r["pnl"] for r in results)
                logging.info("progress %d/%d  traded=%d  net=%.2f", i, len(days), traded, tot)
            await asyncio.sleep(0.3)
    finally:
        ib.disconnect()

    if args.cache:
        pickle.dump(cache, open(args.cache, "wb"))

    trades = [r for r in results if r["kind"] not in ("NOTRADE", "NODATA")]
    nodata = [r for r in results if r["kind"] == "NODATA"]
    wins = [r for r in trades if r["pnl"] > 0]
    losses = [r for r in trades if r["pnl"] <= 0]
    net = sum(r["pnl"] for r in trades)
    print("\n===== 逐日回测报告 =====")
    print(f"交易日      : {len(results)}  (无数据 {len(nodata)})")
    print(f"开仓        : {len(trades)}  (含 NOCRED {sum(1 for r in trades if r['kind']=='NOCRED')})")
    print(f"胜/负       : {len(wins)} / {len(losses)}  (胜率 {len(wins)/max(len(trades),1)*100:.0f}%)")
    print(f"净盈亏      : {net:.2f} USD")
    print(f"平均/笔     : {net/max(len(trades),1):.2f}")
    print(f"最大回撤    : 见明细")
    print("==========================")

    if args.detail:
        print("\n#   day         entry    exit     kind    credit  exit_v  held(s)    pnl")
        for i, r in enumerate(trades, 1):
            et = r["entry_ts"][11:16] if r["entry_ts"] else "--:--"
            xt = r["exit_ts"][11:16] if r["exit_ts"] else "--:--"
            print(f"{i:>2}  {r['day']}  {et}  {xt}  {r['kind']:<7} "
                  f"{r['credit']:>6.2f} {r['exit_v']:>6.2f} {r['held']:>7.0f} {r['pnl']:>8.2f}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2, default=str)
        logging.info("results written to %s", args.json)
    return 0


def parse_args():
    p = argparse.ArgumentParser(description="逐日回测：ES 1分钟 bar + 震荡铁鹰策略")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002)
    p.add_argument("--client-id", type=int, default=110)
    p.add_argument("--start", type=lambda s: date.fromisoformat(s), default=date(2026, 8, 17))
    p.add_argument("--end", type=lambda s: date.fromisoformat(s), default=date(2026, 8, 17))
    p.add_argument("--cache", default="data/daily_backtest.pkl", help="逐日结果缓存 pickle")
    p.add_argument("--detail", action="store_true", help="打印每笔明细")
    p.add_argument("--json", default="", help="结果 JSON 输出路径")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))