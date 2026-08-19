"""拉取 SPY 1 分钟历史数据（逐日拉取并即时缓存到 pickle），用于一年期回测。

用法:
    .venv/bin/python fetch_spy_history.py --start 2025-08-01 --end 2026-08-17 --cache data/spy_1y.pkl
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pickle
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock

ET = ZoneInfo("America/New_York")


async def fetch_day(ib, spy, day: date, retries: int = 3) -> list:
    end = datetime(day.year, day.month, day.day, 0, 0, tzinfo=ET) + timedelta(days=1)
    for attempt in range(retries):
        try:
            bars = await ib.reqHistoricalDataAsync(
                spy, end, "2 D", "1 min", "TRADES", True, formatDate=1, timeout=30
            )
            if bars:
                out = []
                for b in bars:
                    t = b.date.astimezone(ET).replace(tzinfo=None)
                    if t.date() != day or t.time() > datetime(2000, 1, 1, 16, 0).time():
                        continue
                    out.append((t.replace(second=0, microsecond=0),
                                float(b.open), float(b.high), float(b.low),
                                float(b.close), float(b.volume)))
                if out:
                    return sorted(out)
        except Exception as e:
            logging.warning("day %s attempt %d failed: %s", day, attempt + 1, e)
        await asyncio.sleep(1.5)
    return []


def trading_days(start: date, end: date) -> list:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


async def main(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.makedirs("data", exist_ok=True)
    cache = {}
    if os.path.exists(args.cache):
        cache = pickle.load(open(args.cache, "rb"))
        logging.info("loaded %d cached days from %s", len(cache), args.cache)

    ib = IB()
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=20)
    spy = Stock("SPY", "SMART", "USD")
    spy.primaryExchange = "ARCA"
    await ib.qualifyContractsAsync(spy)

    days = trading_days(args.start, args.end)
    logging.info("%d trading days to fetch", len(days))

    fetched = 0
    for i, day in enumerate(days, 1):
        key = day.isoformat()
        if key in cache:
            continue
        raw = await fetch_day(ib, spy, day)
        if raw:
            cache[key] = raw
            fetched += 1
        else:
            logging.warning("day %s: NO DATA", day)
        if i % 10 == 0:
            pickle.dump(cache, open(args.cache, "wb"))
            logging.info("progress %d/%d  fetched+%d  total=%d", i, len(days), fetched, len(cache))
        await asyncio.sleep(0.2)

    pickle.dump(cache, open(args.cache, "wb"))
    logging.info("done: %d days cached to %s", len(cache), args.cache)
    ib.disconnect()
    return 0


def parse_args():
    p = argparse.ArgumentParser(description="SPY 1 分钟历史数据逐日拉取")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002)
    p.add_argument("--client-id", type=int, default=112)
    p.add_argument("--start", type=lambda s: date.fromisoformat(s), default=date(2025, 8, 1))
    p.add_argument("--end", type=lambda s: date.fromisoformat(s), default=date(2026, 8, 17))
    p.add_argument("--cache", default="data/spy_1y.pkl")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))