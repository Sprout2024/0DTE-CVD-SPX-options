from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ib_insync import IB, ComboLeg, Contract, LimitOrder, MarketOrder, Option, Trade

from config import Config
from cvd_engine import Bar, Signal
from options import IronCondor, OptionSelector, Spread
from state_store import CvdStore


@dataclass
class Position:
    id: str
    direction: str
    spread: Spread
    quantity: int
    entry_time: float
    entry_credit: float
    signal_extreme: float
    signal_cvd: float
    tp_target: float
    sl_price: float
    status: str = "OPEN"
    condor: Optional[IronCondor] = None
    close_kind: str = ""
    close_trade: Optional[Trade] = None
    close_time: float = 0.0
    close_price: float = 0.0
    realized_pnl: float = 0.0
    log: List[str] = field(default_factory=list)


class Executor:
    """Places credit-spread entries and manages mechanical exits."""

    def __init__(self, ib: IB, cfg: Config, selector: OptionSelector, store: Optional[CvdStore] = None):
        self.ib = ib
        self.cfg = cfg
        self.selector = selector
        self.store = store
        self.positions: Dict[str, Position] = {}
        self._seq = 0
        self._log = logging.getLogger("executor")

    def has_open_position(self) -> bool:
        return any(p.status == "OPEN" for p in self.positions.values())

    @staticmethod
    def _snap_tick(price: float) -> float:
        """Snap to a valid SPX option tick (0.05 >= $3.00, else 0.10), flooring down."""
        step = 0.05 if price >= 3.00 else 0.10
        snapped = math.floor(round(price / step, 6)) * step
        return round(snapped, 2)

    @staticmethod
    def _tick_step(price: float) -> float:
        return 0.05 if price >= 3.00 else 0.10

    @staticmethod
    def _move_toward(price: float, target: float, step: float) -> float:
        if target > price:
            return round(min(target, price + step), 2)
        return round(max(target, price - step), 2)

    async def _walk_fill(self, combo: Contract, side: str, start: float, target: float,
                         valid_seconds: float, quantity: int) -> Optional[Trade]:
        """Place a limit order at ``start`` and walk 1 tick toward ``target`` every 5s.

        SELL walks down to the bid, BUY walks up to the ask, so the order becomes
        marketable and fills. Returns the filled Trade or None on timeout.
        """
        step = self._tick_step(start)
        price = self._snap_tick(start)
        order = LimitOrder(side, quantity, price)
        order.tif = "DAY"
        order.transmit = True
        order.advancedErrorOverride = "COMBOPAYOUT"
        trade = self.ib.placeOrder(combo, order)
        self._log.info("%s %s @ %.2f, walking toward %.2f (step %.2f)", side, quantity, price, target, step)
        t0 = time.time()
        next_move = t0 + 5.0
        while time.time() - t0 < valid_seconds:
            if trade.isDone():
                break
            if time.time() >= next_move:
                next_move += 5.0
                np_ = self._snap_tick(self._move_toward(price, target, step))
                if np_ != price:
                    price = np_
                    mo = LimitOrder(side, quantity, price)
                    mo.tif = "DAY"
                    mo.transmit = True
                    mo.advancedErrorOverride = "COMBOPAYOUT"
                    if hasattr(self.ib, "modifyOrder"):
                        self.ib.modifyOrder(trade, mo)
                    else:
                        trade.order.lmtPrice = price
                        self.ib.placeOrder(trade.contract, trade.order)
                    self._log.info("walk %s -> %.2f", side, price)
            await asyncio.sleep(0.2)
        if not trade.isDone():
            self.ib.cancelOrder(order)
            self._log.warning("%s order not filled after walk, cancelled (last %.2f)", side, price)
            return None
        if not trade.fills:
            self._log.warning("%s order done without fills", side)
            return None
        return trade

    async def open_position(self, spread: Spread, credit: float, signal: Signal) -> Optional[Position]:
        if credit is None or credit <= 0:
            self._log.warning("no spread mid available, skip entry")
            return None
        bid = self.selector.spread_bid(spread)
        target = bid if bid is not None else credit
        trade = await self._walk_fill(spread.combo, "SELL", credit, target,
                                      self.cfg.entry_valid_seconds, spread.quantity)
        if trade is None:
            self._log.warning("entry not filled, cancelled")
            return None
        avg = sum(f.execution.price for f in trade.fills) / len(trade.fills)
        self._seq += 1
        pos = Position(
            id=f"P{self._seq}",
            direction=spread.direction,
            spread=spread,
            quantity=spread.quantity,
            entry_time=time.time(),
            entry_credit=avg,
            signal_extreme=signal.extreme,
            signal_cvd=signal.cvd_extreme,
            tp_target=round(avg * (1.0 - self.cfg.take_profit_pct), 2),
            sl_price=round(avg * (1.0 + self.cfg.stop_loss_pct), 2),
        )
        self.positions[pos.id] = pos
        if self.store is not None:
            self.store.save_position(pos)
        self._log.info(
            "position %s OPEN %s credit=%.2f tp=%.2f sl=%.2f extreme=%.2f",
            pos.id, pos.direction, avg, pos.tp_target, pos.sl_price, signal.extreme,
        )
        return pos

    async def open_iron_condor(self, condor: IronCondor, credit: float, signal: Signal) -> Optional[Position]:
        if credit is None or credit <= 0:
            self._log.warning("no iron-condor mid available, skip entry")
            return None
        bid = self.selector.iron_condor_bid(condor)
        target = bid if bid is not None else credit
        trade = await self._walk_fill(condor.combo, "SELL", credit, target,
                                      self.cfg.entry_valid_seconds, condor.quantity)
        if trade is None:
            self._log.warning("iron-condor entry not filled, cancelled")
            return None
        avg = sum(f.execution.price for f in trade.fills) / len(trade.fills)
        self._seq += 1
        pos = Position(
            id=f"P{self._seq}",
            direction="range",
            spread=None,
            quantity=condor.quantity,
            entry_time=time.time(),
            entry_credit=avg,
            signal_extreme=signal.extreme,
            signal_cvd=signal.cvd_extreme,
            tp_target=round(avg * (1.0 - self.cfg.iron_condor_take_profit_pct), 2),
            sl_price=round(avg * (1.0 + self.cfg.iron_condor_stop_loss_pct), 2),
            condor=condor,
        )
        self.positions[pos.id] = pos
        if self.store is not None:
            self.store.save_position(pos)
        self._log.info(
            "position %s IRON-CONDOR OPEN credit=%.2f tp=%.2f sl=%.2f",
            pos.id, avg, pos.tp_target, pos.sl_price,
        )
        return pos

    async def restore_position(self, data: dict) -> Optional[Position]:
        if data.get("condor"):
            return await self._restore_condor(data)
        d = data["direction"]
        sym = self.cfg.option_symbol
        short_opt = Option(sym, data["expiry"], data["sell_strike"], data.get("right") or ("P" if d == "bull" else "C"), self.cfg.option_exchange)
        long_opt = Option(sym, data["expiry"], data["buy_strike"], data.get("right") or ("P" if d == "bull" else "C"), self.cfg.option_exchange)
        await self.ib.qualifyContractsAsync(short_opt, long_opt)
        if not short_opt.conId or not long_opt.conId:
            self._log.error("restore: leg qualification failed, cannot restore position")
            return None
        await self.selector._subscribe_legs(short_opt, long_opt)
        combo = Contract()
        combo.symbol = sym
        combo.secType = "BAG"
        combo.currency = self.cfg.currency
        combo.exchange = self.cfg.option_exchange
        combo.comboLegs = [
            ComboLeg(short_opt.conId, 1, "SELL", self.cfg.option_exchange),
            ComboLeg(long_opt.conId, 1, "BUY", self.cfg.option_exchange),
        ]
        spread = Spread(d, combo, short_opt, long_opt, data["expiry"], data["quantity"])
        pos = Position(
            id=data.get("id", f"P{self._seq + 1}"),
            direction=d,
            spread=spread,
            quantity=data["quantity"],
            entry_time=data["entry_time"],
            entry_credit=data["entry_credit"],
            signal_extreme=data["signal_extreme"],
            signal_cvd=data["signal_cvd"],
            tp_target=data["tp_target"],
            sl_price=data["sl_price"],
        )
        self._seq = max(self._seq, int(pos.id.lstrip("P")) if pos.id.startswith("P") else 0)
        self.positions[pos.id] = pos
        self._log.warning(
            "restored position %s %s credit=%.2f tp=%.2f sl=%.2f", pos.id, d, pos.entry_credit, pos.tp_target, pos.sl_price,
        )
        return pos

    async def _restore_condor(self, data: dict) -> Optional[Position]:
        c = data["condor"]
        sym = self.cfg.option_symbol
        ex = self.cfg.option_exchange
        sc = Option(sym, c["expiry"], c["short_call"], "C", ex)
        lc = Option(sym, c["expiry"], c["long_call"], "C", ex)
        sp = Option(sym, c["expiry"], c["short_put"], "P", ex)
        lp = Option(sym, c["expiry"], c["long_put"], "P", ex)
        await self.ib.qualifyContractsAsync(sc, lc, sp, lp)
        if not all((sc.conId, lc.conId, sp.conId, lp.conId)):
            self._log.error("restore condor: leg qualification failed")
            return None
        await self.selector._subscribe_legs4(sc, lc, sp, lp)
        combo = Contract()
        combo.symbol = sym
        combo.secType = "BAG"
        combo.currency = self.cfg.currency
        combo.exchange = ex
        combo.comboLegs = [
            ComboLeg(sc.conId, 1, "SELL", ex),
            ComboLeg(lc.conId, 1, "BUY", ex),
            ComboLeg(sp.conId, 1, "SELL", ex),
            ComboLeg(lp.conId, 1, "BUY", ex),
        ]
        condor = IronCondor(sc, lc, sp, lp, combo, c["expiry"], data["quantity"])
        pos = Position(
            id=data.get("id", f"P{self._seq + 1}"),
            direction="range",
            spread=None,
            quantity=data["quantity"],
            entry_time=data["entry_time"],
            entry_credit=data["entry_credit"],
            signal_extreme=data["signal_extreme"],
            signal_cvd=data["signal_cvd"],
            tp_target=data["tp_target"],
            sl_price=data["sl_price"],
            condor=condor,
        )
        self._seq = max(self._seq, int(pos.id.lstrip("P")) if pos.id.startswith("P") else 0)
        self.positions[pos.id] = pos
        self._log.warning("restored iron-condor %s credit=%.2f", pos.id, pos.entry_credit)
        return pos

    def _combo(self, pos: Position) -> Contract:
        return pos.condor.combo if pos.condor is not None else pos.spread.combo

    def _pos_value(self, pos: Position) -> Optional[float]:
        if pos.condor is not None:
            return self.selector.iron_condor_mid(pos.condor)
        return self.selector.spread_mid(pos.spread)

    async def manage(self, spot: float, last_bar: Optional[Bar]) -> None:
        for pos in list(self.positions.values()):
            if pos.status != "OPEN":
                continue
            if pos.condor is not None:
                await self._manage_condor(pos)
                continue
            mid = self.selector.spread_mid(pos.spread)
            if mid is None:
                continue
            await self._manage_one(pos, mid, spot, last_bar)

    async def _manage_condor(self, pos: Position) -> None:
        if pos.close_trade is not None:
            if pos.close_trade.isDone():
                if pos.close_trade.orderStatus.status == "Filled":
                    self._finalize_close(pos, pos.close_trade)
                else:
                    pos.close_trade = None
            elif time.time() - pos.close_time > self.cfg.close_patience_seconds:
                self.ib.cancelOrder(pos.close_trade.order)
                await self._market_close(pos)
            return
        value = self.selector.iron_condor_mid(pos.condor)
        if value is None:
            return
        if value >= pos.sl_price:
            await self._close(pos, "SL")
            return
        if value <= pos.tp_target:
            await self._close(pos, "TP")
            return

    async def _manage_one(self, pos: Position, mid: float, spot: float, last_bar: Optional[Bar]) -> None:
        if pos.close_trade is not None:
            if pos.close_trade.isDone():
                if pos.close_trade.orderStatus.status == "Filled":
                    self._finalize_close(pos, pos.close_trade)
                else:
                    pos.close_trade = None
            elif time.time() - pos.close_time > self.cfg.close_patience_seconds:
                self.ib.cancelOrder(pos.close_trade.order)
                await self._market_close(pos)
            return

        held = time.time() - pos.entry_time

        if mid >= pos.sl_price:
            await self._close(pos, "SL")
            return
        if mid <= pos.tp_target:
            await self._close(pos, "TP")
            return
        if self._tech_stop_triggered(pos, spot, last_bar):
            await self._close(pos, "TECH")
            return
        if held >= self.cfg.hard_time_seconds:
            await self._close(pos, "TIME")
            return
        if held >= self.cfg.profit_time_seconds and mid < pos.entry_credit:
            await self._close(pos, "TIME")
            return

    def _tech_stop_triggered(self, pos: Position, spot: float, last_bar: Optional[Bar]) -> bool:
        if last_bar is None:
            return False
        buf = self.cfg.tech_stop_buffer
        if pos.direction == "bear":
            return spot > pos.signal_extreme + buf and last_bar.cvd_delta > 0 and last_bar.cvd_close > pos.signal_cvd
        return spot < pos.signal_extreme - buf and last_bar.cvd_delta < 0 and last_bar.cvd_close < pos.signal_cvd

    async def _close(self, pos: Position, kind: str) -> None:
        pos.close_kind = kind
        if kind in ("SL", "TECH"):
            await self._market_close(pos)
            return
        value = self._pos_value(pos)
        if value is None:
            await self._market_close(pos)
            return
        if pos.condor is not None:
            ask = self.selector.iron_condor_ask(pos.condor)
        else:
            ask = self.selector.spread_ask(pos.spread)
        target = ask if ask is not None else value
        trade = await self._walk_fill(self._combo(pos), "BUY", value, target,
                                      self.cfg.close_patience_seconds * 4, pos.quantity)
        if trade is None:
            self._log.warning("position %s %s close not filled after walk, escalating", pos.id, kind)
            await self._market_close(pos)
            return
        self._finalize_close(pos, trade)

    async def _market_close(self, pos: Position) -> None:
        value = self._pos_value(pos)
        if value is not None:
            price = self._snap_tick(value)
            order = LimitOrder("BUY", pos.quantity, price)
            order.tif = "DAY"
            order.advancedErrorOverride = "COMBOPAYOUT"
            trade = self.ib.placeOrder(self._combo(pos), order)
            self._log.info("position %s close %s @ mid %.2f", pos.id, pos.close_kind or "MANUAL", price)
            ok = await self._wait(trade, 8)
            if ok:
                self._finalize_close(pos, trade)
                return
            if not trade.isDone():
                self.ib.cancelOrder(order)
            self._log.warning("position %s mid close not filled, escalating to market", pos.id)
        order = MarketOrder("BUY", pos.quantity)
        order.tif = "DAY"
        order.advancedErrorOverride = "COMBOPAYOUT"
        trade = self.ib.placeOrder(self._combo(pos), order)
        self._log.info("position %s market close %s", pos.id, pos.close_kind or "MANUAL")
        ok = await self._wait(trade, 15)
        if ok:
            self._finalize_close(pos, trade)
        else:
            self._log.error("position %s market close failed", pos.id)

    def _finalize_close(self, pos: Position, trade: Trade) -> None:
        if not trade.fills:
            self._log.error("close trade %s has no fills", pos.id)
            return
        avg = sum(f.execution.price for f in trade.fills) / len(trade.fills)
        pos.close_price = avg
        pos.realized_pnl = (pos.entry_credit - avg) * pos.quantity * 100.0
        pos.status = "CLOSED"
        held = time.time() - pos.entry_time
        self._log.info(
            "position %s CLOSED %s credit=%.2f close=%.2f pnl=%.2f held=%.0fs",
            pos.id, pos.close_kind, pos.entry_credit, avg, pos.realized_pnl, held,
        )
        if pos.condor is not None:
            for t in self.selector.iron_condor_legs(pos.condor):
                self.ib.cancelMktData(t)
        else:
            for t in (pos.spread.short_leg, pos.spread.long_leg):
                self.ib.cancelMktData(t)
        if self.store is not None:
            self.store.clear_position()

    async def _wait(self, trade: Trade, timeout: float) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if trade.isDone():
                return trade.orderStatus.status == "Filled"
            await asyncio.sleep(0.05)
        return trade.orderStatus.status == "Filled"

    async def force_close_all(self) -> None:
        for pos in self.positions.values():
            if pos.status != "OPEN":
                continue
            if pos.close_trade is not None and not pos.close_trade.isDone():
                self.ib.cancelOrder(pos.close_trade.order)
            if pos.close_trade is None or not pos.close_trade.isDone():
                await self._market_close(pos)