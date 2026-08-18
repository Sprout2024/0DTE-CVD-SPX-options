from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional

from config import Config


@dataclass
class Bar:
    ts: datetime
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    ticks: int = 0
    cvd_open: float = 0.0
    cvd_high: float = 0.0
    cvd_low: float = 0.0
    cvd_close: float = 0.0
    cvd_delta: float = 0.0
    iv: float = 0.0

    def update(self, price: float, size: float, total_delta: float) -> None:
        self.ticks += 1
        self.volume += size
        if self.ticks == 1:
            self.open = price
            self.high = price
            self.low = price
        self.close = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.cvd_close = total_delta
        self.cvd_high = max(self.cvd_high, total_delta)
        self.cvd_low = min(self.cvd_low, total_delta)
        self.cvd_delta = self.cvd_close - self.cvd_open


@dataclass
class Signal:
    direction: str
    bar: Bar
    extreme: float
    cvd_extreme: float


class CvdEngine:
    """Builds 1-minute bars from tick trades and flags CVD divergences."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._total_delta = 0.0
        self._cur: Optional[Bar] = None
        self.bars: List[Bar] = []
        self.session_date: Optional[date] = None

    def add_trade(self, price: float, size: float, delta: float, ts: datetime) -> Optional[Bar]:
        if ts is None:
            return None
        if self.session_date is None or ts.date() != self.session_date:
            self._reset_session(ts.date())
        bar_ts = self._align_bar_time(ts)
        rolled: Optional[Bar] = None
        if self._cur is not None and self._cur.ts != bar_ts:
            rolled = self._roll()
        if self._cur is None:
            self._cur = Bar(
                ts=bar_ts,
                cvd_open=self._total_delta,
                cvd_high=self._total_delta,
                cvd_low=self._total_delta,
            )
        self._total_delta += delta
        self._cur.update(price, size, self._total_delta)
        return rolled

    def _align_bar_time(self, ts: datetime) -> datetime:
        seconds = self.cfg.bar_seconds
        aligned = ts.replace(microsecond=0, second=(ts.second // seconds) * seconds)
        return aligned

    def _roll(self) -> Optional[Bar]:
        if self._cur is None:
            return None
        bar = self._cur
        self.bars.append(bar)
        self._cur = None
        if len(self.bars) > 2000:
            self.bars = self.bars[-1000:]
        return bar

    def _reset_session(self, day: date) -> None:
        self.session_date = day
        self.bars.clear()
        self._cur = None
        self._total_delta = 0.0

    def ingest_bar(self, bar: Bar) -> None:
        """Add a pre-built bar (used by bar-mode backtests with a CVD proxy)."""
        if self.session_date is None or bar.ts.date() != self.session_date:
            self._reset_session(bar.ts.date())
        self.bars.append(bar)
        self._total_delta = bar.cvd_close
        if len(self.bars) > 2000:
            self.bars = self.bars[-1000:]

    def detect_signal(self) -> Optional[Signal]:
        if len(self.bars) < self.cfg.divergence_lookback + 1:
            return None
        latest = self.bars[-1]
        if latest.ticks < self.cfg.min_ticks_per_bar:
            return None
        prev = self.bars[:-1]
        window = prev[-self.cfg.divergence_lookback:]

        prev_high = max(b.high for b in prev)
        if latest.high > prev_high and latest.high - prev_high >= self.cfg.min_price_move:
            prev_cvd_max = max(b.cvd_close for b in window)
            if latest.cvd_close < prev_cvd_max - self.cfg.min_cvd_gap:
                return Signal("bear", latest, latest.high, latest.cvd_close)

        prev_low = min(b.low for b in prev)
        if latest.low < prev_low and prev_low - latest.low >= self.cfg.min_price_move:
            prev_cvd_min = min(b.cvd_close for b in window)
            if latest.cvd_close > prev_cvd_min + self.cfg.min_cvd_gap:
                return Signal("bull", latest, latest.low, latest.cvd_close)

        return None

    def detect_breakout(self) -> Optional[Dict]:
        """CVD strong momentum breakout (4-condition "true momentum" check).

        Returns a dict when >= ``breakout_min_conditions`` of the four conditions
        hold: (1) absolute high/low breakout over the lookback, (2) steep CVD
        slope (z-score), (3) price & CVD co-movement, (4) volume-delta spike.
        """
        c = self.cfg
        if len(self.bars) < c.breakout_lookback + 1:
            return None
        latest = self.bars[-1]
        if latest.ticks < c.min_ticks_per_bar:
            return None
        prev = self.bars[-(c.breakout_lookback + 1):-1]
        prev_cvd_max = max(b.cvd_close for b in prev)
        prev_cvd_min = min(b.cvd_close for b in prev)

        c1_bull = latest.cvd_close > prev_cvd_max
        c1_bear = latest.cvd_close < prev_cvd_min

        if len(self.bars) >= 20:
            deltas = [b.cvd_delta for b in self.bars[-20:]]
            m = sum(deltas) / len(deltas)
            var = sum((d - m) ** 2 for d in deltas) / len(deltas)
            sd = var ** 0.5
            mom = sum(b.cvd_delta for b in self.bars[-3:])
            z = mom / sd if sd > 0 else 0.0
        else:
            z = 0.0
        c2_bull = z >= c.breakout_slope_z
        c2_bear = z <= -c.breakout_slope_z

        prev_c = self.bars[-2].close if len(self.bars) >= 2 else latest.close
        price_chg = latest.close - prev_c
        c3_bull = price_chg > 0 and latest.cvd_delta > 0
        c3_bear = price_chg < 0 and latest.cvd_delta < 0

        if len(self.bars) >= 11:
            win = [abs(b.cvd_delta) for b in self.bars[-11:-1]]
            avg = sum(win) / len(win)
        else:
            avg = 0.0
        c4_bull = avg > 0 and latest.cvd_delta >= c.breakout_delta_ratio * avg
        c4_bear = avg > 0 and latest.cvd_delta <= -c.breakout_delta_ratio * avg

        n_bull = int(c1_bull) + int(c2_bull) + int(c3_bull) + int(c4_bull)
        n_bear = int(c1_bear) + int(c2_bear) + int(c3_bear) + int(c4_bear)
        if n_bull >= c.breakout_min_conditions:
            return {
                "direction": "up",
                "count": n_bull,
                "conditions": (c1_bull, c2_bull, c3_bull, c4_bull),
            }
        if n_bear >= c.breakout_min_conditions:
            return {
                "direction": "down",
                "count": n_bear,
                "conditions": (c1_bear, c2_bear, c3_bear, c4_bear),
            }
        return None

    def last_bar(self) -> Optional[Bar]:
        return self.bars[-1] if self.bars else None

    def running_delta(self) -> float:
        return self._total_delta

    def realized_iv(self, lookback: int = 20) -> float:
        """Annualized realized volatility (IV proxy) from recent 1-min close returns."""
        if len(self.bars) < 2:
            return 0.0
        window = self.bars[-lookback:]
        rets = [window[i].close / window[i - 1].close - 1.0 for i in range(1, len(window))]
        if not rets:
            return 0.0
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / len(rets)
        std = var ** 0.5
        bars_per_year = 252.0 * 6.5 * 60.0
        return std * (bars_per_year ** 0.5)