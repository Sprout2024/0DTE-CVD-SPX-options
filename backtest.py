from __future__ import annotations

import logging
import math
from math import exp, log, sqrt
from statistics import NormalDist
from typing import Dict, List, Optional, Tuple

from trend import TrendDetector

N = NormalDist().cdf


def _d1(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    return (log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))


def bs_price_and_delta(right: str, S: float, K: float, T: float, r: float, q: float, sigma: float) -> Tuple[float, float]:
    if T <= 0:
        if right == "C":
            return max(S - K, 0.0), 1.0 if S >= K else 0.0
        return max(K - S, 0.0), -1.0 if S <= K else 0.0
    d1 = _d1(S, K, T, r, q, sigma)
    if right == "C":
        price = S * exp(-q * T) * N(d1) - K * exp(-r * T) * N(d1 - sigma * sqrt(T))
        delta = exp(-q * T) * N(d1)
    else:
        price = K * exp(-r * T) * N(-d1 + sigma * sqrt(T)) - S * exp(-q * T) * N(-d1)
        delta = -exp(-q * T) * N(-d1)
    return price, delta


class OptionPricer:
    """Black-Scholes pricer used to simulate 0DTE option fills in backtests.

    ``expiry_minute`` is the RTH settlement time in minutes after midnight ET
    (SPX 0DTE settles at 16:15, SPY at 16:00).
    """

    def __init__(self, iv: float, rate: float = 0.0, dividend: float = 0.0, expiry_minute: int = 16 * 60 + 15):
        self.iv = iv
        self.r = rate
        self.q = dividend
        self.expiry_minute = expiry_minute

    def minutes_to_close(self, ts) -> float:
        return max(0.0, self.expiry_minute - (ts.hour * 60 + ts.minute) - ts.second / 60.0)

    def _T(self, ts) -> float:
        return self.minutes_to_close(ts) / (365.0 * 24.0 * 60.0)

    def select_spread(self, direction: str, spot: float, ts, strikes: List[float], width: float = 5.0) -> Optional[Tuple[float, float]]:
        right = "C" if direction == "bear" else "P"
        T = self._T(ts)
        lo = 0.20
        hi = 0.40
        best = None
        for k in strikes:
            _, delta = bs_price_and_delta(right, spot, k, T, self.r, self.q, self.iv)
            ad = abs(delta)
            if ad < lo or ad > hi:
                continue
            if direction == "bear" and delta <= 0:
                continue
            if direction == "bull" and delta >= 0:
                continue
            score = abs(ad - 0.30)
            if best is None or score < best[0]:
                best = (score, k)
        if best is None:
            if direction == "bear":
                best = (0.0, next((k for k in strikes if k > spot), strikes[-1]))
            else:
                best = (0.0, next((k for k in reversed(strikes) if k < spot), strikes[0]))
        sell_strike = best[1]
        long_strike = min(strikes, key=lambda s: abs(s - (sell_strike + width)))
        return sell_strike, long_strike

    def spread_mid(self, direction: str, sell_strike: float, buy_strike: float, spot: float, ts) -> float:
        right = "C" if direction == "bear" else "P"
        T = self._T(ts)
        sell_price, _ = bs_price_and_delta(right, spot, sell_strike, T, self.r, self.q, self.iv)
        buy_price, _ = bs_price_and_delta(right, spot, buy_strike, T, self.r, self.q, self.iv)
        return round(sell_price - buy_price, 2)


class Backtester:
    """Replays historical ticks through the live CVD engine and simulates trades."""

    def __init__(self, cfg, pricer: OptionPricer, log=None):
        self.cfg = cfg
        self.pricer = pricer
        self.log = log or logging.getLogger("backtest")
        self.engine = None
        self.trend = TrendDetector(cfg)
        self.trades: List[Dict] = []
        self.pos: Optional[Dict] = None
        self.strikes: List[float] = []
        self._last_signal = None
        self._last_signal_time = None
        self.cooldown_until = None

    def _reset(self) -> None:
        from cvd_engine import CvdEngine

        self.engine = CvdEngine(self.cfg)
        self.trend = TrendDetector(self.cfg)
        self.pos = None

    def _round_trip_cost(self) -> float:
        """Commission + exchange fees to open and close a spread (2 legs)."""
        c = self.cfg.contracts
        legs = 2
        comm = max(self.cfg.min_commission_per_order, self.cfg.commission_per_contract * c * legs)
        fee = self.cfg.exchange_fee_per_contract * c * legs
        return round((comm + fee) * 2, 2)

    def _condor_cost(self) -> float:
        """Commission + exchange fees to open and close an iron condor (4 legs)."""
        c = self.cfg.contracts
        legs = 4
        comm = max(self.cfg.min_commission_per_order, self.cfg.commission_per_contract * c * legs)
        fee = self.cfg.exchange_fee_per_contract * c * legs
        return round((comm + fee) * 2, 2)

    def _op_spot(self, spot: float) -> float:
        """Scale the signal instrument's spot to the option underlying (SPY -> SPX)."""
        return spot * self.cfg.spot_scale

    def _open(self, signal, ts, spot) -> bool:
        if self.pos is not None:
            return False
        op_spot = self._op_spot(spot)
        width = self.cfg.spread_width if signal.direction == "bear" else -self.cfg.spread_width
        sel = self.pricer.select_spread(signal.direction, op_spot, ts, self.strikes, width)
        if sel is None:
            return False
        sell_strike, buy_strike = sel
        mid = self.pricer.spread_mid(signal.direction, sell_strike, buy_strike, op_spot, ts)
        credit = max(0.01, round(mid - self.cfg.mid_offset, 2))
        if credit < self.cfg.min_entry_credit or credit > self.cfg.max_entry_credit:
            return False
        self.pos = {
            "kind": "spread",
            "direction": signal.direction,
            "sell_strike": sell_strike,
            "buy_strike": buy_strike,
            "entry_ts": ts,
            "entry_spot": spot,
            "entry_credit": credit,
            "tp_target": round(credit * (1.0 - self.cfg.take_profit_pct), 2),
            "sl_price": round(credit * (1.0 + self.cfg.stop_loss_pct), 2),
            "signal_extreme": signal.extreme,
            "signal_cvd": signal.cvd_extreme,
            "entry_idx": len(self.trades),
        }
        self.log.info(
            "OPEN %s sell=%s buy=%s spot=%.2f credit=%.2f",
            signal.direction, sell_strike, buy_strike, spot, credit,
        )
        return True

    def _snap5(self, v: float) -> float:
        return int(round(v / 5.0)) * 5

    def _open_condor(self, signal, ts, spot) -> bool:
        if self.pos is not None:
            return False
        c = self.cfg
        op = self._op_spot(spot)
        sc = self._snap5(op + c.iron_condor_offset)
        lc = self._snap5(op + c.iron_condor_offset + c.iron_condor_wing)
        sp = self._snap5(op - c.iron_condor_offset)
        lp = self._snap5(op - c.iron_condor_offset - c.iron_condor_wing)
        credit = round(
            self.pricer.spread_mid("bear", sc, lc, op, ts) + self.pricer.spread_mid("bull", sp, lp, op, ts), 2
        )
        if credit <= 0 or credit < c.min_entry_credit or credit > c.max_entry_credit:
            return False
        self.pos = {
            "kind": "condor",
            "direction": "range",
            "sc": sc, "lc": lc, "sp": sp, "lp": lp,
            "entry_ts": ts,
            "entry_spot": spot,
            "entry_credit": credit,
            "tp_target": round(credit * (1.0 - c.iron_condor_take_profit_pct), 2),
            "sl_price": round(credit * (1.0 + c.iron_condor_stop_loss_pct), 2),
            "signal_extreme": signal.extreme,
            "signal_cvd": signal.cvd_extreme,
            "entry_idx": len(self.trades),
        }
        self.log.info(
            "OPEN CONDOR sc=%s lc=%s sp=%s lp=%s spot=%.2f credit=%.2f",
            sc, lc, sp, lp, spot, credit,
        )
        return True

    def _condor_value(self, pos, spot, ts) -> float:
        op = self._op_spot(spot)
        return round(
            self.pricer.spread_mid("bear", pos["sc"], pos["lc"], op, ts)
            + self.pricer.spread_mid("bull", pos["sp"], pos["lp"], op, ts), 2
        )

    def _manage_tick(self, ts, spot) -> None:
        pos = self.pos
        if pos is None:
            return
        if pos["kind"] == "condor":
            self._manage_condor(ts, spot)
            return
        held = (ts - pos["entry_ts"]).total_seconds()
        mid = self.pricer.spread_mid(
            pos["direction"], pos["sell_strike"], pos["buy_strike"], self._op_spot(spot), ts
        )
        kind = None
        if mid >= pos["sl_price"]:
            kind = "SL"
            close_price = mid
        elif mid <= pos["tp_target"]:
            kind = "TP"
            close_price = round(mid + self.cfg.close_offset, 2)
        elif self._tech_stop(pos, ts, spot):
            kind = "TECH"
            close_price = mid
        elif held >= self.cfg.hard_time_seconds:
            kind = "TIME"
            close_price = round(mid + self.cfg.close_offset, 2)
        elif held >= self.cfg.profit_time_seconds and mid < pos["entry_credit"]:
            kind = "TIME"
            close_price = round(mid + self.cfg.close_offset, 2)
        if kind is None:
            return
        gross = (pos["entry_credit"] - close_price) * 100.0 * self.cfg.contracts
        cost = self._round_trip_cost()
        pnl = round(gross - cost, 2)
        self.trades.append(
            {
                "direction": pos["direction"],
                "sell_strike": pos["sell_strike"],
                "buy_strike": pos["buy_strike"],
                "entry_time": pos["entry_ts"],
                "exit_time": ts,
                "entry_credit": pos["entry_credit"],
                "exit_price": close_price,
                "kind": kind,
                "gross_pnl": round(gross, 2),
                "cost": cost,
                "pnl": pnl,
                "held_sec": round(held, 1),
            }
        )
        self.log.info(
            "CLOSE %s kind=%s exit=%.2f pnl=%.2f held=%.0fs",
            pos["direction"], kind, close_price, pnl, held,
        )
        self.pos = None

    def _manage_condor(self, ts, spot) -> None:
        pos = self.pos
        if pos is None:
            return
        value = self._condor_value(pos, spot, ts)
        kind = None
        if value >= pos["sl_price"]:
            kind = "SL"
            close_price = value
        elif value <= pos["tp_target"]:
            kind = "TP"
            close_price = value
        if kind is None:
            return
        gross = (pos["entry_credit"] - close_price) * 100.0 * self.cfg.contracts
        cost = self._condor_cost()
        pnl = round(gross - cost, 2)
        self.trades.append(
            {
                "direction": "range",
                "sell_strike": pos["sc"],
                "buy_strike": pos["sp"],
                "entry_time": pos["entry_ts"],
                "exit_time": ts,
                "entry_credit": pos["entry_credit"],
                "exit_price": close_price,
                "kind": kind,
                "gross_pnl": round(gross, 2),
                "cost": cost,
                "pnl": pnl,
                "held_sec": round((ts - pos["entry_ts"]).total_seconds(), 1),
            }
        )
        self.log.info("CLOSE CONDOR kind=%s exit=%.2f pnl=%.2f", kind, close_price, pnl)
        self.pos = None

    def _tech_stop(self, pos, ts, spot) -> bool:
        bar = self.engine.last_bar() if self.engine else None
        if bar is None:
            return False
        buf = self.cfg.tech_stop_buffer
        if pos["direction"] == "bear":
            return spot > pos["signal_extreme"] + buf and bar.cvd_delta > 0 and bar.cvd_close > pos["signal_cvd"]
        return spot < pos["signal_extreme"] - buf and bar.cvd_delta < 0 and bar.cvd_close < pos["signal_cvd"]

    def _signal(self) -> bool:
        if self.pos is not None:
            return False
        sig = self.engine.detect_signal()
        if sig is None:
            return False
        if self._last_signal_time is not None and abs(sig.extreme - self._last_signal) < self.cfg.min_signal_spacing:
            return False
        self._last_signal = sig.extreme
        self._last_signal_time = sig.bar.ts
        if self.cfg.regime_filter:
            regime = self.trend.regime()
            if regime != "range":
                self.log.info("signal %s skipped (trend %s, no trade)", sig.direction, regime)
                return False
            return self._open_condor(sig, sig.bar.ts, sig.bar.close)
        return self._open(sig, sig.bar.ts, sig.bar.close)

    def run_ticks(self, ticks: List[Tuple]) -> None:
        from cvd_engine import CvdEngine

        self.engine = CvdEngine(self.cfg)
        for ts, price, delta in ticks:
            bar = self.engine.add_trade(price, 1.0, delta, ts)
            if bar is not None:
                self.trend.update(bar)
                self._signal()
            if self.pos is not None:
                self._manage_tick(ts, price)
        if self.pos is not None:
            self._force_close(ts, price)

    def find_extreme_bars(self, bars: List[Tuple], lookback: Optional[int] = None) -> List:
        """Timestamps of bars that set a new intraday high or low (pure price logic).

        Used by hybrid backtests to decide which minutes need precise tick data.
        """
        if lookback is None:
            lookback = self.cfg.divergence_lookback
        extremes: List = []
        max_h = -math.inf
        min_l = math.inf
        for i, (ts, o, h, l, c, v) in enumerate(bars):
            if h > max_h or l < min_l:
                if i >= lookback:
                    extremes.append(ts)
            if h > max_h:
                max_h = h
            if l < min_l:
                min_l = l
        return extremes

    def run_bars(self, bars: List[Tuple], precise_delta: Optional[Dict] = None) -> None:
        from cvd_engine import Bar, CvdEngine

        self.engine = CvdEngine(self.cfg)
        total = 0.0
        last_ts = None
        last_close = None
        for ts, o, h, l, c, volume in bars:
            t = ts.time()
            if t < self.cfg.session_start or t > self.cfg.session_end:
                continue
            if precise_delta is not None and ts in precise_delta:
                delta_proxy = precise_delta[ts]
            elif h > l:
                delta_proxy = volume * (2.0 * (c - o) / (h - l))
            else:
                delta_proxy = 0.0
            total += delta_proxy
            bar = Bar(
                ts=ts,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=volume,
                ticks=self.cfg.min_ticks_per_bar,
                cvd_open=round(total - delta_proxy, 4),
                cvd_high=round(max(total - delta_proxy, total), 4),
                cvd_low=round(min(total - delta_proxy, total), 4),
                cvd_close=round(total, 4),
                cvd_delta=round(delta_proxy, 4),
            )
            self.engine.ingest_bar(bar)
            self.trend.update(bar)
            self._signal()
            if self.pos is not None:
                self._manage_tick(ts, c)
            last_ts, last_close = ts, c
        if self.pos is not None and last_ts is not None:
            self._force_close(last_ts, last_close)

    def _force_close(self, ts, spot) -> None:
        pos = self.pos
        if pos is None:
            return
        if pos["kind"] == "condor":
            value = self._condor_value(pos, spot, ts)
            cost = self._condor_cost()
            sell, buy = pos["sc"], pos["sp"]
        else:
            value = self.pricer.spread_mid(
                pos["direction"], pos["sell_strike"], pos["buy_strike"], self._op_spot(spot), ts
            )
            cost = self._round_trip_cost()
            sell, buy = pos["sell_strike"], pos["buy_strike"]
        gross = (pos["entry_credit"] - value) * 100.0 * self.cfg.contracts
        pnl = round(gross - cost, 2)
        self.trades.append(
            {
                "direction": pos["direction"],
                "sell_strike": sell,
                "buy_strike": buy,
                "entry_time": pos["entry_ts"],
                "exit_time": ts,
                "entry_credit": pos["entry_credit"],
                "exit_price": value,
                "kind": "EOD",
                "gross_pnl": round(gross, 2),
                "cost": cost,
                "pnl": pnl,
                "held_sec": round((ts - pos["entry_ts"]).total_seconds(), 1),
            }
        )
        self.pos = None

    def report(self) -> Dict:
        trades = self.trades
        if not trades:
            return {"trades": 0}
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        gross = sum(t["pnl"] for t in trades)
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        curve = []
        for t in trades:
            equity += t["pnl"]
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
            curve.append(equity)
        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades),
            "net_pnl": round(gross, 2),
            "avg_pnl": round(gross / len(trades), 2),
            "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
            "profit_factor": round(sum(t["pnl"] for t in wins) / max(1e-9, abs(sum(t["pnl"] for t in losses))), 2),
            "total_cost": round(sum(t["cost"] for t in trades), 2),
            "max_drawdown": round(max_dd, 2),
            "avg_hold_sec": round(sum(t["held_sec"] for t in trades) / len(trades), 1),
            "equity_curve": curve,
        }