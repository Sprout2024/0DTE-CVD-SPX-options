from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import pickle
import sys
from datetime import date, datetime

from ib_insync import IB

from backtest import Backtester, OptionPricer
from config import Config
from contracts import make_signal_contract
from hist_data import HistoricalLoader, load_cache, merge_to_stream, minute_deltas, save_cache, trading_days
from logger import setup_logging


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest the CVD 0DTE credit-spread scalper on SPY.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=31)
    p.add_argument("--end", type=lambda s: date.fromisoformat(s), default=date.today(), help="last day (YYYY-MM-DD)")
    p.add_argument("--days", type=int, default=1, help="number of trading days to backtest")
    p.add_argument("--data", choices=["tick", "bar", "hybrid"], default="tick", help="tick=real BID_ASK/TRADES CVD, bar=1-min proxy, hybrid=proxy + precise ticks on new high/low minutes")
    p.add_argument("--iv", type=float, default=0.20, help="option implied volatility for the BS pricer")
    p.add_argument("--lookback", type=int, default=10, help="CVD divergence lookback bars")
    p.add_argument("--min-ticks", type=int, default=3, help="min ticks per bar to trust a signal")
    p.add_argument("--cvd-gap", type=float, default=1.0, help="min CVD gap for divergence")
    p.add_argument("--tp", type=float, default=0.30, help="take profit as fraction of credit")
    p.add_argument("--sl", type=float, default=1.00, help="stop loss as fraction of credit")
    p.add_argument("--cooldown", type=int, default=120, help="cooldown seconds between trades")
    p.add_argument("--contracts", type=int, default=1, help="contracts per spread leg")
    p.add_argument("--commission", type=float, default=0.65, help="commission USD per contract per leg")
    p.add_argument("--exchange-fee", type=float, default=0.15, help="exchange fee USD per contract per leg (SPX)")
    p.add_argument("--cache", default="", help="pickle cache file for fetched ticks/bars (save or load)")
    p.add_argument("--offline", action="store_true", help="run from cache without connecting to IB")
    p.add_argument("--spot-scale", type=float, default=None, help="scale signal spot to option spot (default: cfg.spot_scale; 10.0 when replaying cached SPY ticks for SPX)")
    p.add_argument("--no-regime-filter", action="store_true", help="disable trend regime gate (range->iron condor, trend->no trade); default ON")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def save_trades_csv(path: str, trades) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["direction", "sell_strike", "buy_strike", "entry_time", "exit_time", "entry_credit", "exit_price", "kind", "gross_pnl", "cost", "pnl", "held_sec"])
        w.writeheader()
        for t in trades:
            w.writerow(t)


def print_report(stats: dict) -> None:
    if not stats or stats["trades"] == 0:
        print("no trades generated")
        return
    print("\n===== BACKTEST REPORT =====")
    print(f"trades        : {stats['trades']}   (wins {stats['wins']} / losses {stats['losses']})")
    print(f"win rate      : {stats['win_rate'] * 100:.1f}%")
    print(f"net P&L       : {stats['net_pnl']:+.2f} USD")
    print(f"total cost    : {stats['total_cost']:.2f} USD (commissions + exchange fees)")
    print(f"avg P&L/trade : {stats['avg_pnl']:+.2f}")
    print(f"avg win       : {stats['avg_win']:+.2f}")
    print(f"avg loss      : {stats['avg_loss']:+.2f}")
    print(f"profit factor : {stats['profit_factor']:.2f}")
    print(f"max drawdown  : {stats['max_drawdown']:.2f}")
    print(f"avg hold      : {stats['avg_hold_sec']:.0f}s")
    print("===========================")


def print_trades(trades) -> None:
    if not trades:
        return
    print("\n#  entry   dir  sell  buy   credit  exit   kind  held(s)  gross  cost  net")
    for i, t in enumerate(trades, 1):
        print(
            f"{i:2} {t['entry_time'].strftime('%H:%M:%S')} {t['direction'][:1]:4} "
            f"{t['sell_strike']:6.1f} {t['buy_strike']:6.1f} {t['entry_credit']:5.2f} "
            f"{t['exit_price']:5.2f}  {t['kind']:4} {t['held_sec']:7.1f} "
            f"{t['gross_pnl']:+6.2f} {t['cost']:5.2f} {t['pnl']:+6.2f}"
        )
    print()


async def main(args: argparse.Namespace) -> int:
    cfg = Config(
        log_level=args.log_level,
        log_file="",
        divergence_lookback=args.lookback,
        min_ticks_per_bar=args.min_ticks,
        min_cvd_gap=args.cvd_gap,
        take_profit_pct=args.tp,
        stop_loss_pct=args.sl,
        cooldown_seconds=args.cooldown,
        contracts=args.contracts,
        commission_per_contract=args.commission,
        exchange_fee_per_contract=args.exchange_fee,
        regime_filter=not args.no_regime_filter,
    )
    setup_logging(cfg)
    log = logging.getLogger("backtest-run")

    if args.offline and not args.cache:
        log.error("--offline requires --cache")
        return 1

    ib = IB()
    loader = None
    signal = None
    if not args.offline:
        try:
            await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=20)
        except Exception as e:
            log.error("connection failed: %s", e)
            return 1
        signal = make_signal_contract(cfg)
        await ib.qualifyContractsAsync(signal)
        loader = HistoricalLoader(ib, log)

    if args.spot_scale is not None:
        cfg.spot_scale = args.spot_scale

    pricer = OptionPricer(iv=args.iv)
    bt = Backtester(cfg, pricer, log=log)
    bt.strikes = [round(7000 + i * 5.0, 1) for i in range(300)]

    days = trading_days(args.end, args.days)
    cache_hit = False
    cached_days = {}
    if args.cache:
        try:
            cached_days = load_cache(args.cache)
            cache_hit = True
        except (OSError, EOFError, pickle.PickleError) as e:
            log.warning("cache load failed: %s", e)

    for day in days:
        data = cached_days.get(str(day)) if cache_hit else None
        if data is None:
            if args.offline:
                log.warning("day %s not in cache, skipping", day)
                continue
            log.info("fetching %s data for %s", args.data, day)
            if args.data == "tick":
                fetched = await loader.fetch_day_ticks(signal, day, args.max_requests)
                if fetched is None:
                    log.warning("no data for %s", day)
                    continue
                data = ("tick", *fetched)
            elif args.data == "hybrid":
                bars = await loader.fetch_day_bars(signal, day)
                if bars is None:
                    log.warning("no data for %s", day)
                    continue
                extremes = bt.find_extreme_bars(bars)
                log.info("day %s: %d bars, %d extreme minutes need precise ticks", day, len(bars), len(extremes))
                per_minute = await loader.fetch_minute_ticks(signal, extremes, max_requests=30)
                data = ("hybrid", bars, {m.isoformat(): v for m, v in per_minute.items()})
            else:
                bars = await loader.fetch_day_bars(signal, day)
                if bars is None:
                    log.warning("no data for %s", day)
                    continue
                data = ("bar", bars)
            if args.cache:
                cached_days[str(day)] = data
        else:
            log.info("using cached data for %s", day)

        if data[0] == "tick":
            stream, used_ba = merge_to_stream(data[1], data[2])
            log.info("day %s: %d ticks, bid/ask rule=%s", day, len(stream), used_ba)
            bt.run_ticks(stream)
        elif data[0] == "hybrid":
            bars = data[1]
            per_minute = {datetime.fromisoformat(k): v for k, v in data[2].items()}
            precise = minute_deltas(per_minute)
            bt.run_bars(bars, precise_delta=precise)
        else:
            bt.run_bars(data[1])

    if args.cache and not cache_hit and not args.offline:
        save_cache(args.cache, cached_days)
        log.info("saved cache to %s", args.cache)

    if not args.offline:
        ib.disconnect()

    stats = bt.report()
    print_report(stats)
    print_trades(bt.trades)
    if bt.trades:
        save_trades_csv("backtest_trades.csv", bt.trades)
        log.info("trades written to backtest_trades.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))