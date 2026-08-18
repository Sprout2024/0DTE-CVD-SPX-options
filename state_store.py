from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from cvd_engine import Bar

_KEEP_DAYS = 7
_ET = ZoneInfo("America/New_York")


def _et_day_from_ts(ts: datetime) -> date:
    if ts.tzinfo is None:
        return ts.date()
    return ts.astimezone(_ET).date()


def _et_now_day() -> date:
    return datetime.now(_ET).date()


class CvdStore:
    """Persists rolled 1-minute CVD bars and the open position to a JSONL file.

    On restart/reconnect the strategy can replay today's bars into a fresh
    CvdEngine (restoring the running cumulative delta) and rebuild the open
    position without re-deriving it from IBKR.
    """

    def __init__(self, path: str):
        self.path = path
        self._prune()

    def _prune(self) -> None:
        if not os.path.exists(self.path):
            return
        cutoff = (_et_now_day() - timedelta(days=_KEEP_DAYS)).isoformat()
        keep = []
        with open(self.path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("d", "") >= cutoff:
                    keep.append(line)
        if len(keep) != self._count_lines():
            with open(self.path, "w") as f:
                f.write("\n".join(keep))
                if keep:
                    f.write("\n")

    def _count_lines(self) -> int:
        try:
            with open(self.path, "r") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _append(self, rec: dict) -> None:
        line = json.dumps(rec, separators=(",", ":"), default=str)
        with open(self.path, "a") as f:
            f.write(line + "\n")

    def append_bar(self, bar: Bar) -> None:
        self._append(
            {
                "k": "bar",
                "d": _et_day_from_ts(bar.ts).isoformat(),
                "ts": bar.ts.isoformat(),
                "o": bar.open, "h": bar.high, "l": bar.low, "c": bar.close,
                "v": bar.volume, "n": bar.ticks,
                "co": bar.cvd_open, "ch": bar.cvd_high, "cl": bar.cvd_low,
                "cc": bar.cvd_close, "cd": bar.cvd_delta,
            }
        )

    def save_position(self, pos) -> None:
        day = datetime.fromtimestamp(pos.entry_time, _ET).date().isoformat()
        base = {
            "id": pos.id,
            "direction": pos.direction,
            "quantity": pos.quantity,
            "entry_time": pos.entry_time,
            "entry_credit": pos.entry_credit,
            "signal_extreme": pos.signal_extreme,
            "signal_cvd": pos.signal_cvd,
            "tp_target": pos.tp_target,
            "sl_price": pos.sl_price,
        }
        if pos.condor is not None:
            base["condor"] = {
                "expiry": pos.condor.expiry,
                "short_call": pos.condor.short_call.strike,
                "long_call": pos.condor.long_call.strike,
                "short_put": pos.condor.short_put.strike,
                "long_put": pos.condor.long_put.strike,
            }
        else:
            base["right"] = "P" if pos.direction == "bull" else "C"
            base["sell_strike"] = pos.spread.short_leg.strike
            base["buy_strike"] = pos.spread.long_leg.strike
            base["expiry"] = pos.spread.expiry
        self._append({"k": "pos", "d": day, "pos": base})

    def clear_position(self) -> None:
        self._append({"k": "pos_done", "d": _et_now_day().isoformat()})

    def load(self, day: date) -> Tuple[List[Bar], Optional[Dict]]:
        """Return (bars, position_dict) persisted for ``day``."""
        day_s = day.isoformat()
        bars: List[Bar] = []
        pos: Optional[Dict] = None
        if not os.path.exists(self.path):
            return bars, pos
        with open(self.path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("d") != day_s:
                    continue
                k = rec.get("k")
                if k == "bar":
                    bars.append(self._bar_from_rec(rec))
                elif k == "pos":
                    pos = rec.get("pos")
                elif k == "pos_done":
                    pos = None
        return bars, pos

    @staticmethod
    def _bar_from_rec(rec: dict) -> Bar:
        return Bar(
            ts=datetime.fromisoformat(rec["ts"]),
            open=rec["o"], high=rec["h"], low=rec["l"], close=rec["c"],
            volume=rec["v"], ticks=rec["n"],
            cvd_open=rec["co"], cvd_high=rec["ch"], cvd_low=rec["cl"],
            cvd_close=rec["cc"], cvd_delta=rec["cd"],
        )