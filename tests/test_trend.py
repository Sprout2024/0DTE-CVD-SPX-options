import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from cvd_engine import Bar
from trend import TrendDetector


def _cfg(**kw):
    c = dict(trend_lookback_hours=4, trend_slope_threshold=5.0)
    c.update(kw)
    return Config(**c)


def _feed(det, hours_data, base_hour=9):
    """hours_data: list of (hour, close); feeds 1-min bars within each hour."""
    for hour, close in hours_data:
        ts = datetime(2026, 8, 17, base_hour + hour, 0, 0)
        # a few 1-min bars per hour ending at `close`
        for m in range(3):
            t = ts + timedelta(minutes=m)
            b = Bar(ts=t, open=close, high=close, low=close, close=close, volume=100, ticks=1)
            det.update(b)


def test_uptrend():
    det = TrendDetector(_cfg())
    # 4 hours rising: 100, 110, 120, 130
    _feed(det, [(0, 100), (1, 110), (2, 120), (3, 130)])
    assert det.regime() == "up", det.regime()
    print("OK uptrend")


def test_downtrend():
    det = TrendDetector(_cfg())
    _feed(det, [(0, 130), (1, 120), (2, 110), (3, 100)])
    assert det.regime() == "down", det.regime()
    print("OK downtrend")


def test_range():
    det = TrendDetector(_cfg())
    _feed(det, [(0, 100), (1, 102), (2, 99), (3, 101)])
    assert det.regime() == "range", det.regime()
    print("OK range")


def test_insufficient_data():
    det = TrendDetector(_cfg())
    _feed(det, [(0, 100), (1, 101)])
    assert det.regime() == "range", det.regime()
    print("OK insufficient -> range")


def test_reset():
    det = TrendDetector(_cfg())
    _feed(det, [(0, 100), (1, 110), (2, 120), (3, 130)])
    assert det.regime() == "up"
    det.reset()
    assert det.regime() == "range"
    print("OK reset")


if __name__ == "__main__":
    test_uptrend()
    test_downtrend()
    test_range()
    test_insufficient_data()
    test_reset()
    print("ALL TREND TESTS PASSED")
