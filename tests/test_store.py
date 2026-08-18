import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from cvd_engine import Bar, CvdEngine
from state_store import CvdStore


def test_roundtrip_bars():
    cfg = Config(state_file="")
    eng = CvdEngine(cfg)
    ts = datetime(2026, 8, 17, 9, 30, 0)
    rolled = []
    for i in range(5):
        for sec in (0, 59):
            t = ts.replace(minute=30 + i, second=sec)
            b = eng.add_trade(590.0 + i, 100.0, 10.0, t)
            if b is not None:
                rolled.append(b)
    assert len(rolled) == 4, f"expected 4 rolled bars, got {len(rolled)}"
    for b in rolled:
        eng.ingest_bar(b)
    final_cvd = eng.running_delta()
    print("final running cvd:", final_cvd)


def test_store_roundtrip():
    import tempfile

    d = date(2026, 8, 17)
    ts = datetime(2026, 8, 17, 9, 30, 0)
    bars = [
        Bar(ts=ts.replace(minute=30), open=590, high=591, low=589.5, close=590.5, volume=100, ticks=5, cvd_open=0, cvd_high=10, cvd_low=0, cvd_close=10, cvd_delta=10),
        Bar(ts=ts.replace(minute=31), open=590.5, high=592, low=590, close=591.5, volume=150, ticks=8, cvd_open=10, cvd_high=25, cvd_low=10, cvd_close=25, cvd_delta=15),
        Bar(ts=ts.replace(minute=32), open=591.5, high=591.8, low=589, close=589.5, volume=120, ticks=6, cvd_open=25, cvd_high=25, cvd_low=5, cvd_close=5, cvd_delta=-20),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "state.jsonl")
        store = CvdStore(path)
        for b in bars:
            store.append_bar(b)
        store.clear_position()
        restored, pos = store.load(d)
        assert pos is None
        assert len(restored) == 3, f"expected 3 bars, got {len(restored)}"
        for a, b in zip(bars, restored):
            assert a.ts == b.ts and a.cvd_close == b.cvd_close and a.ticks == b.ticks
        cfg = Config(state_file="")
        eng = CvdEngine(cfg)
        for b in restored:
            eng.ingest_bar(b)
        assert eng.running_delta() == 5.0, eng.running_delta()
        assert len(eng.bars) == 3
        print("OK store bar roundtrip, restored cvd =", eng.running_delta())


def test_store_position_roundtrip():
    import tempfile

    d = date(2026, 8, 17)
    ts = datetime(2026, 8, 17, 9, 30, 0)
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "state.jsonl")
        store = CvdStore(path)
        store.append_bar(Bar(ts=ts, open=590, high=591, low=589.5, close=590.5, volume=100, ticks=5, cvd_open=0, cvd_high=10, cvd_low=0, cvd_close=10, cvd_delta=10))

        class _Leg:
            strike = 7800.0

        class _Spread:
            short_leg = _Leg()
            long_leg = _Leg()
            long_leg.strike = 7750.0
            expiry = "20260817"

        class _Pos:
            id = "P1"
            direction = "bull"
            spread = _Spread()
            quantity = 1
            entry_time = datetime(2026, 8, 17, 12, 0).timestamp()
            entry_credit = 5.0
            signal_extreme = 7780.0
            signal_cvd = 42.0
            tp_target = 3.5
            sl_price = 10.0

        store.save_position(_Pos())
        restored, pos = store.load(d)
        assert pos is not None
        assert pos["id"] == "P1" and pos["direction"] == "bull"
        assert pos["sell_strike"] == 7800.0 and pos["buy_strike"] == 7750.0
        assert pos["right"] == "P"
        print("OK store position roundtrip")


async def test_engine_restore_recomputes_cvd():
    import tempfile

    cfg = Config(
        divergence_lookback=3,
        min_ticks_per_bar=1,
        min_price_move=0.05,
        min_cvd_gap=1.0,
        state_file="",
    )
    ts = datetime(2026, 8, 17, 9, 30, 0)
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "state.jsonl")
        store = CvdStore(path)
        eng = CvdEngine(cfg)
        # build a tick stream producing a bear divergence (price up, cvd down)
        ticks = [
            (ts.replace(minute=30, second=0), 590.0, 100.0),
            (ts.replace(minute=30, second=5), 590.8, 80.0),
            (ts.replace(minute=30, second=10), 591.6, 60.0),
            (ts.replace(minute=31, second=0), 592.0, 20.0),
            (ts.replace(minute=31, second=5), 592.4, -30.0),
            (ts.replace(minute=31, second=10), 592.8, -40.0),
            (ts.replace(minute=32, second=0), 593.0, 30.0),
            (ts.replace(minute=32, second=5), 593.6, 25.0),
            (ts.replace(minute=33, second=0), 593.8, -10.0),
            (ts.replace(minute=33, second=5), 594.2, -15.0),
            (ts.replace(minute=34, second=0), 594.3, -5.0),
        ]
        rolled = []
        for t, p, d in ticks:
            b = eng.add_trade(p, abs(d), d, t)
            if b is not None:
                store.append_bar(b)
                rolled.append(b)
        assert len(rolled) == 4, f"expected 4 rolled bars, got {len(rolled)}"
        live_signal = eng.detect_signal()
        print("live signal:", live_signal)

        # simulate restart: fresh engine, replay persisted bars
        bars, _ = store.load(ts.date())
        eng2 = CvdEngine(cfg)
        for b in bars:
            eng2.ingest_bar(b)
        restored_signal = eng2.detect_signal()
        assert eng2.running_delta() == bars[-1].cvd_close
        assert len(eng2.bars) == len(eng.bars)
        assert live_signal is not None, "test stream should produce a bear divergence"
        assert restored_signal is not None
        assert restored_signal.direction == live_signal.direction == "bear"
        assert restored_signal.extreme == live_signal.extreme
        print("OK engine restore reproduces signal:", restored_signal)


if __name__ == "__main__":
    test_roundtrip_bars()
    test_store_roundtrip()
    test_store_position_roundtrip()
    asyncio.run(test_engine_restore_recomputes_cvd())
    print("ALL STORE TESTS PASSED")