from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass
class Config:
    """Central configuration for the CVD 0DTE option scalping strategy."""

    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 30
    account: str = "U18853178"  # IBKR account id to trade; empty = default
    dry_run: bool = False  # True = observe only, never place real orders
    symbol: str = "ES"
    signal_sec_type: str = "FUT"
    signal_exchange: str = "CME"
    future_expiry: str = ""
    option_symbol: str = "SPX"
    option_sec_type: str = "IND"
    option_exchange: str = "CBOE"
    currency: str = "USD"
    exchange: str = "SMART"
    primary_exchange: str = "ARCA"
    market_data_type: int = 1
    spot_scale: float = 1.0

    bar_seconds: int = 60
    min_ticks_per_bar: int = 3
    divergence_lookback: int = 10
    min_price_move: float = 0.05
    min_cvd_gap: float = 1.0

    regime_filter: bool = True
    trend_lookback_hours: int = 4
    trend_slope_threshold: float = 5.0
    # 1-minute window range gate (replaces/extends hourly regime for condor entry)
    regime_min_window: int = 30
    regime_min_slope: float = 0.15  # |slope| <= this (points/min) => range
    # open-30min volatility filter (points): skip the day if first 30 min range exceeds this
    open_vol_filter: float = 4.0  # 0 = disabled
    open_vol_window_minutes: int = 30
    # SPX/SPY ratio used to normalize the signal range into SPY-equivalent points
    # for the open-vol filter. Signal ES/SPX (~10x SPY) => 10.0; signal SPY => 1.0.
    open_vol_scale: float = 10.0
    # prior-day trend filter (15-min closes slope, points/15min): skip if |slope| > this
    prev_day_trend_filter: float = 2.0  # 0 = disabled
    prev_day_trend_window_minutes: int = 120
    iron_condor_offset: float = 30.0
    iron_condor_wing: float = 25.0
    iron_condor_take_profit_pct: float = 0.70
    iron_condor_stop_loss_pct: float = 0.50

    breakout_lookback: int = 60
    breakout_slope_z: float = 2.0
    breakout_delta_ratio: float = 3.0
    breakout_min_conditions: int = 3
    breakout_cooldown: int = 30

    session_start: time = time(9, 30)
    session_end: time = time(16, 0)
    no_trade_first_minutes: int = 15
    no_trade_last_minutes: int = 30
    max_position: int = 3
    cooldown_seconds: int = 600
    max_daily_sl: int = 2
    min_signal_spacing: float = 0.30
    entry_valid_seconds: int = 45

    target_delta: float = 0.30
    delta_tolerance: float = 0.10
    spread_width: float = 50.0
    strike_band: float = 120.0
    strike_tolerance: float = 2.6
    max_entry_credit: float = 30.00
    min_entry_credit: float = 1.00
    mid_offset: float = 0.10
    close_offset: float = 0.10
    greeks_timeout: float = 6.0
    quote_wait_seconds: float = 4.0

    contracts: int = 1
    commission_per_contract: float = 0.65
    min_commission_per_order: float = 1.00
    exchange_fee_per_contract: float = 0.15

    take_profit_pct: float = 0.30
    stop_loss_pct: float = 1.00
    profit_time_seconds: int = 300
    hard_time_seconds: int = 600
    tech_stop_buffer: float = 0.10
    close_patience_seconds: int = 10

    log_level: str = "INFO"
    log_file: str = "cvd_strategy.log"
    state_file: str = "data/cvd_state.jsonl"
    signals_file: str = "data/signals.jsonl"
    trades_file: str = "data/trades.jsonl"  # per-trade open/close detail log
    day_stop_file: str = "data/day_stop.json"  # kill switch flag: strategy stops trading for the day
    reconnect_retry_seconds: float = 5.0
