import asyncio
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ib_insync import ComboLeg, Contract, LimitOrder, MarketOrder, Option, Stock
from ib_insync.objects import OptionChain
from ib_insync.order import OrderStatus


class MockTicker:
    def __init__(self, contract):
        self.contract = contract
        self.bid = float("nan")
        self.ask = float("nan")
        self.modelGreeks = None
        self._bid_set = False
        self._ask_set = False


class MockFill:
    def __init__(self, execution):
        self.execution = execution


class MockExecution:
    def __init__(self, price):
        self.price = price


class MockTrade:
    def __init__(self, contract, order):
        self.contract = contract
        self.order = order
        self.fills = []
        self._done = False
        self.orderStatus = OrderStatus(status="PendingSubmit")
        self._fill_after = None

    def isDone(self):
        return self._done


class MockIB:
    def __init__(self, spot=592.0):
        self.spot = spot
        self.pendingTickersEvent = MockEvent()
        self.tickers: Dict[int, MockTicker] = {}
        self.trades = []
        self._next_conid = 1000
        self.chain = OptionChain(
            exchange="SMART", underlyingConId=1, tradingClass="SPY",
            multiplier="100",
            expirations=["20260817", "20260818", "20260821"],
            strikes=[round(540 + i * 1.0, 1) for i in range(81)],
        )
        self.expiry = "20260817"

    async def connectAsync(self, *a, **k):
        return None

    def disconnect(self):
        pass

    def isConnected(self):
        return True

    async def reqPositionsAsync(self):
        return []

    def positions(self):
        return []

    async def qualifyContractsAsync(self, *contracts):
        for c in contracts:
            if not c.conId:
                self._next_conid += 1
                c.conId = self._next_conid
        return contracts

    async def reqSecDefOptParamsAsync(self, symbol, exchange, secType, conId):
        return [self.chain]

    def reqMarketDataType(self, *a, **k):
        pass

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False, mktDataOptions=None):
        key = id(contract)
        t = MockTicker(contract)
        self.tickers[key] = t
        if contract.secType == "OPT":
            self._seed_option(t, contract, genericTickList)
        else:
            t.bid = self.spot - 0.01
            t.ask = self.spot + 0.01
            t._bid_set = t._ask_set = True
        return t

    def _seed_option(self, t, contract, generic):
        r = random.Random(hash((contract.strike, contract.right)))
        otm = contract.strike - self.spot
        if contract.right == "C":
            delta = 0.5 - max(otm, 0.0) * 0.05
            price = max(0.05, 1.0 - max(otm, 0.0) * 0.05)
        else:
            delta = -(0.5 - max(-otm, 0.0) * 0.05)
            price = max(0.05, 1.0 - max(-otm, 0.0) * 0.05)
        if "106" in generic or "107" in generic or "108" in generic:
            t.modelGreeks = _MockGreeks(delta)
        t.bid = max(0.02, price - 0.03)
        t.ask = price + 0.03
        t._bid_set = t._ask_set = True

    def cancelMktData(self, contract):
        self.tickers.pop(id(contract), None)

    def placeOrder(self, contract, order):
        t = MockTrade(contract, order)
        if isinstance(order, MarketOrder):
            self._fill(t, self._spread_mid(contract))
        else:
            asyncio.get_running_loop().call_later(0.2, lambda: self._fill(t, order.lmtPrice))
        self.trades.append(t)
        return t

    def _fill(self, trade, price):
        if trade.isDone():
            return
        trade.fills = [MockFill(MockExecution(price))]
        trade.orderStatus = OrderStatus(status="Filled", filled=trade.order.totalQuantity, avgFillPrice=price, lastFillPrice=price)
        trade._done = True

    def _spread_mid(self, contract):
        total = 0.0
        for leg in contract.comboLegs:
            strike = self._strike_by_conid(leg.conId)
            if leg.action == "SELL":
                sign = 1.0
            else:
                sign = -1.0
            total += sign * self._opt_value(strike, self._right_by_conid(leg.conId))
        return round(total, 2)

    def _opt_value(self, strike, right):
        otm = strike - self.spot
        if right == "C":
            return max(0.05, 1.0 - max(otm, 0.0) * 0.05)
        return max(0.05, 1.0 - max(-otm, 0.0) * 0.05)

    def _right_by_conid(self, conid):
        for t in self.tickers.values():
            if t.contract.secType == "OPT" and t.contract.conId == conid:
                return t.contract.right
        return "C"

    def _strike_by_conid(self, conid):
        for t in self.tickers.values():
            if t.contract.secType == "OPT" and t.contract.conId == conid:
                return t.contract.strike
        return self.spot

    def cancelOrder(self, order):
        pass


class _MockGreeks:
    def __init__(self, delta):
        self.delta = delta
        self.impliedVol = 0.25
        self.gamma = 0.02
        self.vega = 0.3
        self.theta = 0.05


class MockEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, h):
        self.handlers.append(h)
        return self

    def emit(self, *a, **k):
        for h in self.handlers:
            h(*a, **k)
