from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from ib_insync import ComboLeg, Contract, IB, Option, Stock, Ticker

from config import Config

_ET = ZoneInfo("America/New_York")


@dataclass
class Spread:
    direction: str
    combo: Contract
    short_leg: Option
    long_leg: Option
    expiry: str
    quantity: int = 1


@dataclass
class IronCondor:
    short_call: Option
    long_call: Option
    short_put: Option
    long_put: Option
    combo: Contract
    expiry: str
    quantity: int = 1


class OptionSelector:
    """Selects 0DTE credit-spread legs on the option underlying and prices them from live quotes.

    The underlying price signal (CVD) comes from the equity/futures instrument in ``cfg.symbol``;
    the option contracts are built on ``cfg.option_symbol`` (e.g. SPX index options).
    """

    def __init__(self, ib: IB, cfg: Config):
        self.ib = ib
        self.cfg = cfg
        self.chain = None
        self.expiry: Optional[str] = None
        self._live_tickers: Dict[int, Ticker] = {}

    def reset(self) -> None:
        self._live_tickers.clear()
        self.chain = None
        self.expiry = None

    async def init(self, underlying: Contract) -> None:
        sec_type = getattr(underlying, "secType", None) or "STK"
        chains = await self.ib.reqSecDefOptParamsAsync(
            self.cfg.option_symbol, "", sec_type, underlying.conId
        )
        self.chain = self._best_chain(chains)
        if self.chain is None:
            raise RuntimeError(f"option chain for {self.cfg.option_symbol} not found")

    def _best_chain(self, chains) -> Optional[OptionChain]:
        """Prefer the chain that offers the earliest expiry (0DTE weekly).

        For index options the weekly (e.g. SPXW) and monthly (e.g. SPX) chains
        are returned separately; the 0DTE trade needs the weekly one.
        """
        today = datetime.now(_ET).date()
        best = None
        best_exp = None
        for ch in chains:
            if not ch.tradingClass.startswith(self.cfg.option_symbol):
                continue
            try:
                exps = sorted(datetime.strptime(e, "%Y%m%d").date() for e in ch.expirations)
            except (ValueError, TypeError):
                continue
            future = [d for d in exps if d >= today]
            if not future:
                continue
            if best is None or future[0] < best_exp:
                best, best_exp = ch, future[0]
        return best

    def _pick_expiry(self) -> Optional[str]:
        # Use US Eastern date so we pick today's 0DTE expiry even when the local
        # clock (e.g. UTC+8) is already past midnight but ET is still yesterday.
        today = datetime.now(_ET).date()
        exps = sorted(
            (datetime.strptime(e, "%Y%m%d").date(), e)
            for e in self.chain.expirations
            if datetime.strptime(e, "%Y%m%d").date() >= today
        )
        if not exps:
            return None
        return exps[0][1]

    async def select_spread(self, direction: str, spot: float) -> Optional[Spread]:
        expiry = self._pick_expiry()
        if expiry is None:
            return None
        self.expiry = expiry
        right = "C" if direction == "bear" else "P"
        band = self.cfg.strike_band
        strikes = sorted(s for s in self.chain.strikes if spot - band <= s <= spot + band)
        if not strikes:
            return None

        opts = [Option(self.cfg.option_symbol, expiry, s, right, self.cfg.exchange) for s in strikes]
        await self.ib.qualifyContractsAsync(*opts)
        tickers = await self._snapshot_greeks(opts)
        delta_by_strike: Dict[float, float] = {}
        for t in tickers:
            g = t.modelGreeks
            if g is not None and g.delta is not None and not math.isnan(g.delta):
                delta_by_strike[t.contract.strike] = g.delta

        sell_strike = self._pick_sell_strike(strikes, delta_by_strike, direction)
        if sell_strike is None:
            sell_strike = self._fallback_strike(strikes, spot, direction)
        if sell_strike is None:
            return None

        offset = self.cfg.spread_width if direction == "bear" else -self.cfg.spread_width
        long_strike = self._nearest_strike(sell_strike + offset)
        if long_strike is None or long_strike == sell_strike:
            return None

        short_opt = next(o for o in opts if o.strike == sell_strike)
        long_opt = Option(self.cfg.option_symbol, expiry, long_strike, right, self.cfg.exchange)
        await self.ib.qualifyContractsAsync(long_opt)
        await self._subscribe_legs(short_opt, long_opt)

        combo = Contract()
        combo.symbol = self.cfg.option_symbol
        combo.secType = "BAG"
        combo.currency = self.cfg.currency
        combo.exchange = self.cfg.exchange
        combo.comboLegs = [
            ComboLeg(conId=short_opt.conId, ratio=1, action="SELL", exchange=self.cfg.exchange),
            ComboLeg(conId=long_opt.conId, ratio=1, action="BUY", exchange=self.cfg.exchange),
        ]
        return Spread(direction, combo, short_opt, long_opt, expiry)

    def _pick_sell_strike(self, strikes: List[float], delta_by_strike: Dict[float, float], direction: str) -> Optional[float]:
        lo = self.cfg.target_delta - self.cfg.delta_tolerance
        hi = self.cfg.target_delta + self.cfg.delta_tolerance
        best = None
        for s in strikes:
            d = delta_by_strike.get(s)
            if d is None:
                continue
            ad = abs(d)
            if ad < lo or ad > hi:
                continue
            if direction == "bear" and d <= 0:
                continue
            if direction == "bull" and d >= 0:
                continue
            score = abs(ad - self.cfg.target_delta)
            if best is None or score < best[0]:
                best = (score, s)
        return best[1] if best else None

    def _fallback_strike(self, strikes: List[float], spot: float, direction: str) -> Optional[float]:
        if direction == "bear":
            return next((s for s in strikes if s > spot), None)
        return next((s for s in reversed(strikes) if s < spot), None)

    def _nearest_strike(self, strike: float) -> Optional[float]:
        if not self.chain:
            return None
        near = min(self.chain.strikes, key=lambda s: abs(s - strike))
        return near if abs(near - strike) <= self.cfg.strike_tolerance else None

    async def _snapshot_greeks(self, opts: List[Option]) -> List[Ticker]:
        tickers = [self.ib.reqMktData(o, "106", False, False) for o in opts]
        deadline = time.time() + self.cfg.greeks_timeout
        while time.time() < deadline:
            if all(t.modelGreeks is not None for t in tickers):
                break
            await asyncio.sleep(0.1)
        for t in tickers:
            self.ib.cancelMktData(t.contract)
        return tickers

    async def _subscribe_legs(self, short_opt: Option, long_opt: Option) -> None:
        for o in (short_opt, long_opt):
            if o.conId not in self._live_tickers:
                self._live_tickers[o.conId] = self.ib.reqMktData(o, "", False, False)
        deadline = time.time() + self.cfg.quote_wait_seconds
        while time.time() < deadline:
            ts = self._live_tickers.get(short_opt.conId)
            tl = self._live_tickers.get(long_opt.conId)
            if ts and tl and self._has_bid_ask(ts) and self._has_bid_ask(tl):
                return
            await asyncio.sleep(0.1)

    def on_ticker(self, ticker: Ticker) -> None:
        cid = getattr(getattr(ticker, "contract", None), "conId", None)
        if cid in self._live_tickers:
            self._live_tickers[cid] = ticker

    @staticmethod
    def _has_bid_ask(t: Ticker) -> bool:
        bid = getattr(t, "bid", float("nan"))
        ask = getattr(t, "ask", float("nan"))
        return not math.isnan(bid) and not math.isnan(ask) and bid > 0 and ask > 0

    def _spread_quotes(self, spread: Spread) -> Optional[tuple]:
        ts = self._live_tickers.get(spread.short_leg.conId)
        tl = self._live_tickers.get(spread.long_leg.conId)
        if not ts or not tl or not self._has_bid_ask(ts) or not self._has_bid_ask(tl):
            return None
        return ts.bid, ts.ask, tl.bid, tl.ask

    def spread_bid(self, spread: Spread) -> Optional[float]:
        q = self._spread_quotes(spread)
        if q is None:
            return None
        sb, sa, lb, la = q
        return round(sb - la, 2)

    def spread_ask(self, spread: Spread) -> Optional[float]:
        q = self._spread_quotes(spread)
        if q is None:
            return None
        sb, sa, lb, la = q
        return round(sa - lb, 2)

    def spread_mid(self, spread: Spread) -> Optional[float]:
        q = self._spread_quotes(spread)
        if q is None:
            return None
        sb, sa, lb, la = q
        s_mid = (sb + sa) / 2.0
        l_mid = (lb + la) / 2.0
        return round(s_mid - l_mid, 2)

    async def build_iron_condor(self, spot: float) -> Optional[IronCondor]:
        expiry = self._pick_expiry()
        if expiry is None:
            return None
        self.expiry = expiry
        off = self.cfg.iron_condor_offset
        wing = self.cfg.iron_condor_wing
        sc_k = self._nearest_strike(spot + off)
        lc_k = self._nearest_strike(spot + off + wing)
        sp_k = self._nearest_strike(spot - off)
        lp_k = self._nearest_strike(spot - off - wing)
        if not all((sc_k, lc_k, sp_k, lp_k)) or sc_k == lc_k or sp_k == lp_k:
            return None
        ex = self.cfg.exchange
        sc = Option(self.cfg.option_symbol, expiry, sc_k, "C", ex)
        lc = Option(self.cfg.option_symbol, expiry, lc_k, "C", ex)
        sp = Option(self.cfg.option_symbol, expiry, sp_k, "P", ex)
        lp = Option(self.cfg.option_symbol, expiry, lp_k, "P", ex)
        await self.ib.qualifyContractsAsync(sc, lc, sp, lp)
        if not all((sc.conId, lc.conId, sp.conId, lp.conId)):
            return None
        await self._subscribe_legs4(sc, lc, sp, lp)
        combo = Contract()
        combo.symbol = self.cfg.option_symbol
        combo.secType = "BAG"
        combo.currency = self.cfg.currency
        combo.exchange = ex
        combo.comboLegs = [
            ComboLeg(conId=sc.conId, ratio=1, action="SELL", exchange=ex),
            ComboLeg(conId=lc.conId, ratio=1, action="BUY", exchange=ex),
            ComboLeg(conId=sp.conId, ratio=1, action="SELL", exchange=ex),
            ComboLeg(conId=lp.conId, ratio=1, action="BUY", exchange=ex),
        ]
        return IronCondor(sc, lc, sp, lp, combo, expiry)

    async def _subscribe_legs4(self, sc: Option, lc: Option, sp: Option, lp: Option) -> None:
        for o in (sc, lc, sp, lp):
            if o.conId not in self._live_tickers:
                self._live_tickers[o.conId] = self.ib.reqMktData(o, "", False, False)
        deadline = time.time() + self.cfg.quote_wait_seconds
        while time.time() < deadline:
            if all(self._has_bid_ask(self._live_tickers.get(o.conId)) for o in (sc, lc, sp, lp)):
                return
            await asyncio.sleep(0.1)

    def iron_condor_mid(self, condor: IronCondor) -> Optional[float]:
        qs = [self._live_tickers.get(o.conId) for o in (condor.short_call, condor.long_call, condor.short_put, condor.long_put)]
        if any(t is None or not self._has_bid_ask(t) for t in qs):
            return None
        sc_mid = (qs[0].bid + qs[0].ask) / 2.0
        lc_mid = (qs[1].bid + qs[1].ask) / 2.0
        sp_mid = (qs[2].bid + qs[2].ask) / 2.0
        lp_mid = (qs[3].bid + qs[3].ask) / 2.0
        return round((sc_mid - lc_mid) + (sp_mid - lp_mid), 2)

    def iron_condor_bid(self, condor: IronCondor) -> Optional[float]:
        qs = [self._live_tickers.get(o.conId) for o in (condor.short_call, condor.long_call, condor.short_put, condor.long_put)]
        if any(t is None or not self._has_bid_ask(t) for t in qs):
            return None
        return round((qs[0].bid - qs[1].ask) + (qs[2].bid - qs[3].ask), 2)

    def iron_condor_ask(self, condor: IronCondor) -> Optional[float]:
        qs = [self._live_tickers.get(o.conId) for o in (condor.short_call, condor.long_call, condor.short_put, condor.long_put)]
        if any(t is None or not self._has_bid_ask(t) for t in qs):
            return None
        return round((qs[0].ask - qs[1].bid) + (qs[2].ask - qs[3].bid), 2)

    def iron_condor_legs(self, condor: IronCondor) -> List[Option]:
        return [condor.short_call, condor.long_call, condor.short_put, condor.long_put]

    async def cleanup(self) -> None:
        for t in self._live_tickers.values():
            self.ib.cancelMktData(t.contract)
        self._live_tickers.clear()