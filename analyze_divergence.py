from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pickle
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ib_insync import IB

from config import Config
from contracts import make_signal_contract
from cvd_engine import Bar, CvdEngine, Signal
from hist_data import HistoricalLoader, save_cache, load_cache

ET = ZoneInfo("America/New_York")

BARS = "bars"      # cache key: list of (day, [(ts,o,h,l,c,v)...])
TICKS = "ticks"    # cache key: day -> minute delta dict


def cache_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "analysis.pkl")


def build_daily_signals(cfg: Config, bars: List[Tuple]) -> List[Signal]:
    """Replay 1-min bars through a CVD engine (bar delta proxy) and return divergence signals."""
    engine = CvdEngine(cfg)
    signals: List[Signal] = []
    last_extreme = None
    last_ts = None
    total = 0.0
    for ts, o, h, l, c, vol in bars:
        if h > l:
            delta_proxy = vol * (2.0 * (c - o) / (h - l))
        else:
            delta_proxy = 0.0
        total += delta_proxy
        bar = Bar(
            ts=ts, open=o, high=h, low=l, close=c, volume=vol,
            ticks=cfg.min_ticks_per_bar,
            cvd_open=round(total - delta_proxy, 4),
            cvd_high=round(max(total - delta_proxy, total), 4),
            cvd_low=round(min(total - delta_proxy, total), 4),
            cvd_close=round(total, 4),
            cvd_delta=round(delta_proxy, 4),
        )
        engine.ingest_bar(bar)
        sig = engine.detect_signal()
        if sig is None:
            continue
        # mimic strategy filters: cooldown + min signal spacing
        if last_ts is not None and (sig.bar.ts - last_ts).total_seconds() < cfg.cooldown_seconds:
            continue
        if last_extreme is not None and abs(sig.extreme - last_extreme) < cfg.min_signal_spacing:
            continue
        last_extreme = sig.extreme
        last_ts = sig.bar.ts
        signals.append(sig)
    return signals


def apply_precise_delta(cfg: Config, bars: List[Tuple], deltas: Dict[datetime, float]) -> List[Tuple]:
    """Rebuild bars with precise per-minute tick deltas where available (hybrid)."""
    out = []
    for ts, o, h, l, c, vol in bars:
        d = deltas.get(ts)
        if d is None:
            d = vol * (2.0 * (c - o) / (h - l)) if h > l else 0.0
        out.append((ts, o, h, l, c, vol, d))
    return out


def run_stats(cfg: Config, day_bars: Dict[date, List[Tuple]]) -> Dict:
    stats = {"signals": 0, "bear": 0, "bull": 0}
    for W in (2, 5, 10):
        stats[f"hold{W}"] = {"n": 0, "wins": 0, "pts": 0.0}
        stats[f"break{W}"] = {"n": 0, "wins": 0, "pts": 0.0}
    MULT = 50.0
    BUF = 1.0

    for day, bars in sorted(day_bars.items()):
        signals = build_daily_signals(cfg, bars)
        for sig in signals:
            stats["signals"] += 1
            stats[sig.direction] += 1
            ext = sig.extreme
            for W in (2, 5, 10):
                win = [b for b in bars if b[0] > sig.bar.ts and b[0] <= sig.bar.ts + timedelta(minutes=W)]
                if not win:
                    continue
                # extreme hold rate
                if sig.direction == "bear":
                    held = not any(b[2] > ext for b in win)   # high
                else:
                    held = not any(b[3] < ext for b in win)   # low
                stats[f"hold{W}"]["n"] += 1
                if held:
                    stats[f"hold{W}"]["wins"] += 1
                # breakout-follow: enter when price breaks extreme±buf, hold W
                fill = None
                fill_ts_lookup = None
                for b in win:
                    if sig.direction == "bear" and b[2] > ext + BUF:
                        fill, side, fill_ts_lookup = b[4], "LONG", b[0]
                        break
                    if sig.direction == "bull" and b[3] < ext - BUF:
                        fill, side, fill_ts_lookup = b[4], "SHORT", b[0]
                        break
                if fill is None:
                    continue
                end = sig.bar.ts + timedelta(minutes=W)
                exit_p = None
                for b in bars:
                    if b[0] <= fill_ts_lookup or b[0] > end:
                        continue
                    exit_p = b[4]
                if exit_p is None:
                    continue
                pts = (exit_p - fill) if side == "LONG" else (fill - exit_p)
                stats[f"break{W}"]["n"] += 1
                if pts > 0:
                    stats[f"break{W}"]["wins"] += 1
                stats[f"break{W}"]["pts"] += pts
    return stats


def print_stats(stats: Dict, label: str) -> None:
    n = stats["signals"]
    print(f"\n=== {label} | 信号 {n} (空 {stats['bear']} / 多 {stats['bull']}) ===")
    print(f"{'窗口':<6} | {'极值守住':>10} | {'突破顺势':>14}")
    for W in (2, 5, 10):
        h = stats[f"hold{W}"]
        b = stats[f"break{W}"]
        hs = f"{h['wins']}/{h['n']} = {h['wins']/max(1,h['n'])*100:.0f}%" if h['n'] else "-"
        bs = f"{b['wins']}/{b['n']} = {b['wins']/max(1,b['n'])*100:.0f}% ${b['pts']*50:.0f}" if b['n'] else "-"
        print(f"{W:>2}分   | {hs:>10} | {bs:>14}")


async def main() -> int:
    p = argparse.ArgumentParser(description="Multi-day ES divergence short-term stats")
    p.add_argument("--days", type=int, default=5, help="number of trading days to analyze")
    p.add_argument("--port", type=int, default=4002, help="IB Gateway/TWS port")
    p.add_argument("--client-id", type=int, default=90)
    p.add_argument("--cache-dir", default=".analysis_cache", help="cache directory")
    p.add_argument("--precise", action="store_true", help="fetch precise ticks for extreme minutes")
    p.add_argument("--no-fetch", action="store_true", help="only analyze cached data")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("analyze")
    os.makedirs(args.cache_dir, exist_ok=True)

    cfg = Config()
    ib = IB()
    if not args.no_fetch:
        try:
            await ib.connectAsync("127.0.0.1", args.port, clientId=args.client_id, timeout=20)
        except Exception as e:
            log.error("connect failed: %s", e)
            return 1

    loader = HistoricalLoader(ib, log)
    contract = make_signal_contract(cfg)

    today = datetime.now(ET).date()
    days = [d for d in [today - timedelta(days=i) for i in range(0, args.days * 2 + 5)] if d.weekday() < 5][-args.days:]

    cache = {}
    if os.path.exists(cache_path(args.cache_dir)):
        cache = load_cache(cache_path(args.cache_dir))

    day_bars: Dict[date, List[Tuple]] = {}
    for day in days:
        if str(day) in cache:
            day_bars[day] = cache[str(day)]
            log.info("%s: cached (%d bars)", day, len(day_bars[day]))
            continue
        if args.no_fetch:
            continue
        bars = await loader.fetch_day_bars(contract, day)
        if bars:
            day_bars[day] = bars
            cache[str(day)] = bars
            log.info("%s: %d bars", day, len(bars))
        else:
            log.warning("%s: no data", day)
    save_cache(cache_path(args.cache_dir), cache)

    if not day_bars:
        log.error("no data. run without --no-fetch first.")
        return 1

    if args.precise:
        # fetch precise ticks at divergence-candidate extreme minutes (hybrid)
        for day, bars in list(day_bars.items()):
            from backtest import Backtester
            bt = Backtester(cfg, None, log)
            extremes = bt.find_extreme_bars([(b[0], b[1], b[2], b[3], b[4], b[5]) for b in bars])
            deltas = await loader.fetch_minute_ticks(contract, extremes)
            from hist_data import minute_deltas
            precise = minute_deltas(deltas)
            cache[f"precise_{day}"] = precise

    stats = run_stats(cfg, day_bars)
    print_stats(stats, f"最近 {len(day_bars)} 天 (bar delta proxy)")
    if args.precise:
        # rebuild with precise deltas
        precise_day_bars = {}
        for day, bars in day_bars.items():
            precise = cache.get(f"precise_{day}", {})
            precise_day_bars[day] = [(b[0], b[1], b[2], b[3], b[4], b[5], precise.get(b[0])) for b in bars]
        stats_p = run_stats(cfg, precise_day_bars)
        print_stats(stats_p, f"hybrid (precise tick CVD)")

    if not args.no_fetch:
        ib.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
