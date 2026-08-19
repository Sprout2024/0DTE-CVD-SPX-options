# 震荡趋势下的 Iron Condor 0DTE SPY（v2.0）

基于 **1 分钟窗口趋势判定 + Iron Condor（铁鹰）** 的 SPX 0DTE 期权日内策略。

- **信号源**：**ES 期货**（CME）逐笔成交，按「成交价 vs 买一/卖一」规则累积 CVD，聚合成 1 分钟 bar
- **交易标的**：**SPX 指数期权**（CBOE，0DTE 周选 SPXW 链）
- **核心思想**：只在**震荡趋势（range）**下卖 Iron Condor 收权利金，用 θ 衰减收割；趋势日通过多层过滤回避，不做方向性方向

> 版本：v2.0.0 ｜ 上一版 v1.0.0 为「CVD 背离信号 + 方向性价差」策略，v2.0 重构为「震荡趋势铁鹰 + 多仓 + 风控过滤」。

## 策略逻辑（v2.0）

### 1. 入场触发：1 分钟窗口趋势判定（`regime_min_window`）
- 取最近 `regime_min_window`（30）根 1 分钟收盘价，线性回归求斜率
- **|斜率| ≤ `regime_min_slope`（0.15 点/分）→ range（震荡）**，可开仓
- up / down / 数据不足（none）→ 不开仓
- 数据不足一律视为 `none`（不交易），避免开盘前 30 分钟无脑入场

### 2. 多层过滤（都通过才开仓）
| 过滤器 | 参数 | 作用 |
|---|---|---|
| 前一天趋势 | `prev_day_trend_filter`（15m 斜率 ≤2.0） | 前一天收盘非震荡 → 次日不开 |
| 开盘波动 | `open_vol_filter`（开盘 30 分钟波幅 ≤4 点） | 开盘剧烈波动 → 当日不开 |
| 当日 SL 上限 | `max_daily_sl`（2） | 当日累计 2 笔 SL → 当日停止新开仓 |

- 前一天趋势决策写入 `data/trade_decision.json`（可用 `trade_decision.py` 生成），策略直接读取；缺失则自动生成

### 3. Iron Condor 入场
- 以当前 SPX 指数价 `spot` 为锚：卖 `spot±offset(30)`，买 `spot±(offset+wing=55)`
- 每笔 1 手（`contracts=1`），权利金须在 `[1.00, 30.00]`
- **多仓**：`max_position=5`，每笔入场后 `cooldown_seconds`（600s=10分钟）才可再开，直到满仓
- 入场用 **IBKR Adaptive Limit**（SELL 限价锚定「买一价」，区间 `(买一, mid)`）

### 4. 离场（每仓独立管理）
- **TP**：净值 ≤ 0.30× 权利金（锁 70% 利润）
- **SL**：净值 ≥ 1.50× 权利金（翅膀被击穿）
- **EOD**：16:00 ET 全局强平兜底
- 平仓用 **IBKR Adaptive Limit**（BUY 限价锚定「卖一价」，区间 `(mid, 卖一)`）；SL/TECH 也走 Adaptive urgent，超时升级市价单

## 目录结构

```
├── main.py                # 实盘/纸面交易入口
├── strategy.py            # 策略主循环（range判定→开仓→管理→重连→恢复）
├── cvd_engine.py          # 1 分钟 bar 构建 + CVD 计算
├── options.py             # SPX 期权链选择、铁鹰构建与定价
├── executor.py            # 下单 / 多仓管理 / 机械离场 / Adaptive 限价
├── contracts.py           # 信号合约解析（ES 期货前月）
├── state_store.py         # CVD bar 与多持仓状态持久化（JSONL）
├── trend.py               # 趋势判定（小时级）
├── hist_data.py           # IBKR 历史 tick/bar 拉取
├── backtest.py            # 回测引擎 + BS 期权定价器
├── run_backtest.py        # 回测 CLI（单仓）
├── run_spy_year.py        # 一年期多仓回测 CLI（含 cooldown / SL限制 / 双过滤）
├── fetch_spy_history.py   # SPY 历史数据拉取
├── trade_decision.py      # 生成当日是否开仓决策文件
├── logger.py              # 日志配置
├── config.py              # 集中配置
└── tests/                 # 单元 / mock 测试
```

## 安装

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # ib-insync==0.9.86
```

需要运行中的 **TWS / IB Gateway**（纸面交易默认端口 4002，实盘 7497）。

## 运行

### 纸面 / 实盘交易

```bash
.venv/bin/python main.py --port 4002 --client-id 30 --market-data-type 1
```

- `--market-data-type 1` = 实时（需已订阅 `US Securities Snapshot and Futures Value Bundle`）
- `--state-file data/cvd_state.jsonl` 状态持久化（重启/断线后恢复 CVD 与多持仓）
- 运行中 `kill <pid>`（SIGTERM）优雅平仓退出；断线自动重连

### 生成当日是否开仓决策（前一天趋势过滤）

```bash
.venv/bin/python trade_decision.py              # 默认判断今天（用昨天数据）
.venv/bin/python trade_decision.py --date 2026-08-19
```

输出 `data/trade_decision.json`（保留最近 10 个交易日记录）：
```json
{ "trade_day": "2026-08-19", "decision": "open"|"skip", "prev_day_15m_slope": 0.82 }
```

### 一年期多仓回测

```bash
.venv/bin/python fetch_spy_history.py --start 2025-08-01 --end 2026-08-17 --cache data/spy_1y.pkl
.venv/bin/python run_spy_year.py --cache data/spy_1y.pkl --detail
```

### 测试

```bash
.venv/bin/python tests/test_trend.py
.venv/bin/python tests/test_backtest.py
.venv/bin/python tests/test_flow.py
.venv/bin/python tests/test_store.py
```

## 关键配置（`config.py`，v2.0 默认）

| 参数 | 默认 | 说明 |
|------|------|------|
| `symbol` / `signal_sec_type` | ES / FUT | 信号合约（CVD） |
| `option_symbol` / `option_exchange` | SPX / CBOE | 期权标的 |
| `regime_min_window` | 30 | 1 分钟窗口趋势判定 bar 数 |
| `regime_min_slope` | 0.15 | range 判定斜率阈值（点/分） |
| `open_vol_filter` | 4.0 | 开盘 30 分钟波幅过滤（点） |
| `open_vol_window_minutes` | 30 | 开盘波动窗口 |
| `prev_day_trend_filter` | 2.0 | 前一天 15m 斜率过滤（点/15min） |
| `prev_day_trend_window_minutes` | 120 | 前一天收盘段窗口 |
| `iron_condor_offset` | 30.0 | 铁鹰短腿偏移 |
| `iron_condor_wing` | 25.0 | 铁鹰翼宽 |
| `iron_condor_take_profit_pct` | 0.70 | TP（锁 70% 权利金） |
| `iron_condor_stop_loss_pct` | 0.50 | SL（净值 1.5× 权利金） |
| `max_position` | 5 | 最大并发持仓 |
| `cooldown_seconds` | 600 | 每笔入场后 cooldown（10 分钟） |
| `max_daily_sl` | 2 | 当日 SL 上限，达 2 笔停止新开仓 |
| `contracts` | 1 | 每笔手数 |

## 实盘注意事项

- **SPX 期权最小报价单位**：权利金 ≥ \$3 为 0.05，< \$3 为 0.10，入场/离场限价已按此取整（`Executor._snap_tick`）
- **Riskless combination 拒绝**：IBKR 可能把信用价差组合单判为 riskless combination 拒绝（Error 201），用 `advancedErrorOverride="COMBOPAYOUT"` 绕过
- **指数期权链**：`reqSecDefOptParamsAsync` 的 `futFopExchange` 传空串，0DTE 优先周选 SPXW 链
- **数据订阅**：实时行情需 `US Securities Snapshot and Futures Value Bundle`（NP,L1）
- **资金**：满仓 5 个铁鹰峰值保证金约 $12,500（翼宽 25 × $100），建议账户 $20,000+

## 状态持久化

数据文件统一存于 `data/`（已 gitignore）：

- **`data/cvd_state.jsonl`**（`--state-file`）：每分钟 bar（含 CVD）+ 多持仓快照（`pos_snapshot`），重启后恢复
- **`data/trade_decision.json`**：当日是否开仓决策（前一天趋势过滤）
- **`data/signals.jsonl`**：检测到的 CVD 信号日志（分析用，不驱动入场）

## 回测表现（v2.0，2025-08 至 2026-08，262 交易日）

| 指标 | 数值 |
|---|---|
| 参与日 | 143 |
| 总笔数 | 973（平均 6.8 笔/日） |
| 胜率 | 85% |
| 净盈亏 | +$289,624 |
| 平均/笔 | +$298 |
| 最大回撤（逐笔） | -$5,540 |
| 最大连续亏损 | 11 笔 |
| 峰值资金占用 | $12,500（满仓 5） |

> 回测基于 SPY 1 分钟数据（×10 映射到 SPX）+ BS 定价器，为名义盈亏（每笔 1 手）。单仓策略（v2.0 前）为 80% 胜率 / +$53,867。
