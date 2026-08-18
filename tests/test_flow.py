import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from cvd_engine import Signal
from executor import Executor
from options import OptionSelector
from ib_insync import Stock
from tests.mock_ib import MockIB


async def test_select_spread():
    ib = MockIB(spot=592.0)
    cfg = Config(option_symbol="SPY", spread_width=5.0, strike_band=12.0)
    sel = OptionSelector(ib, cfg)
    await sel.init(Stock("SPY", "SMART", "USD"))
    spr = await sel.select_spread("bear", 592.0)
    assert spr is not None, "bear spread failed"
    assert spr.direction == "bear"
    assert spr.combo.secType == "BAG"
    assert len(spr.combo.comboLegs) == 2
    assert spr.combo.comboLegs[0].action == "SELL"
    assert spr.combo.comboLegs[1].action == "BUY"
    assert spr.short_leg.right == "C"
    assert spr.long_strike if hasattr(spr, "long_strike") else spr.long_leg.strike > spr.short_leg.strike
    print("bear spread:", spr.short_leg.strike, spr.long_leg.strike)
    mid = sel.spread_mid(spr)
    assert mid is not None and mid > 0
    print("bear spread mid:", mid)

    spr2 = await sel.select_spread("bull", 592.0)
    assert spr2 is not None
    assert spr2.short_leg.right == "P"
    assert spr2.long_leg.strike < spr2.short_leg.strike
    mid2 = sel.spread_mid(spr2)
    assert mid2 is not None and mid2 > 0
    print("bull spread:", spr2.short_leg.strike, spr2.long_leg.strike, "mid:", mid2)


async def test_executor_open_close():
    ib = MockIB(spot=592.0)
    cfg = Config(option_symbol="SPY", spread_width=5.0, strike_band=12.0)
    sel = OptionSelector(ib, cfg)
    await sel.init(Stock("SPY", "SMART", "USD"))
    execr = Executor(ib, cfg, sel)
    spr = await sel.select_spread("bear", 592.0)
    mid = sel.spread_mid(spr)
    sig = Signal("bear", bar=None, extreme=594.0, cvd_extreme=330.0)
    pos = await execr.open_position(spr, mid, sig)
    assert pos is not None, "entry not filled"
    print("entry filled credit:", pos.entry_credit)
    # force TP: drop spread mid by setting short leg bid/ask lower
    for t in sel._live_tickers.values():
        if t.contract.conId == spr.short_leg.conId:
            t.bid = 0.10
            t.ask = 0.20
    await execr.manage(592.0, None)
    pos.close_trade._fill_after = 0  # ensure fill pending
    for _ in range(3):
        await asyncio.sleep(0.1)
        await execr.manage(592.0, None)
    assert pos.status == "CLOSED", pos.status
    print("closed, pnl:", pos.realized_pnl, "kind:", pos.close_kind)


async def test_executor_stop_loss():
    ib = MockIB(spot=592.0)
    cfg = Config(option_symbol="SPY", spread_width=5.0, strike_band=12.0)
    sel = OptionSelector(ib, cfg)
    await sel.init(Stock("SPY", "SMART", "USD"))
    execr = Executor(ib, cfg, sel)
    spr = await sel.select_spread("bull", 592.0)
    mid = sel.spread_mid(spr)
    sig = Signal("bull", bar=None, extreme=589.0, cvd_extreme=100.0)
    pos = await execr.open_position(spr, mid, sig)
    assert pos is not None
    for t in sel._live_tickers.values():
        if t.contract.conId == spr.short_leg.conId:
            t.bid = 1.0
            t.ask = 1.2
    await execr.manage(592.0, None)
    for _ in range(3):
        await asyncio.sleep(0.1)
        await execr.manage(592.0, None)
    assert pos.status == "CLOSED"
    assert pos.close_kind == "SL"
    print("stop loss triggered OK, kind:", pos.close_kind)


def test_snap_tick():
    snap = Executor._snap_tick
    assert snap(2.93) == 2.90, snap(2.93)
    assert snap(3.08) == 3.05, snap(3.08)
    assert snap(3.53) == 3.50, snap(3.53)
    assert snap(2.55) == 2.50, snap(2.55)
    assert snap(5.10) == 5.10, snap(5.10)
    assert snap(0.90) == 0.90, snap(0.90)
    assert snap(3.00) == 3.00, snap(3.00)
    assert snap(2.99) == 2.90, snap(2.99)
    print("OK snap_tick")


async def main():
    test_snap_tick()
    await test_select_spread()
    await test_executor_open_close()
    await test_executor_stop_loss()
    print("ALL MOCK TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
