import asyncio
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_insync.objects import TickData

from config import Config
from logger import setup_logging
from strategy import Strategy
from tests.mock_ib import MockIB

ET = ZoneInfo("America/New_York")


def make_strategy(cfg):
    ib = MockIB(spot=592.0)
    setup_logging(cfg)
    strat = Strategy(ib, cfg)
    strat.entry_allowed = lambda: True
    return strat, ib


async def test_no_trade_windows():
    cfg = Config(option_symbol="SPY", option_sec_type="STK", spread_width=5.0, strike_band=12.0)
    ib = MockIB(spot=592.0)
    strat = Strategy(ib, cfg)
    d = datetime(2026, 8, 17, tzinfo=ET)

    def at(h, m, s=0):
        return d.replace(hour=h, minute=m, second=s)

    assert not strat.entry_allowed(at(9, 29)), "pre-open"
    assert not strat.entry_allowed(at(9, 30)), "first 15 min blocked"
    assert not strat.entry_allowed(at(9, 44, 59)), "still within first 15 min"
    assert strat.entry_allowed(at(9, 45)), "window opens at 09:45"
    assert strat.entry_allowed(at(12, 0)), "midday ok"
    assert strat.entry_allowed(at(15, 29, 59)), "before close window"
    assert not strat.entry_allowed(at(15, 30)), "last 30 min blocked"
    assert not strat.entry_allowed(at(16, 0)), "after session end"
    assert strat.in_trading_hours(at(9, 35)), "session open though entries blocked"
    assert not strat.in_trading_hours(at(16, 1)), "past session end"
    assert not strat.entry_allowed(d.replace(month=8, day=22)), "saturday"
    print("OK no-trade windows")


def emit_trade(ib, ticker, price, size, side, ts):
    if side > 0:
        bid = round(price - 0.02, 2)
        ask = price
        trade_price = ask
    else:
        bid = price
        ask = round(price + 0.02, 2)
        trade_price = bid
    ticks = [
        TickData(ts, 1, bid, size),
        TickData(ts, 2, ask, size),
        TickData(ts, 4, trade_price, size),
    ]
    ticker.ticks = ticks
    ib.pendingTickersEvent.emit([ticker])


async def test_full_strategy():
    with TemporaryDirectory() as tmp:
        cfg = Config(
            divergence_lookback=3,
            min_ticks_per_bar=1,
            option_symbol="SPY",
            option_sec_type="STK",
            target_delta=0.30,
            delta_tolerance=0.10,
            spread_width=5.0,
            strike_band=12.0,
            min_entry_credit=0.10,
            max_entry_credit=3.00,
            cooldown_seconds=0,
            profit_time_seconds=30,
            hard_time_seconds=60,
            take_profit_pct=0.30,
            stop_loss_pct=1.00,
            mid_offset=0.02,
            regime_filter=False,
            log_level="INFO",
            log_file="",
            state_file=str(Path(tmp) / "state.jsonl"),
            signals_file="",
        )
        ib = MockIB(spot=592.0)
        setup_logging(cfg)
        strat = Strategy(ib, cfg)
        strat.entry_allowed = lambda: True
        await strat.setup()

        spy_ticker = strat.spot_ticker
        today = datetime.now(ZoneInfo("America/New_York")).date()
        base = datetime(today.year, today.month, today.day, 9, 30, 0)

        feeds = {
            "A": [(590.0, 100, 1), (591.0, 100, 1), (592.0, 100, 1)],
            "B": [(592.2, 50, 1), (592.5, 50, 1)],
            "C": [(592.5, 50, 1), (592.6, 50, 1)],
            "D": [(593.0, 50, -1), (593.5, 50, -1), (594.0, 50, -1)],
        }
        bar_start = {k: base.replace(minute=30 + i) for i, k in enumerate(["A", "B", "C", "D"])}
        for key, trades in feeds.items():
            ts = bar_start[key]
            for seq, (price, size, side) in enumerate(trades):
                t = ts.replace(second=seq)
                emit_trade(ib, spy_ticker, price, size, side, t)
                await asyncio.sleep(0.02)
        emit_trade(ib, spy_ticker, 593.5, 50, -1, base.replace(minute=34, second=1))
        await asyncio.sleep(0.5)

        positions = strat.executor.positions
        assert len(positions) == 1, f"expected 1 position, got {positions}"
        pos = next(iter(positions.values()))
        assert pos.direction == "bear", pos.direction
        assert pos.status == "OPEN"
        print("entry OK:", pos.id, pos.direction, "credit:", pos.entry_credit)

        # simulate restart: fresh strategy on the same IB + state file must restore the position
        strat2 = Strategy(ib, cfg)
        await strat2.setup()
        await strat2._restore_state()
        assert len(strat2.executor.positions) == 1, f"expected restored position, got {strat2.executor.positions}"
        pos2 = next(iter(strat2.executor.positions.values()))
        assert pos2.direction == pos.direction and pos2.entry_credit == pos.entry_credit
        assert pos2.status == "OPEN" and pos2.signal_extreme == pos.signal_extreme
        assert len(strat2.engine.bars) == len(strat.engine.bars), (len(strat2.engine.bars), len(strat.engine.bars))
        print("restore OK: pos", pos2.id, "bars", len(strat2.engine.bars), "cvd", round(strat2.engine.running_delta(), 2))

        # force a profitable exit by lowering the short leg quote
        spread = pos2.spread
        for t in strat2.selector._live_tickers.values():
            if t.contract.conId == spread.short_leg.conId:
                t.bid = 0.05
                t.ask = 0.15
        for _ in range(20):
            await asyncio.sleep(0.1)
            await strat2.executor.manage(594.0, strat2.engine.last_bar())
            if pos2.status == "CLOSED":
                break
        assert pos2.status == "CLOSED", pos2.status
        assert pos2.realized_pnl > 0, pos2.realized_pnl
        print("close OK: kind=%s pnl=%.2f" % (pos2.close_kind, pos2.realized_pnl))


async def _feed_bear_signal(ib, strat):
    """Feed a 4-minute uptrend followed by a bear divergence (new high + cvd down)."""
    spy_ticker = strat.spot_ticker
    today = datetime.now(ZoneInfo("America/New_York")).date()
    base = datetime(today.year, today.month, today.day, 9, 30, 0)
    feeds = {
        "A": [(590.0, 100, 1), (591.0, 100, 1), (592.0, 100, 1)],
        "B": [(592.2, 50, 1), (592.5, 50, 1)],
        "C": [(592.5, 50, 1), (592.6, 50, 1)],
        "D": [(593.0, 50, -1), (593.5, 50, -1), (594.0, 50, -1)],
    }
    bar_start = {k: base.replace(minute=30 + i) for i, k in enumerate(["A", "B", "C", "D"])}
    for key, trades in feeds.items():
        ts = bar_start[key]
        for seq, (price, size, side) in enumerate(trades):
            t = ts.replace(second=seq)
            emit_trade(ib, spy_ticker, price, size, side, t)
            await asyncio.sleep(0.02)
    emit_trade(ib, spy_ticker, 593.5, 50, -1, base.replace(minute=34, second=1))
    await asyncio.sleep(0.5)


async def test_regime_gate():
    with TemporaryDirectory() as tmp:
        cfg = Config(
            divergence_lookback=3, min_ticks_per_bar=1,
            option_symbol="SPY", option_sec_type="STK",
            spread_width=5.0, strike_band=12.0,
            min_entry_credit=0.10, max_entry_credit=3.00,
            cooldown_seconds=0, regime_filter=True,
            log_level="INFO", log_file="", signals_file="",
            state_file=str(Path(tmp) / "state.jsonl"),
        )

        # uptrend: bear signal must be skipped
        ib = MockIB(spot=592.0)
        strat = Strategy(ib, cfg)
        strat.entry_allowed = lambda: True
        await strat.setup()
        strat.trend.regime = lambda: "up"
        await _feed_bear_signal(ib, strat)
        assert len(strat.executor.positions) == 0, "trend up should be no-trade (skipped)"
        print("OK regime gate: trend up -> no trade")

        # downtrend: also no-trade (空仓)
        ib2 = MockIB(spot=592.0)
        strat2 = Strategy(ib2, cfg)
        strat2.entry_allowed = lambda: True
        await strat2.setup()
        strat2.trend.regime = lambda: "down"
        await _feed_bear_signal(ib2, strat2)
        assert len(strat2.executor.positions) == 0, "trend down should be no-trade (skipped)"
        print("OK regime gate: trend down -> no trade")


async def main():
    await test_full_strategy()
    await test_no_trade_windows()
    await test_regime_gate()
    await test_rth_gate()
    print("FULL STRATEGY MOCK TEST PASSED")


async def test_rth_gate():
    """Outside 09:30-16:00 ET ticks must NOT build/record CVD bars."""
    with TemporaryDirectory() as tmp:
        cfg = Config(
            divergence_lookback=3, min_ticks_per_bar=1,
            option_symbol="SPY", option_sec_type="STK",
            spread_width=5.0, strike_band=12.0,
            regime_filter=False, cooldown_seconds=0,
            log_level="INFO", log_file="", signals_file="",
            state_file=str(Path(tmp) / "state.jsonl"),
        )
        ib = MockIB(spot=592.0)
        strat = Strategy(ib, cfg)
        strat.entry_allowed = lambda: True
        await strat.setup()
        ticker = strat.spot_ticker
        today = datetime.now(ZoneInfo("America/New_York")).date()

        # feed an out-of-hours tick (08:00 ET) -> no processing
        emit_trade(ib, ticker, 590.0, 100, 1, datetime(today.year, today.month, today.day, 8, 0, 0))
        await asyncio.sleep(0.2)
        assert strat._last_spot is None, f"out-of-hours should not update spot, got {strat._last_spot}"
        print("OK RTH gate: 08:00 tick ignored")

        # feed an in-hours tick (10:00 ET) -> processed
        emit_trade(ib, ticker, 591.0, 100, 1, datetime(today.year, today.month, today.day, 10, 0, 0))
        await asyncio.sleep(0.2)
        assert strat._last_spot == 591.0, f"in-hours should update spot, got {strat._last_spot}"
        print("OK RTH gate: 10:00 tick processed")


if __name__ == "__main__":
    asyncio.run(main())
