from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import datetime, time as dtime
from typing import List, Optional
from zoneinfo import ZoneInfo

from ib_insync import Contract, IB, MarketOrder, Ticker

from config import Config
from contracts import make_signal_contract
from cvd_engine import Bar, CvdEngine, Signal
from executor import Executor
from options import OptionSelector
from state_store import CvdStore
from trend import TrendDetector

_IB_ON_ERROR_PATCHED = False


def _patch_ib_on_error() -> None:
    """ib_insync auto-resubscribes account summary on error 1102 (reconnect);
    each reconnect stacks a new subscription and IB rejects it with
    Error 322 (max account summary requests exceeded). We never use account
    summary, so suppress the auto-resubscribe."""
    global _IB_ON_ERROR_PATCHED
    if _IB_ON_ERROR_PATCHED:
        return
    orig = IB._onError

    def patched(self, reqId, errorCode, errorString, contract):
        if errorCode == 1102:
            return
        return orig(self, reqId, errorCode, errorString, contract)

    IB._onError = patched
    _IB_ON_ERROR_PATCHED = True


_patch_ib_on_error()

EASTERN = None


def _eastern():
    global EASTERN
    if EASTERN is None:
        EASTERN = ZoneInfo("America/New_York")
    return EASTERN


class Strategy:
    def __init__(self, ib: IB, cfg: Config):
        self.ib = ib
        self.cfg = cfg
        self.engine = CvdEngine(cfg)
        self.trend = TrendDetector(cfg)
        self.selector = OptionSelector(ib, cfg)
        self.store = CvdStore(cfg.state_file)
        self.executor = Executor(ib, cfg, self.selector, store=self.store)
        self.signal: Optional[Contract] = None
        self.option_underlying: Optional[Contract] = None
        self.spot_ticker: Optional[Ticker] = None
        self.index_ticker: Optional[Ticker] = None
        self._bid: Optional[float] = None
        self._ask: Optional[float] = None
        self._last_spot: Optional[float] = None
        self._index_spot: Optional[float] = None
        self._last_signal: Optional[Signal] = None
        self._entry_task: Optional[asyncio.Task] = None
        self._last_breakout_ts = 0.0
        self._session_day = None
        self._last_entry_ts = 0.0
        self.running = True
        self._tickers_subscribed = False
        self._log = logging.getLogger("strategy")

    async def setup(self) -> None:
        self.selector.reset()
        if not self._tickers_subscribed:
            self.ib.pendingTickersEvent += self._on_pending_tickers
            self._tickers_subscribed = True
        self.signal = make_signal_contract(self.cfg)
        await self.ib.qualifyContractsAsync(self.signal)
        if self.cfg.option_symbol == self.cfg.symbol:
            self.option_underlying = self.signal
        else:
            self.option_underlying = Contract(
                symbol=self.cfg.option_symbol,
                secType=self.cfg.option_sec_type,
                exchange=self.cfg.option_exchange,
                currency=self.cfg.currency,
            )
            await self.ib.qualifyContractsAsync(self.option_underlying)
        self.ib.reqMarketDataType(self.cfg.market_data_type)
        self.ib.pendingTickersEvent += self._on_pending_tickers
        self.spot_ticker = self.ib.reqMktData(self.signal, "", False, False)
        self.index_ticker = self.ib.reqMktData(self.option_underlying, "", False, False)
        await self.selector.init(self.option_underlying)

        t0 = time.time()
        while time.time() - t0 < 10 and not self._has_spot_quote():
            await asyncio.sleep(0.5)
        if not self._has_spot_quote() and self.cfg.market_data_type == 1:
            self._log.warning("no realtime quotes, falling back to delayed data")
            self.ib.reqMarketDataType(3)
            t0 = time.time()
            while time.time() - t0 < 10 and not self._has_spot_quote():
                await asyncio.sleep(0.5)
        self._log.info("setup complete, spot=%s", self._last_spot)

    def _has_spot_quote(self) -> bool:
        return self._last_spot is not None

    def _on_pending_tickers(self, tickers: List[Ticker]) -> None:
        for t in tickers:
            if t.contract == self.signal:
                self._feed_signal(t)
            if self.option_underlying is not self.signal and t.contract == self.option_underlying:
                self._feed_index(t)
            self.selector.on_ticker(t)

    def _feed_index(self, ticker: Ticker) -> None:
        bid = getattr(ticker, "bid", float("nan"))
        ask = getattr(ticker, "ask", float("nan"))
        last = getattr(ticker, "last", float("nan"))
        for tick in getattr(ticker, "ticks", []):
            if tick.tickType == 4 and not math.isnan(tick.price):
                last = tick.price
            elif tick.tickType == 1:
                bid = tick.price
            elif tick.tickType == 2:
                ask = tick.price
        if not math.isnan(last) and last > 0:
            self._index_spot = float(last)
        elif not math.isnan(bid) and not math.isnan(ask) and bid > 0 and ask > 0:
            self._index_spot = (bid + ask) / 2.0

    def _option_spot(self) -> Optional[float]:
        """Index spot used to pick/price option legs (SPX level, or SPY x scale as fallback)."""
        if self._index_spot is not None:
            return self._index_spot
        if self._last_spot is None:
            return None
        if self.cfg.option_symbol == self.cfg.symbol:
            return self._last_spot
        return self._last_spot * self.cfg.spot_scale

    def _tick_in_rth(self, ts) -> bool:
        """True if the tick's timestamp falls inside US RTH (09:30-16:00 ET)."""
        if ts is None:
            return True
        if ts.tzinfo is not None:
            ts = ts.astimezone(_eastern())
        return self.in_trading_hours(ts)

    def _feed_signal(self, ticker: Ticker) -> None:
        for tick in ticker.ticks:
            if tick.tickType == 1:
                self._bid = tick.price
            elif tick.tickType == 2:
                self._ask = tick.price
        for tick in ticker.ticks:
            if tick.tickType == 4 and tick.size > 0:
                price = float(tick.price)
                size = float(tick.size)
                bid, ask = self._bid, self._ask
                if bid is None or ask is None or math.isnan(bid) or math.isnan(ask):
                    continue
                if price >= ask:
                    delta = size
                elif price <= bid:
                    delta = -size
                else:
                    delta = 0.0
                self._last_spot = price
                if not self._tick_in_rth(tick.time):
                    continue
                bar = self.engine.add_trade(price, size, delta, tick.time)
                if bar is not None:
                    bar.iv = round(self.engine.realized_iv(), 4)
                    self.store.append_bar(bar)
                    self._on_new_bar(bar)

    def _on_new_bar(self, bar: Bar) -> None:
        self.trend.update(bar)
        # Record CVD divergence signals for analysis (does not drive entry).
        signal = self.engine.detect_signal()
        if signal is not None:
            self._record_signal(signal)
        regime = self.trend.regime()
        if regime in ("up", "down"):
            bo = self.engine.detect_breakout()
            if bo is not None and time.time() - self._last_breakout_ts >= self.cfg.breakout_cooldown:
                self._record_breakout(bo, bar)
                self._last_breakout_ts = time.time()
        # Backtest-replicating entry: open an iron condor once the 30-min
        # window regime is "range" (and filters allow). Re-enter after each
        # close + cooldown until reaching max_position.
        if self.executor.open_count() >= self.cfg.max_position:
            return
        if self.executor.sl_limit_reached():
            self._log.info("entry skipped (daily SL limit reached)")
            return
        if self._entry_task is not None and not self._entry_task.done():
            return
        if not self.in_trading_hours():
            return
        if not self.open_vol_allows():
            self._log.info("entry skipped (open-30min volatility filter)")
            return
        if not self.prev_day_trend_allows():
            self._log.info("entry skipped (prior-day trend filter)")
            return
        if not self._cooldown_elapsed():
            return
        if self._regime_30min() != "range":
            return
        self._log.info(
            "30min-range entry triggered bar=%s", bar.ts,
        )
        self._entry_task = asyncio.ensure_future(self._execute_range_entry(bar))

    def _record_breakout(self, bo: dict, bar: Bar) -> None:
        """Append a CVD strong-momentum breakout signal (trend regime) to the signals file."""
        import json

        c1, c2, c3, c4 = bo["conditions"]
        rec = {
            "ts": bar.ts.isoformat(),
            "type": "breakout",
            "direction": bo["direction"],
            "regime": self.trend.regime(),
            "conditions": bo["count"],
            "c1_abs_highlow": bool(c1),
            "c2_slope": bool(c2),
            "c3_comove": bool(c3),
            "c4_delta_spike": bool(c4),
        }
        path = self.cfg.signals_file
        if path:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")

    def _record_signal(self, signal: Signal) -> None:
        """Append every detected signal to the signals file for later analysis."""
        import json

        rec = {
            "ts": signal.bar.ts.isoformat(),
            "direction": signal.direction,
            "regime": self.trend.regime(),
            "extreme": signal.extreme,
            "cvd_extreme": signal.cvd_extreme,
        }
        path = self.cfg.signals_file
        if path:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")

    def _on_signal(self, signal: Signal) -> None:
        if self.executor.has_open_position():
            return
        if self._entry_task is not None and not self._entry_task.done():
            return
        if self._last_signal is not None and abs(signal.extreme - self._last_signal.extreme) < self.cfg.min_signal_spacing:
            return
        if not self.entry_allowed():
            return
        if not self.open_vol_allows():
            self._log.info("signal %s skipped (open-30min volatility filter)", signal.direction)
            return
        if not self.prev_day_trend_allows():
            self._log.info("signal %s skipped (prior-day trend filter)", signal.direction)
            return
        self._last_signal = signal
        self._log.info(
            "signal %s bar=%s extreme=%.2f cvd_extreme=%.2f",
            signal.direction, signal.bar.ts, signal.extreme, signal.cvd_extreme,
        )
        self._entry_task = asyncio.ensure_future(self._execute_signal(signal))

    async def _execute_signal(self, signal: Signal) -> None:
        try:
            spot = self._option_spot()
            if spot is None:
                return
            regime = self.trend.regime()
            if self.cfg.regime_filter:
                if regime == "range":
                    condor = await self.selector.build_iron_condor(spot)
                    if condor is None:
                        self._log.warning("no iron condor built for signal")
                        return
                    credit = self.selector.iron_condor_mid(condor)
                    if credit is None:
                        self._log.warning("no iron-condor quote available")
                        return
                    if credit < self.cfg.min_entry_credit or credit > self.cfg.max_entry_credit:
                        self._log.warning("condor credit %.2f outside [%.2f, %.2f]", credit, self.cfg.min_entry_credit, self.cfg.max_entry_credit)
                        return
                    pos = await self.executor.open_iron_condor(condor, credit, signal)
                    if pos is None:
                        self._log.warning("iron condor entry failed")
                    return
                self._log.info("signal %s skipped (trend %s, no trade)", signal.direction, regime)
                return
            spread = await self.selector.select_spread(signal.direction, spot)
            if spread is None:
                self._log.warning("no spread selected for signal")
                return
            credit = self.selector.spread_mid(spread)
            if credit is None:
                self._log.warning("no spread quote available")
                return
            if credit < self.cfg.min_entry_credit or credit > self.cfg.max_entry_credit:
                self._log.warning("credit %.2f outside [%.2f, %.2f]", credit, self.cfg.min_entry_credit, self.cfg.max_entry_credit)
                return
            pos = await self.executor.open_position(spread, credit, signal)
            if pos is None:
                self._log.warning("spread entry failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log.exception("execute signal failed: %s", e)

    async def _execute_range_entry(self, bar: Bar) -> None:
        """Backtest-replicating entry: build an iron condor at the current spot
        and open it. Called when the 30-min regime is range and cooldown passed."""
        try:
            spot = self._option_spot()
            if spot is None:
                self._log.warning("range entry: no option spot available")
                return
            condor = await self.selector.build_iron_condor(spot)
            if condor is None:
                self._log.warning("range entry: no iron condor built")
                return
            credit = self.selector.iron_condor_mid(condor)
            if credit is None:
                self._log.warning("range entry: no iron-condor quote available")
                return
            if credit < self.cfg.min_entry_credit or credit > self.cfg.max_entry_credit:
                self._log.warning("range entry: condor credit %.2f outside [%.2f, %.2f]",
                                  credit, self.cfg.min_entry_credit, self.cfg.max_entry_credit)
                return
            sig = Signal(direction="range", bar=bar,
                         extreme=spot, cvd_extreme=0.0)
            pos = await self.executor.open_iron_condor(condor, credit, sig)
            if pos is None:
                self._log.warning("range entry: iron condor entry failed")
            else:
                self._mark_entry()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log.exception("range entry failed: %s", e)

    def _shift(self, t: dtime, minutes: int) -> dtime:
        """Shift a time-of-day by +/- minutes (helper for no-trade windows)."""
        from datetime import timedelta

        base = datetime(2000, 1, 1, t.hour, t.minute, t.second)
        return (base + timedelta(minutes=minutes)).time()

    def in_trading_hours(self, now: Optional[datetime] = None) -> bool:
        if now is None:
            now = datetime.now(_eastern())
        if now.weekday() >= 5:
            return False
        if now.time() < self.cfg.session_start or now.time() > self.cfg.session_end:
            return False
        return True

    def entry_allowed(self, now: Optional[datetime] = None) -> bool:
        """New entries only after the open ramp and before the close window."""
        if now is None:
            now = datetime.now(_eastern())
        if not self.in_trading_hours(now):
            return False
        if now.time() < self._shift(self.cfg.session_start, self.cfg.no_trade_first_minutes):
            return False
        if now.time() >= self._shift(self.cfg.session_end, -self.cfg.no_trade_last_minutes):
            return False
        return True

    def _regime_30min(self) -> str:
        """1-minute-window range gate (mirrors the backtest): returns
        'range' if the last ``regime_min_window`` closes have |slope| <=
        ``regime_min_slope`` (points/min), else 'up'/'down', or 'none' if
        there isn't enough data yet."""
        from statistics import linear_regression

        lb = self.cfg.regime_min_window
        closes = [b.close for b in self.engine.bars]
        w = closes[-lb:]
        if len(w) < lb:
            return "none"
        slope, _ = linear_regression(list(range(len(w))), w)
        if abs(slope) <= self.cfg.regime_min_slope:
            return "range"
        return "up" if slope > 0 else "down"

    def _cooldown_elapsed(self) -> bool:
        """True once the 10-min entry cooldown has elapsed since the last entry.
        Before the first entry of the day, cooldown is satisfied."""
        if self._last_entry_ts == 0.0:
            return True
        return time.time() - self._last_entry_ts >= self.cfg.cooldown_seconds

    def _mark_entry(self) -> None:
        self._last_entry_ts = time.time()

    def open_vol_allows(self) -> bool:
        """Open-30min volatility filter: skip the day if the first N minutes'
        high-low range (in option points) exceeds the configured threshold."""
        thresh = self.cfg.open_vol_filter
        if thresh <= 0:
            return True
        win_min = self.cfg.open_vol_window_minutes
        cutoff = self._shift(self.cfg.session_start, win_min)
        early = []
        for b in self.engine.bars:
            t = b.ts
            if t.tzinfo is not None:
                t = t.astimezone(_eastern())
            if t.time() <= cutoff:
                early.append(b)
        if not early:
            return True  # still before/inside the window, allow (not yet decided)
        hi = max(b.high for b in early)
        lo = min(b.low for b in early)
        scale = self.cfg.spot_scale if self.cfg.option_symbol != self.cfg.symbol else 1.0
        return (hi - lo) / scale <= thresh

    def prev_day_trend_allows(self) -> bool:
        """Prior-day trend filter: read the open/skip decision from
        data/trade_decision.json for today's trade_day. If the file is missing
        or has no entry for today, generate it by running trade_decision.py."""
        thresh = self.cfg.prev_day_trend_filter
        if thresh <= 0:
            return True
        today = datetime.now(_eastern()).date().isoformat()
        decision = self._read_trade_decision(today)
        if decision is None:
            self._generate_trade_decision()
            decision = self._read_trade_decision(today)
        if decision is None:
            self._log.warning("prev-day trend filter: no decision for %s, allowing", today)
            return True
        self._log.info("prev-day trend filter: trade_day=%s decision=%s", today, decision)
        return decision == "open"

    def _trade_decision_path(self) -> str:
        return os.path.join(os.path.dirname(self.cfg.state_file), "trade_decision.json")

    def _read_trade_decision(self, trade_day: str):
        """Return the decision ('open'/'skip'/'unknown') for ``trade_day`` from
        data/trade_decision.json, or None if not present."""
        import json

        path = self._trade_decision_path()
        if not os.path.exists(path):
            return None
        try:
            data = json.load(open(path))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return None
        for rec in data:
            if rec.get("trade_day") == trade_day:
                return rec.get("decision")
        return None

    def _generate_trade_decision(self) -> None:
        """Generate today's trade decision by running trade_decision.py."""
        import subprocess
        import sys

        today = datetime.now(_eastern()).date().isoformat()
        try:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_decision.py")
            subprocess.run(
                [sys.executable, script, "--date", today],
                capture_output=True, timeout=30,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            self._log.info("prev-day trend filter: generated trade_decision.json")
        except Exception as e:
            self._log.warning("prev-day trend filter: failed to generate decision: %s", e)

    async def _restore_state(self) -> None:
        """Replay today's persisted bars into a fresh engine and rebuild open positions."""
        day = datetime.now(_eastern()).date()
        self._session_day = day
        self.executor.update_session_day(day)
        bars, pos_list = self.store.load(day)
        if bars:
            self.engine = CvdEngine(self.cfg)
            self.trend.reset()
            for b in bars:
                self.engine.ingest_bar(b)
                self.trend.update(b)
            self._log.info(
                "restored %d bars, running cvd=%.2f", len(bars), self.engine.running_delta()
            )
        for pd in pos_list:
            await self.executor.restore_position(pd)
        await self._sync_ib_positions()

    async def _sync_ib_positions(self) -> None:
        """Close any option position on IBKR that we are not tracking (orphan from a hard kill)."""
        await self.ib.reqPositionsAsync()
        tracked = set()
        for p in self.executor.positions.values():
            if p.condor is not None:
                for leg in (p.condor.short_call, p.condor.long_call,
                            p.condor.short_put, p.condor.long_put):
                    tracked.add(leg.conId)
            else:
                tracked.add(p.spread.short_leg.conId)
                tracked.add(p.spread.long_leg.conId)
        for ibp in self.ib.positions():
            c = ibp.contract
            if c.secType != "OPT" or c.symbol != self.cfg.option_symbol:
                continue
            if c.conId in tracked:
                continue
            action = "BUY" if ibp.position < 0 else "SELL"
            order = MarketOrder(action, abs(ibp.position))
            order.tif = "DAY"
            self.ib.placeOrder(c, order)
            self._log.warning(
                "flattening orphan position %s %s x%s", c.localSymbol, action, abs(ibp.position)
            )

    async def _reconnect(self) -> None:
        self._log.error("connection lost, attempting reconnect")
        ok = False
        while self.running and not ok:
            try:
                await self.ib.connectAsync(
                    self.cfg.host, self.cfg.port, clientId=self.cfg.client_id, timeout=20
                )
                ok = True
            except Exception as e:
                self._log.error("reconnect failed: %s", e)
                await asyncio.sleep(self.cfg.reconnect_retry_seconds)
        if ok:
            await self.setup()
            await self._restore_state()
            self._log.info("reconnected and state restored")

    async def run(self) -> None:
        await self.setup()
        await self._restore_state()
        self._log.info("strategy running")
        try:
            while self.running:
                await asyncio.sleep(0.5)
                if not self.ib.isConnected():
                    await self._reconnect()
                    continue
                now_day = datetime.now(_eastern()).date()
                if self._session_day is not None and now_day != self._session_day:
                    self._session_day = now_day
                    self._last_entry_ts = 0.0
                self.executor.update_session_day(now_day)
                if not self.in_trading_hours():
                    if self.executor.has_open_position():
                        self._log.warning("outside trading hours with open position, closing")
                        await self.executor.force_close_all()
                    continue
                spot = self._last_spot
                if spot is None:
                    continue
                had_open = self.executor.has_open_position()
                await self.executor.manage(spot, self.engine.last_bar())
                if had_open and not self.executor.has_open_position():
                    self._last_signal = None
        finally:
            self._log.info("shutting down")
            if self._entry_task is not None and not self._entry_task.done():
                self._entry_task.cancel()
            await self.executor.force_close_all()
            await self.selector.cleanup()

    def request_stop(self) -> None:
        self.running = False