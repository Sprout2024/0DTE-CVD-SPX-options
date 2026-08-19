import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_insync import MarketOrder, Option

from config import Config
from kill_switch import flatten_ib_positions, write_day_stop
from logger import setup_logging
from strategy import Strategy
from tests.mock_ib import MockIB, MockPosition
from tests.test_strategy import _feed_bear_signal

ET = ZoneInfo("America/New_York")


def test_write_day_stop():
    with TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "day_stop.json")
        write_day_stop(path, "2026-08-19", note="manual")
        data = json.load(open(path))
        assert data["day"] == "2026-08-19"
        assert data["note"] == "manual"
        print("OK write_day_stop")


async def test_flatten_ib_positions():
    ib = MockIB(spot=592.0)
    cfg = Config(option_symbol="SPY", account="U1")
    short = Option("SPY", "20260821", 595.0, "C", "SMART")
    short.conId = 1
    long = Option("SPY", "20260821", 620.0, "C", "SMART")
    long.conId = 2
    other = Option("XYZ", "20260821", 100.0, "P", "SMART")
    other.conId = 3
    ib.positions_list = [
        MockPosition(short, "U1", -2),
        MockPosition(long, "U1", 2),
        MockPosition(other, "U1", -1),
    ]

    closed = await flatten_ib_positions(ib, cfg, flatten_all=False)
    assert len(closed) == 2, closed
    assert [c["action"] for c in closed] == ["BUY", "SELL"]
    assert all(c["filled"] for c in closed)
    assert all(c["mode"] == "adaptive" for c in closed), closed
    assert closed[0]["quantity"] == 2
    print("OK flatten (symbol filter):", closed)

    # --all flattens the non-SPX position too
    ib.trades.clear()
    closed_all = await flatten_ib_positions(ib, cfg, flatten_all=True)
    assert len(closed_all) == 3, closed_all
    assert all(c["mode"] == "adaptive" for c in closed_all)
    print("OK flatten (--all):", closed_all)

    # account filter excludes other accounts
    ib2 = MockIB(spot=592.0)
    ib2.positions_list = [MockPosition(short, "U9", -1)]
    cfg2 = Config(option_symbol="SPY", account="U1")
    closed2 = await flatten_ib_positions(ib2, cfg2)
    assert len(closed2) == 0, closed2
    print("OK flatten (account filter)")


async def test_flatten_dry_run():
    ib = MockIB(spot=592.0)
    cfg = Config(option_symbol="SPY", account="U1", dry_run=True)
    short = Option("SPY", "20260821", 595.0, "C", "SMART")
    short.conId = 1
    ib.positions_list = [MockPosition(short, "U1", -2)]
    closed = await flatten_ib_positions(ib, cfg)
    assert len(closed) == 1
    assert closed[0]["filled"] is False
    assert len(ib.trades) == 0, "dry-run must not place orders"
    print("OK flatten dry-run")


async def test_flatten_market_fallback():
    """No quote available -> falls back to a market order so the leg is still closed."""
    ib = MockIB(spot=592.0)
    ib.no_option_quotes = True
    cfg = Config(option_symbol="SPY", account="U1")
    short = Option("SPY", "20260821", 595.0, "C", "SMART")
    short.conId = 1
    ib.positions_list = [MockPosition(short, "U1", -2)]
    closed = await flatten_ib_positions(ib, cfg, timeout=0.5, quote_timeout=0.3)
    assert len(closed) == 1
    assert closed[0]["filled"] is True, closed
    assert closed[0]["mode"] == "market", closed
    assert len(ib.trades) == 1, "fallback market order must be placed"
    assert isinstance(ib.trades[0].order, MarketOrder), "fallback must be a market order"
    print("OK flatten market fallback:", closed)


async def test_strategy_day_stop():
    with TemporaryDirectory() as tmp:
        state_file = str(Path(tmp) / "state.jsonl")
        day_stop_file = str(Path(tmp) / "day_stop.json")
        cfg = Config(
            divergence_lookback=3, min_ticks_per_bar=1,
            option_symbol="SPY", option_sec_type="STK",
            target_delta=0.30, delta_tolerance=0.10,
            spread_width=5.0, strike_band=12.0,
            iron_condor_offset=2, iron_condor_wing=5,
            min_entry_credit=0.10, max_entry_credit=3.00,
            cooldown_seconds=0, regime_filter=False,
            prev_day_trend_filter=0, open_vol_filter=0,
            log_level="INFO", log_file="", signals_file="",
            state_file=state_file, day_stop_file=day_stop_file,
        )
        ib = MockIB(spot=592.0)
        setup_logging(cfg)
        strat = Strategy(ib, cfg)
        strat.entry_allowed = lambda: True
        strat.in_trading_hours = lambda *a: True
        strat._regime_30min = lambda: "range"
        await strat.setup()
        await _feed_bear_signal(ib, strat)
        assert len(strat.executor.positions) == 1, "expected one position before kill switch"
        pos = next(iter(strat.executor.positions.values()))
        assert pos.status == "OPEN"

        write_day_stop(day_stop_file, datetime.now(ET).date().isoformat(), note="test")
        strat._handle_day_stop()
        assert strat._day_stopped, "strategy must be day-stopped"
        assert pos.status == "CLOSED", pos.status
        assert pos.close_kind == "KILLSWITCH", pos.close_kind

        await _feed_bear_signal(ib, strat)
        assert len(strat.executor.positions) == 1, "no new entries allowed after day stop"
        print("OK strategy day-stop clears positions and blocks entries")


async def test_restore_after_day_stop():
    with TemporaryDirectory() as tmp:
        state_file = str(Path(tmp) / "state.jsonl")
        day_stop_file = str(Path(tmp) / "day_stop.json")
        cfg = Config(
            divergence_lookback=3, min_ticks_per_bar=1,
            option_symbol="SPY", option_sec_type="STK",
            target_delta=0.30, delta_tolerance=0.10,
            spread_width=5.0, strike_band=12.0,
            iron_condor_offset=2, iron_condor_wing=5,
            min_entry_credit=0.10, max_entry_credit=3.00,
            cooldown_seconds=0, regime_filter=False,
            prev_day_trend_filter=0, open_vol_filter=0,
            log_level="INFO", log_file="", signals_file="",
            state_file=state_file, day_stop_file=day_stop_file,
        )
        ib = MockIB(spot=592.0)
        setup_logging(cfg)
        strat = Strategy(ib, cfg)
        strat.entry_allowed = lambda: True
        strat.in_trading_hours = lambda *a: True
        strat._regime_30min = lambda: "range"
        await strat.setup()
        await _feed_bear_signal(ib, strat)
        assert len(strat.executor.positions) == 1

        # kill switch fired while the strategy was down: flag present on restart
        write_day_stop(day_stop_file, datetime.now(ET).date().isoformat(), note="test")
        strat2 = Strategy(ib, cfg)
        await strat2.setup()
        await strat2._restore_state()
        assert strat2._day_stopped, "restart must pick up the day-stop flag"
        assert len(strat2.executor.positions) == 0, "positions must not be restored after day stop"
        print("OK restore after day stop skips positions")


async def main():
    test_write_day_stop()
    await test_flatten_ib_positions()
    await test_flatten_dry_run()
    await test_flatten_market_fallback()
    await test_strategy_day_stop()
    await test_restore_after_day_stop()
    print("ALL KILL-SWITCH MOCK TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
