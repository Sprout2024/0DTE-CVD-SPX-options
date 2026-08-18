from __future__ import annotations

from datetime import date, timedelta

from ib_insync import Contract, Future, Stock

from config import Config


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7
    first_fri = first + timedelta(days=offset)
    return first_fri + timedelta(days=14)


def front_month_expiry(d: date = None) -> str:
    """YYYYMM of the next quarterly futures expiry strictly after ``d``."""
    d = d or date.today()
    for year in (d.year, d.year + 1):
        for m in (3, 6, 9, 12):
            if _third_friday(year, m) > d:
                return f"{year}{m:02d}"
    raise RuntimeError("unreachable")


def make_signal_contract(cfg: Config) -> Contract:
    """Signal instrument for the CVD tick feed (e.g. ES futures or SPY stock)."""
    if cfg.signal_sec_type == "FUT":
        expiry = cfg.future_expiry or front_month_expiry()
        return Future(cfg.symbol, expiry, cfg.signal_exchange, currency=cfg.currency)
    stock = Stock(cfg.symbol, cfg.exchange, cfg.currency)
    stock.primaryExchange = cfg.primary_exchange
    return stock
