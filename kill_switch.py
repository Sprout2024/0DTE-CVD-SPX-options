#!/usr/bin/env python3
"""一键清仓 + 停止当天交易。

1) 写入 day-stop 标记文件（默认 data/day_stop.json）：运行中的 main.py 会在
   0.5s 内发现并立刻停止开新仓、停止管理本地持仓；
2) 连接 TWS / IB Gateway，把账户里的期权持仓全部清掉 —— 即使 main.py 已卡死 /
   崩溃，也能直接对 IBKR 下清仓单。

清仓优先用 **IBKR Adaptive 限价单**：先取每条腿的买卖价，BUY 锚定卖一价、
SELL 锚定买一价，由 Adaptive 算法在 (bid, ask) 区间内自动寻找最优成交点；
若超时未成交或无报价，才降级为市价单兜底，保证一定能清掉。

用法：
  .venv/bin/python kill_switch.py --port 4002 --client-id 31
  .venv/bin/python kill_switch.py --all          # 连非 SPX 期权持仓也一起清
  .venv/bin/python kill_switch.py --dry-run      # 演练：不写标记、不下单

注意：命令顺序是先写 day-stop 标记、再下清仓单，让正在运行的策略先停手，
避免两边同时对同一持仓下单造成重复成交。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from ib_insync import IB, LimitOrder, MarketOrder
from ib_insync.objects import TagValue

from config import Config
from logger import setup_logging

_ET = ZoneInfo("America/New_York")
_QUOTE_TIMEOUT = 8.0
_MARKET_FALLBACK_TIMEOUT = 15.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="一键清仓并停止当天交易：写 day-stop 标记 + IBKR 用 Adaptive 限价单清掉所有期权持仓"
    )
    p.add_argument("--host", default="127.0.0.1", help="TWS / IB Gateway host")
    p.add_argument("--port", type=int, default=7497, help="paper=7497, live=7496")
    p.add_argument("--client-id", type=int, default=31, help="unique client id for the API (main.py 已用 30)")
    p.add_argument("--account", default="DUR888412", help="IBKR account id to flatten (empty = default)")
    p.add_argument("--symbol", default="SPX", help="option symbol to flatten (unless --all is given)")
    p.add_argument("--all", action="store_true", help="flatten EVERY option position on the account")
    p.add_argument("--dry-run", action="store_true", help="no flag file, no orders -- only report")
    p.add_argument("--day-stop-file", default="data/day_stop.json", help="flag file the strategy polls")
    p.add_argument("--timeout", type=float, default=15.0,
                   help="seconds to wait for each Adaptive fill before escalating to market")
    p.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ERROR")
    return p.parse_args()


def write_day_stop(path: str, day: str, note: str = "manual kill switch") -> str:
    """Write the day-stop flag that the running strategy polls. Returns ``path``."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    rec = {
        "day": day,
        "ts": datetime.now().astimezone(_ET).isoformat(),
        "note": note,
    }
    with open(path, "w") as f:
        json.dump(rec, f, indent=2)
    return path


async def _wait_fill(trade, timeout: float) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if trade.isDone():
            return trade.orderStatus.status == "Filled"
        await asyncio.sleep(0.05)
    return trade.orderStatus.status == "Filled"


async def _wait_quote(ib: IB, contract, timeout: float = _QUOTE_TIMEOUT) -> Optional[Tuple[float, float]]:
    """Request a quote for ``contract`` and wait for a valid bid/ask. Returns
    ``(bid, ask)`` or None on timeout."""
    ticker = ib.reqMktData(contract, "", False, False)
    quotes = None
    t0 = time.time()
    while time.time() - t0 < timeout:
        bid = getattr(ticker, "bid", float("nan"))
        ask = getattr(ticker, "ask", float("nan"))
        if not math.isnan(bid) and not math.isnan(ask) and bid > 0 and ask > 0:
            quotes = (bid, ask)
            break
        await asyncio.sleep(0.1)
    ib.cancelMktData(contract)
    return quotes


def _adaptive_order(cfg: Config, side: str, quantity: int, limit: float, urgency: str = "Urgent") -> LimitOrder:
    """IBKR Adaptive Limit order: auto-seeks fills between bid/ask.

    For a BUY, the limit is anchored at the ask and Adaptive seeks a fill in
    the (mid, ask) range; for a SELL, anchored at the bid and seeks in the
    (bid, mid) range. ``urgency`` = "Patient" | "Normal" | "Urgent".
    """
    order = LimitOrder(side, quantity, limit)
    order.tif = "DAY"
    order.transmit = True
    order.advancedErrorOverride = "COMBOPAYOUT"
    order.algoStrategy = "Adaptive"
    order.algoParams = [TagValue("adaptivePriority", urgency)]
    if cfg.account:
        order.account = cfg.account
    return order


async def _close_leg(ib: IB, cfg: Config, contract, action: str, qty: int,
                     timeout: float, quote_timeout: float) -> Tuple[bool, str]:
    """Close one option leg, preferring an Adaptive limit anchored to the far
    side of the spread. Returns ``(filled, mode)`` where mode is
    ``"adaptive"`` or ``"market"`` (fallback when no quote / no fill)."""
    log = logging.getLogger("kill_switch")
    quotes = await _wait_quote(ib, contract, quote_timeout)
    if quotes is not None:
        bid, ask = quotes
        limit = ask if action == "BUY" else bid
        order = _adaptive_order(cfg, action, qty, limit)
        trade = ib.placeOrder(contract, order)
        log.info("%s %s x%s @ %.2f (adaptive urgent, bid=%.2f ask=%.2f)",
                 action, contract.localSymbol, qty, limit, bid, ask)
        filled = await _wait_fill(trade, timeout)
        if filled:
            log.info("%s %s x%s filled via adaptive", action, contract.localSymbol, qty)
            return True, "adaptive"
        log.warning("%s %s x%s adaptive not filled in %.0fs, escalating to market",
                    action, contract.localSymbol, qty, timeout)
        if not trade.isDone():
            ib.cancelOrder(order)
    else:
        log.warning("no quote for %s, falling back to market order", contract.localSymbol)

    order = MarketOrder(action, qty)
    order.tif = "DAY"
    if cfg.account:
        order.account = cfg.account
    trade = ib.placeOrder(contract, order)
    log.info("%s %s x%s (market fallback)", action, contract.localSymbol, qty)
    return await _wait_fill(trade, _MARKET_FALLBACK_TIMEOUT), "market"


async def flatten_ib_positions(ib: IB, cfg: Config, flatten_all: bool = False,
                               timeout: float = 15.0,
                               quote_timeout: float = _QUOTE_TIMEOUT) -> List[dict]:
    """Flatten every option position on the account with Adaptive limit orders.

    With ``flatten_all=False`` only positions whose symbol matches
    ``cfg.option_symbol`` (e.g. SPX) are flattened. Returns a list of
    ``{"local_symbol", "action", "quantity", "filled", "mode"}`` records.
    """
    log = logging.getLogger("kill_switch")
    await ib.reqPositionsAsync()
    results: List[dict] = []
    for ibp in ib.positions():
        c = ibp.contract
        if c.secType != "OPT":
            continue
        if not flatten_all and c.symbol != cfg.option_symbol:
            continue
        if cfg.account and ibp.account != cfg.account:
            continue
        if ibp.position == 0:
            continue
        action = "BUY" if ibp.position < 0 else "SELL"
        qty = abs(ibp.position)
        rec = {
            "local_symbol": c.localSymbol or f"{c.symbol} {c.strike} {c.right}",
            "action": action,
            "quantity": qty,
            "filled": False,
            "mode": "adaptive",
        }
        results.append(rec)
        if cfg.dry_run:
            log.info("DRY-RUN %s %s x%s (adaptive limit)", action, rec["local_symbol"], qty)
            continue
        await ib.qualifyContractsAsync(c)
        rec["filled"], rec["mode"] = await _close_leg(ib, cfg, c, action, qty, timeout, quote_timeout)
    return results


async def main(args: argparse.Namespace) -> int:
    cfg = Config(account=args.account, dry_run=args.dry_run, option_symbol=args.symbol)
    cfg.log_file = ""
    cfg.log_level = args.log_level
    setup_logging(cfg)
    log = logging.getLogger("kill_switch")

    ib = IB()
    try:
        await ib.connectAsync(cfg.host, args.port, clientId=args.client_id, timeout=20)
    except Exception as e:
        log.error("connection to IB Gateway failed: %s", e)
        return 1
    log.info("connected to IB Gateway")

    try:
        if not args.dry_run:
            write_day_stop(args.day_stop_file, datetime.now(_ET).date().isoformat())
            log.info("day-stop flag written to %s", args.day_stop_file)

        positions = await flatten_ib_positions(ib, cfg, flatten_all=args.all, timeout=args.timeout)
        filled = sum(1 for r in positions if r["filled"])
        log.info("flatten summary: %d/%d positions closed", filled, len(positions))
        for r in positions:
            status = "FILLED" if r["filled"] else ("DRY" if args.dry_run else "FAILED")
            log.info("  %-8s %s x%s %s (%s)", status, r["action"], r["quantity"],
                     r["local_symbol"], r["mode"])
    finally:
        ib.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
