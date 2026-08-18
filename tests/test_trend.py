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


def _engine_with_bars(cfg, n_flat=65, spike_delta=500):
    """Build an engine: `n_flat` flat bars then a single strong momentum bar."""
    from cvd_engine import CvdEngine

    eng = CvdEngine(cfg)
    ts = datetime(2026, 8, 17, 9, 30, 0)
    cvd = 0.0
    for i in range(n_flat):
        dp = 1.0 if i % 2 == 0 else -1.0
        cvd += dp
        t = ts + timedelta(minutes=i)
        eng.ingest_bar(Bar(ts=t, open=100, high=101, low=99, close=100, volume=100, ticks=5,
                           cvd_open=cvd - dp, cvd_high=max(cvd, cvd - dp), cvd_low=min(cvd, cvd - dp),
                           cvd_close=cvd, cvd_delta=dp))
    # spike bar
    cvd += spike_delta
    t = ts + timedelta(minutes=n_flat)
    eng.ingest_bar(Bar(ts=t, open=100, high=102, low=100, close=102, volume=2000, ticks=20,
                       cvd_open=cvd - spike_delta, cvd_high=cvd, cvd_low=cvd - spike_delta,
                       cvd_close=cvd, cvd_delta=spike_delta))
    return eng


def test_breakout_up():
    cfg = _cfg(breakout_lookback=60, breakout_slope_z=2.0, breakout_delta_ratio=3.0, breakout_min_conditions=3)
    eng = _engine_with_bars(cfg)
    bo = eng.detect_breakout()
    assert bo is not None and bo["direction"] == "up", bo
    assert bo["count"] == 4, bo
    print("OK breakout up (all 4 conditions)")


def test_breakout_insufficient():
    cfg = _cfg(breakout_lookback=60, breakout_slope_z=2.0, breakout_delta_ratio=3.0, breakout_min_conditions=3)
    eng = _engine_with_bars(cfg, n_flat=30)  # < 60 lookback bars
    assert eng.detect_breakout() is None
    print("OK breakout insufficient data")


if __name__ == "__main__":
    test_uptrend()
    test_downtrend()
    test_range()
    test_insufficient_data()
    test_reset()
    test_breakout_up()
    test_breakout_insufficient()
    print("ALL TREND TESTS PASSED")
