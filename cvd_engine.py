from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional

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

    def last_bar(self) -> Optional[Bar]:
        return self.bars[-1] if self.bars else None

    def running_delta(self) -> float:
        return self._total_delta