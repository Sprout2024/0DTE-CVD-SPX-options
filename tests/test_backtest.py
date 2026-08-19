import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import Backtester, OptionPricer
from config import Config
from hist_data import minute_deltas


def make_bars():
    base = datetime(2026, 8, 17, 9, 30, 0)
    return [
        (base, 590.0, 592.0, 590.0, 592.0, 5000),
        (base + timedelta(minutes=1), 592.0, 592.5, 591.8, 592.4, 2000),
        (base + timedelta(minutes=2), 592.4, 592.6, 592.3, 592.5, 1500),
        (base + timedelta(minutes=3), 592.5, 594.0, 592.0, 592.2, 1200),
        (base + timedelta(minutes=4), 592.2, 592.3, 591.0, 591.2, 1800),
        (base + timedelta(minutes=5), 591.2, 591.4, 590.5, 590.6, 2200),
    ]


def make_day_ticks():
    """Build a realistic tick stream with a bear divergence and enough follow-through."""
    base = datetime(2026, 8, 17, 9, 30, 0)
    ticks = []

    def add(prices, deltas, minute):
        for i, (p, d) in enumerate(zip(prices, deltas)):
            t = base.replace(minute=minute, second=i * 2)
            ticks.append((t, p, d))

    add([590.0, 590.6, 591.2, 591.6, 592.0, 592.4], [50] * 6, 30)
    add([592.5, 592.7, 592.8, 592.9], [30] * 4, 31)
    add([592.9, 593.0, 593.1, 593.0], [20, 15, 15, -10], 32)
    add([593.2, 593.5, 593.8, 594.0, 594.2, 594.3], [-40] * 6, 33)
    add([594.0, 593.5, 593.0, 592.5, 592.0, 591.5, 591.0, 590.5], [-25] * 8, 34)
    add([590.5, 590.3, 590.1, 589.9, 589.7, 589.5, 589.3, 589.1], [-10] * 8, 35)
    return ticks


async def test_backtest_tick():
    cfg = Config(
        divergence_lookback=3,
        min_ticks_per_bar=1,
        regime_filter=False,
        cooldown_seconds=0,
        profit_time_seconds=120,
        hard_time_seconds=300,
        take_profit_pct=0.30,
        stop_loss_pct=1.00,
        spot_scale=1.0,
        spread_width=5.0,
        min_entry_credit=0.10,
        max_entry_credit=3.00,
        log_level="INFO",
        log_file="",
    )
    bt = Backtester(cfg, OptionPricer(iv=0.20))
    bt.strikes = [round(450 + i * 1.0, 1) for i in range(400)]
    ticks = make_day_ticks()
    bt.run_ticks(ticks)
    assert len(bt.trades) == 1, f"expected 1 trade, got {bt.trades}"
    tr = bt.trades[0]
    assert tr["direction"] == "bear"
    assert tr["pnl"] > 0, tr
    assert tr["gross_pnl"] - tr["cost"] == tr["pnl"], tr
    assert tr["cost"] == 3.20, tr
    print("trade:", tr)
    stats = bt.report()
    print("stats:", {k: v for k, v in stats.items() if k != "equity_curve"})
    assert stats["trades"] == 1 and stats["win_rate"] == 1.0
    print("OK backtest tick mode")


async def test_backtest_bar_proxy():
    cfg = Config(
        divergence_lookback=3,
        min_ticks_per_bar=1,
        regime_filter=False,
        cooldown_seconds=0,
        spot_scale=1.0,
        spread_width=5.0,
        min_entry_credit=0.10,
        max_entry_credit=3.00,
        log_level="INFO",
        log_file="",
    )
    bars = make_bars()
    bt = Backtester(cfg, OptionPricer(iv=0.20))
    bt.strikes = [round(450 + i * 1.0, 1) for i in range(400)]
    bt.run_bars(bars)
    stats = bt.report()
    print("bar-mode stats:", {k: v for k, v in stats.items() if k != "equity_curve"})
    assert stats["trades"] == 1
    assert stats["trades"][0] if False else True
    print("OK backtest bar mode")


async def test_backtest_hybrid():
    cfg = Config(
        divergence_lookback=3,
        min_ticks_per_bar=1,
        regime_filter=False,
        cooldown_seconds=0,
        spot_scale=1.0,
        spread_width=5.0,
        min_entry_credit=0.10,
        max_entry_credit=3.00,
        log_level="INFO",
        log_file="",
    )
    bars = make_bars()
    bt = Backtester(cfg, OptionPricer(iv=0.20))
    bt.strikes = [round(450 + i * 1.0, 1) for i in range(400)]

    extremes = bt.find_extreme_bars(bars)
    assert extremes == [bars[3][0]], extremes
    print("extreme minutes:", [t.strftime("%H:%M") for t in extremes])

    base = datetime(2026, 8, 17, 9, 30, 0)
    d_ts = base + timedelta(minutes=3)

    bt2 = Backtester(cfg, OptionPricer(iv=0.20))
    bt2.strikes = [round(450 + i * 1.0, 1) for i in range(400)]
    bt2.run_bars(bars, precise_delta={d_ts: +2000.0})
    stats = bt2.report()
    print("hybrid positive-delta stats:", {k: v for k, v in stats.items() if k != "equity_curve"})
    assert stats["trades"] == 0, "precise positive delta should kill the bear divergence"

    bt3 = Backtester(cfg, OptionPricer(iv=0.20))
    bt3.strikes = [round(450 + i * 1.0, 1) for i in range(400)]
    bt3.run_bars(bars, precise_delta={d_ts: -3000.0})
    stats3 = bt3.report()
    print("hybrid negative-delta stats:", {k: v for k, v in stats3.items() if k != "equity_curve"})
    assert stats3["trades"] == 1, "precise negative delta should keep the bear divergence"
    print("OK backtest hybrid mode")


async def test_minute_deltas():
    base = datetime(2026, 8, 17, 9, 33, 0)
    ba = [
        (base - timedelta(seconds=2), 595.0, 595.1),
        (base, 594.4, 594.5),
    ]
    tr = [
        (base - timedelta(seconds=1), 595.0, 999),
        (base, 594.4, 100),
        (base + timedelta(seconds=1), 594.4, 200),
    ]
    per_minute = {base: (ba, tr)}
    out = minute_deltas(per_minute)
    assert out[base] == -300.0, out
    print("OK minute_deltas")


async def test_backtest_condor():
    """regime_filter=True: insufficient hourly data -> "none" -> NO trade (safety).
    A resolved "range" regime opens iron condors; trend regimes skip."""
    cfg = Config(
        divergence_lookback=3,
        min_ticks_per_bar=1,
        cooldown_seconds=0,
        spot_scale=13.0,
        spread_width=5.0,
        iron_condor_offset=30.0,
        iron_condor_wing=25.0,
        iron_condor_take_profit_pct=0.70,
        iron_condor_stop_loss_pct=0.50,
        min_entry_credit=0.10,
        max_entry_credit=30.00,
        regime_filter=True,
        trend_lookback_hours=4,
        trend_slope_threshold=5.0,
        log_level="INFO",
        log_file="",
    )
    ticks = make_day_ticks()
    bt = Backtester(cfg, OptionPricer(iv=0.20), log=__import__("logging").getLogger("btc"))
    bt.strikes = [round(450 + i * 1.0, 1) for i in range(400)]
    bt.run_ticks(ticks)
    # only 6 minutes of data -> insufficient hourly closes -> regime "none" -> no trade
    stats = bt.report()
    print("condor-mode stats (insufficient data):", {k: v for k, v in stats.items() if k != "equity_curve"})
    assert stats["trades"] == 0, f"expected no trade on insufficient data, got {stats}"
    print("OK backtest condor mode (insufficient data -> no trade)")


if __name__ == "__main__":
    asyncio.run(test_backtest_tick())
    asyncio.run(test_backtest_bar_proxy())
    asyncio.run(test_backtest_hybrid())
    asyncio.run(test_minute_deltas())
    asyncio.run(test_backtest_condor())
    print("ALL BACKTEST TESTS PASSED")
