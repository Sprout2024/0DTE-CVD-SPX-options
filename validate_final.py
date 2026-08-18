"""最终定稿策略验证脚本：震荡日 Iron Condor + 趋势日空仓。

对指定交易日的背离信号，用小时级趋势门控分类：
- range -> 开 Iron Condor（最终参数）
- up/down -> 空仓

默认验证 2026-08-17（实盘跑过的 16 个信号）。也可用 --date 换日期并用
--detect 自动检测信号。

用法:
    .venv/bin/python validate_final.py                  # 08-17 已知 16 信号
    .venv/bin/python validate_final.py --date 2026-08-10 --detect
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pickle
import sys
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from ib_insync import IB

from backtest import OptionPricer
from config import Config
from contracts import make_signal_contract
from cvd_engine import Bar, CvdEngine
from hist_data import HistoricalLoader
from trend import TrendDetector

ET = ZoneInfo("America/New_York")
SE = dtime(16, 0)
CACHE = ".analysis_cache/analysis.pkl"

# 2026-08-17 实盘触发过的信号 (UTC -> 转为 ET)
KNOWN_0817 = [
    ("2026-08-17T13:48:00+00:00", "bear"),
    ("2026-08-17T14:00:00+00:00", "bull"),
    ("2026-08-17T14:03:00+00:00", "bull"),
    ("2026-08-17T14:38:00+00:00", "bull"),
    ("2026-08-17T14:55:00+00:00", "bull"),
    ("2026-08-17T15:40:00+00:00", "bull"),
    ("2026-08-17T16:54:00+00:00", "bull"),
    ("2026-08-17T17:00:00+00:00", "bull"),
    ("2026-08-17T17:05:00+00:00", "bull"),
    ("2026-08-17T17:11:00+00:00", "bull"),
    ("2026-08-17T17:14:00+00:00", "bull"),
    ("2026-08-17T17:17:00+00:00", "bull"),
    ("2026-08-17T17:23:00+00:00", "bull"),
    ("2026-08-17T17:26:00+00:00", "bull"),
    ("2026-08-17T17:34:00+00:00", "bull"),
    ("2026-08-17T17:38:00+00:00", "bull"),
]

COST = 6.40  # 铁鹰 4 腿往返


def snap5(v: float) -> int:
    return int(round(v / 5.0)) * 5


class CondorSim:
    """用 BS 定价器模拟 Iron Condor 净值，应用最终离场规则。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pricer = OptionPricer(iv=0.20)

    def value(self, spot: float, ts, sc: int, lc: int, sp: int, lp: int) -> float:
        c = self.pricer.spread_mid("bear", sc, lc, spot, ts)
        p = self.pricer.spread_mid("bull", sp, lp, spot, ts)
        return round(c + p, 2)

    def simulate(self, entry_ts, spot, bars):
        c = self.cfg
        sc = snap5(spot + c.iron_condor_offset)
        lc = snap5(spot + c.iron_condor_offset + c.iron_condor_wing)
        sp = snap5(spot - c.iron_condor_offset)
        lp = snap5(spot - c.iron_condor_offset - c.iron_condor_wing)
        credit = self.value(spot, entry_ts, sc, lc, sp, lp)
        if credit <= 0:
            return None
        tp = round((1 - c.iron_condor_take_profit_pct) * credit, 2)  # 净值<=0.30*credit 锁70%
        sl = round((1 + c.iron_condor_stop_loss_pct) * credit, 2)    # 净值>=1.5*credit
        for b in bars:
            if b[0] <= entry_ts:
                continue
            if b[0].time() > SE:
                break
            held = (b[0] - entry_ts).total_seconds()
            val = self.value(b[4], b[0], sc, lc, sp, lp)
            if val >= sl:
                return (b[0], "SL", val, held)
            if val <= tp:
                return (b[0], "TP", val, held)
        last = [b for b in bars if b[0] > entry_ts and b[0].time() <= SE]
        if not last:
            return None
        b = last[-1]
        val = self.value(b[4], b[0], sc, lc, sp, lp)
        return (b[0], "EOD", val, (b[0] - entry_ts).total_seconds())


def detect_signals(cfg, bars):
    """用 CVD 引擎自动检测背离信号（含 cooldown / 间距过滤）。"""
    engine = CvdEngine(cfg)
    out = []
    last_extreme = None
    last_ts = None
    total = 0.0
    for ts, o, h, l, c, v in bars:
        dp = v * (2.0 * (c - o) / (h - l)) if h > l else 0.0
        total += dp
        bar = Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v,
                  ticks=cfg.min_ticks_per_bar,
                  cvd_open=round(total - dp, 4), cvd_high=round(max(total - dp, total), 4),
                  cvd_low=round(min(total - dp, total), 4), cvd_close=round(total, 4), cvd_delta=round(dp, 4))
        engine.ingest_bar(bar)
        sig = engine.detect_signal()
        if sig is None:
            continue
        if last_ts is not None and (sig.bar.ts - last_ts).total_seconds() < cfg.cooldown_seconds:
            continue
        if last_extreme is not None and abs(sig.extreme - last_extreme) < cfg.min_signal_spacing:
            continue
        last_extreme = sig.extreme
        last_ts = sig.bar.ts
        out.append((sig.bar.ts, sig.direction))
    return out


async def load_bars(cfg, log, day: date, port: int) -> list:
    os.makedirs(".analysis_cache", exist_ok=True)
    cache = {}
    if os.path.exists(CACHE):
        cache = pickle.load(open(CACHE, "rb"))
    key = str(day)
    if key in cache:
        return cache[key]
    ib = IB()
    await ib.connectAsync("127.0.0.1", port, clientId=97, timeout=20)
    loader = HistoricalLoader(ib, log)
    contract = make_signal_contract(cfg)
    bars = await loader.fetch_day_bars(contract, day)
    ib.disconnect()
    if bars:
        cache[key] = bars
        pickle.dump(cache, open(CACHE, "wb"))
    return bars or []


async def main() -> int:
    p = argparse.ArgumentParser(description="最终定稿策略验证")
    p.add_argument("--date", type=lambda s: date.fromisoformat(s), default=date(2026, 8, 17))
    p.add_argument("--port", type=int, default=4002)
    p.add_argument("--detect", action="store_true", help="自动检测信号（默认用 08-17 已知 16 信号）")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("validate")
    cfg = Config()

    bars = await load_bars(cfg, log, args.date, args.port)
    if not bars:
        log.error("no data for %s", args.date)
        return 1
    bars.sort(key=lambda x: x[0])
    log.info("%s: %d bars", args.date, len(bars))

    if args.detect:
        sigs = detect_signals(cfg, bars)
        log.info("自动检测到 %d 个信号", len(sigs))
    else:
        if args.date != date(2026, 8, 17):
            log.warning("已知信号仅 08-17；其他日期请用 --detect")
        sigs = [(datetime.fromisoformat(t).astimezone(ET).replace(tzinfo=None), d) for t, d in KNOWN_0817]

    trend = TrendDetector(cfg)
    sim = CondorSim(cfg)
    bar_idx = 0
    total = 0.0
    traded = 0
    wins = 0
    skipped = 0
    total_pnl = 0.0
    print(f"\n{'#':>2} {'dir':<4} {'timeET':>7} {'regime':<6} {'credit':>6} | {'exitET':>7} {'kind':<4} {'exit':>6} {'held':>5} | {'pnl':>8}")
    for i, (entry_ts, direction) in enumerate(sigs, 1):
        while bar_idx < len(bars) and bars[bar_idx][0] <= entry_ts:
            ts, o, h, l, c, v = bars[bar_idx]
            dp = v * (2.0 * (c - o) / (h - l)) if h > l else 0.0
            total += dp
            trend.update(Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v, ticks=1, cvd_close=total, cvd_delta=dp))
            bar_idx += 1
        regime = trend.regime()
        if regime != "range":
            skipped += 1
            print(f"{i:>2} {direction:<4} {entry_ts.strftime('%H:%M'):>7} {regime:<6} | 空仓")
            continue
        spot = [b[4] for b in bars if b[0] <= entry_ts][-1]
        r = sim.simulate(entry_ts, spot, bars)
        if r is None:
            print(f"{i:>2} {direction:<4} {entry_ts.strftime('%H:%M'):>7} {regime:<6} | 无权利金")
            continue
        ex_ts, kind, exit_v, held = r
        sc = snap5(spot + cfg.iron_condor_offset)
        lc = snap5(spot + cfg.iron_condor_offset + cfg.iron_condor_wing)
        sp = snap5(spot - cfg.iron_condor_offset)
        lp = snap5(spot - cfg.iron_condor_offset - cfg.iron_condor_wing)
        credit = sim.value(spot, entry_ts, sc, lc, sp, lp)
        pnl = round((credit - exit_v) * 100 - COST, 2)
        traded += 1
        if pnl > 0:
            wins += 1
        total_pnl += pnl
        print(f"{i:>2} {direction:<4} {entry_ts.strftime('%H:%M'):>7} {regime:<6} {credit:>6.2f} | {ex_ts.strftime('%H:%M'):>7} {kind:<4} {exit_v:>6.2f} {held:>5.0f} | {pnl:>8.2f}")

    print("-" * 74)
    print(f"信号 {len(sigs)} | 铁鹰 {traded} 笔 (胜 {wins}, 胜率 {wins/traded*100:.0f}%) | 趋势日空仓 {skipped}")
    print(f"铁鹰净盈亏 ${total_pnl:.2f} (含成本 ${COST*traded:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
