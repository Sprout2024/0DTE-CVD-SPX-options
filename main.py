from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from ib_insync import IB

from config import Config
from logger import setup_logging
from strategy import Strategy


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SPY 0DTE credit-spread scalper driven by 1-minute CVD divergence."
    )
    p.add_argument("--host", default="127.0.0.1", help="TWS / IB Gateway host")
    p.add_argument("--port", type=int, default=7497, help="paper=7497, live=7496")
    p.add_argument("--client-id", type=int, default=30, help="unique client id for the API")
    p.add_argument("--account", default="U18853178", help="IBKR account id to trade (empty = default)")
    p.add_argument("--dry-run", action="store_true", help="observe only, never place real orders")
    p.add_argument("--market-data-type", type=int, default=1, help="1=realtime, 3=delayed")
    p.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ERROR")
    p.add_argument("--no-log-file", action="store_true", help="disable file logging")
    p.add_argument("--state-file", default="data/cvd_state.jsonl", help="CVD bar / position state file")
    p.add_argument("--signals-file", default="data/signals.jsonl", help="detected signals log file")
    p.add_argument("--trades-file", default="data/trades.jsonl", help="per-trade open/close detail log")
    return p.parse_args()


async def main(args: argparse.Namespace) -> int:
    cfg = Config(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        account=args.account,
        dry_run=args.dry_run,
        market_data_type=args.market_data_type,
        log_level=args.log_level,
        log_file="" if args.no_log_file else "cvd_strategy.log",
        state_file=args.state_file,
        signals_file=args.signals_file,
        trades_file=args.trades_file,
    )
    setup_logging(cfg)
    log = logging.getLogger("main")

    ib = IB()
    try:
        await ib.connectAsync(cfg.host, cfg.port, clientId=cfg.client_id, timeout=20)
    except Exception as e:
        log.error("connection to IB Gateway failed: %s", e)
        return 1
    log.info("connected to IB Gateway")

    strat = Strategy(ib, cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, strat.request_stop)
        except NotImplementedError:
            pass

    try:
        await strat.run()
    finally:
        ib.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))