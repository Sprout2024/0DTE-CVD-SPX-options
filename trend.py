from __future__ import annotations

from statistics import linear_regression
from typing import List, Optional

from cvd_engine import Bar


class TrendDetector:
    """Aggregates 1-minute bars into hourly closes and classifies the regime.

    ``regime()`` returns one of "up", "down", "range", or "none" when there is
    not enough data yet to classify (no-trade).
    slope (points/hour) over the last ``trend_lookback_hours`` hourly closes.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.hourly_closes: List[float] = []
        self._cur_hour = None
        self._cur_close: Optional[float] = None

    def update(self, bar: Bar) -> None:
        hour = bar.ts.replace(minute=0, second=0, microsecond=0)
        if self._cur_hour is None or hour != self._cur_hour:
            if self._cur_hour is not None and self._cur_close is not None:
                self.hourly_closes.append(self._cur_close)
                if len(self.hourly_closes) > 60:
                    self.hourly_closes = self.hourly_closes[-60:]
            self._cur_hour = hour
        self._cur_close = bar.close

    def regime(self) -> str:
        closes = list(self.hourly_closes)
        if self._cur_close is not None:
            closes.append(self._cur_close)
        n = self.cfg.trend_lookback_hours
        if len(closes) < max(3, n):
            return "none"
        window = closes[-n:]
        if len(window) < 3:
            return "none"
        x = list(range(len(window)))
        slope, _ = linear_regression(x, window)
        if slope > self.cfg.trend_slope_threshold:
            return "up"
        if slope < -self.cfg.trend_slope_threshold:
            return "down"
        return "range"

    def reset(self) -> None:
        self.hourly_closes.clear()
        self._cur_hour = None
        self._cur_close = None
