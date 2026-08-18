from __future__ import annotations

import asyncio
import pickle
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from ib_insync import IB, Contract, Stock

ET = ZoneInfo("America/New_York")


def _et(y: int, m: int, d: int, hour: int, minute: int) -> datetime:
    return datetime(y, m, d, hour, minute, tzinfo=ET)


def _to_naive_et(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(ET)
        return dt.replace(tzinfo=None)
    return dt


def trading_days(end: date, days: int) -> List[date]:
    out = []
    d = end
    while len(out) < days:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


class HistoricalLoader:
    """Fetches historical SPY ticks (BID_ASK + TRADES) or 1-min bars from IBKR."""

    def __init__(self, ib: IB, log, chunk_minutes: int = 15, concurrency: int = 8):
        self.ib = ib
        self.log = log
        self.chunk_minutes = chunk_minutes
        self.sem = asyncio.Semaphore(concurrency)

    async def fetch_day_ticks(
        self, spy: Stock, day: date, max_requests: int = 500,
    ) -> Optional[Tuple[List[Tuple], List[Tuple]]]:
        start = _et(day.year, day.month, day.day, 9, 30)
        end = _et(day.year, day.month, day.day, 15, 45)
        chunks = []
        cur = start
        while cur < end:
            nxt = min(cur + timedelta(minutes=self.chunk_minutes), end)
            chunks.append((cur, nxt))
            cur = nxt
        self.log.info("fetching %d chunks for %s", len(chunks), day)
        results = await asyncio.gather(
            *[self._fetch_chunk(what, spy, c0, c1, max_requests)
              for what in ("BID_ASK", "TRADES")
              for c0, c1 in chunks]
        )
        bid_ask: List[Tuple] = []
        trades: List[Tuple] = []
        for what, part in results:
            if part:
                if what == "BID_ASK":
                    bid_ask.extend(part)
                else:
                    trades.extend(part)
        if not bid_ask:
            self.log.warning("no BID_ASK data for %s, using TRADES tick-rule", day)
        bid_ask.sort(key=lambda x: x[0])
        trades.sort(key=lambda x: x[0])
        if not trades:
            return None
        return bid_ask, trades

    async def _fetch_chunk(self, what: str, spy: Stock, start: datetime, end: datetime, max_requests: int) -> Tuple[str, List[Tuple]]:
        async with self.sem:
            out = await self._page(what, spy, start, end, max_requests)
            return what, out

    async def _page(self, what: str, spy: Stock, start: datetime, end: datetime, max_requests: int) -> List[Tuple]:
        out: List[Tuple] = []
        cur = start
        requests = 0
        while cur < end and requests < max_requests:
            requests += 1
            batch = await self.ib.reqHistoricalTicksAsync(
                spy, cur, end, 1000, what, True, False, []
            )
            if not batch:
                break
            last_ts = None
            for t in batch:
                ts = _to_naive_et(t.time)
                if what == "BID_ASK":
                    out.append((ts, float(t.priceBid), float(t.priceAsk)))
                else:
                    out.append((ts, float(t.price), float(t.size)))
                last_ts = ts
            if len(batch) < 1000 or last_ts is None:
                break
            next_cur = last_ts.replace(tzinfo=ET) + timedelta(microseconds=1)
            if next_cur <= cur:
                next_cur = cur + timedelta(seconds=1)
            cur = next_cur
            await asyncio.sleep(0.05)
        return out

    async def fetch_day_bars(self, spy: Contract, day: date) -> Optional[List[Tuple]]:
        """Fetch 1-min RTH bars for ``day``.

        IBKR interprets the historical ``endDateTime`` in the exchange's own
        timezone (UTC for futures, ET for stocks), so request a 1-week window
        ending the day after ``day`` and filter bars to the target day.
        """
        end = datetime(day.year, day.month, day.day, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)
        try:
            bars = await self.ib.reqHistoricalDataAsync(
                spy, end, "1 W", "1 min", "TRADES", True
            )
        except Exception as e:
            self.log.warning("bar fetch failed for %s: %s", day, e)
            return None
        if not bars:
            return None
        out = []
        for b in bars:
            ts = _to_naive_et(b.date)
            if ts.date() != day:
                continue
            ts = ts.replace(second=0, microsecond=0)
            out.append((ts, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)))
        return out or None

    async def fetch_minute_ticks(
        self, spy: Stock, minutes: List[datetime], max_requests: int = 30,
    ) -> Dict[datetime, Tuple[List[Tuple], List[Tuple]]]:
        """Fetch BID_ASK + TRADES ticks for specific 1-minute windows (concurrent)."""
        results = await asyncio.gather(
            *[self._fetch_minute(spy, m, max_requests) for m in minutes]
        )
        return {m: (ba, tr) for m, ba, tr in results}

    async def _fetch_minute(self, spy: Stock, m: datetime, max_requests: int):
        async with self.sem:
            start = m.replace(tzinfo=ET) - timedelta(seconds=1)
            end = m.replace(tzinfo=ET) + timedelta(minutes=1)
            ba = await self._page("BID_ASK", spy, start, end, max_requests)
            tr = await self._page("TRADES", spy, start, end, max_requests)
        return m, ba, tr


def merge_to_stream(
    bid_ask: List[Tuple], trades: List[Tuple],
) -> Tuple[List[Tuple], bool]:
    """Merge BID_ASK snapshots with TRADES into a (ts, price, delta) tick stream.

    Uses the live Bid/Ask rule when bid_ask is available, else a tick rule.
    """
    used_bid_ask = bool(bid_ask)
    ba = sorted(bid_ask)
    tr = sorted(trades)
    stream: List[Tuple] = []
    if used_bid_ask:
        i = 0
        cur_bid = None
        cur_ask = None
        for ts, price, size in tr:
            while i < len(ba) and ba[i][0] <= ts:
                _, cur_bid, cur_ask = ba[i]
                i += 1
            if cur_bid is None or cur_ask is None:
                continue
            if price >= cur_ask:
                delta = size
            elif price <= cur_bid:
                delta = -size
            else:
                delta = 0.0
            stream.append((ts, price, delta))
    else:
        last_price = None
        last_dir = 1.0
        for ts, price, size in tr:
            if last_price is None or price > last_price:
                last_dir = 1.0
            elif price < last_price:
                last_dir = -1.0
            stream.append((ts, price, last_dir * size))
            last_price = price
    return stream, used_bid_ask


def minute_deltas(per_minute: Dict[datetime, Tuple[List[Tuple], List[Tuple]]]) -> Dict[datetime, float]:
    """Sum the precise tick delta within each minute window (Bid/Ask rule)."""
    out: Dict[datetime, float] = {}
    for m, (ba, tr) in per_minute.items():
        stream, _ = merge_to_stream(ba, tr)
        start = m
        end = m + timedelta(minutes=1)
        out[m] = round(sum(delta for ts, price, delta in stream if start <= ts < end), 4)
    return out


def save_cache(path: str, data) -> None:
    with open(path, "wb") as f:
        pickle.dump(data, f)


def load_cache(path: str):
    with open(path, "rb") as f:
        return pickle.load(f)