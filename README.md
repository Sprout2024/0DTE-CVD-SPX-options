# 0DTE CVD SPX Options

基于 **CVD（累积成交量差）背离** 的 SPX 0DTE 期权价差日内剥头皮策略。

- 信号源：**ES 期货**（CME）的逐笔成交，按「成交价 vs 买一/卖一」规则累积 CVD
- 交易标的：**SPX 指数期权**（CBOE，0DTE 周选 SPXW 链）
- 逻辑：价格创新高/新低但 CVD 未跟随（买/卖动量衰竭）→ 卖方向性信用价差

## 策略逻辑

1. 将 ES 逐笔成交聚合成 **1 分钟 bar**，同时累积 CVD（`cvd_close`）
2. 检测 **背离**（`divergence_lookback` 根 bar）：
   - **空头背离**：价格创新高，但 CVD 显著低于前高对应水平 → 卖 **bear call 价差**（赌回落）
   - **多头背离**：价格创新低，但 CVD 显著高于前低对应水平 → 卖 **bull put 价差**（赌反弹）
3. 离场优先级：
   - **SL**：价差 mid ≥ 2× 权利金
   - **TP**：价差 mid ≤ 0.70× 权利金
   - **技术止损**：价格突破信号极值 ± 缓冲且 CVD 反向确认
   - **时间**：持有满 `hard_time_seconds` 强平；或满 `profit_time_seconds` 且已浮盈

## 目录结构

```
├── main.py                # 实盘/纸面交易入口
├── strategy.py            # 策略主循环（信号→开仓→管理→重连→恢复）
├── cvd_engine.py          # 1 分钟 bar 构建 + CVD 背离检测
├── options.py             # SPX 期权链选择、价差构建与定价
├── executor.py            # 下单 / 持仓管理 / 机械离场
├── contracts.py           # 信号合约解析（ES 期货前月）
├── state_store.py         # CVD bar 与持仓状态持久化（JSONL）
├── hist_data.py           # IBKR 历史 tick/bar 拉取
├── backtest.py            # 回测引擎 + BS 期权定价器
├── run_backtest.py        # 回测 CLI
├── analyze_divergence.py  # 多天背离信号统计
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
- `--state-file cvd_state.jsonl` 状态持久化文件（重启/断线后恢复 CVD 与持仓）
- 运行中 `kill <pid>`（SIGTERM）会优雅平仓退出；断线自动重连

### 回测

```bash
# tick 模式（真实 BID_ASK/TRADES CVD）
.venv/bin/python run_backtest.py --days 3 --data tick --cache bt.pkl

# hybrid 模式（bar 代理 + 极值分钟精确 tick）
.venv/bin/python run_backtest.py --days 3 --data hybrid --cache bt.pkl

# 离线回放缓存
.venv/bin/python run_backtest.py --offline --cache bt.pkl
```

### 多天信号统计

```bash
.venv/bin/python analyze_divergence.py --days 6 --port 4002
```

### 测试

```bash
.venv/bin/python tests/test_backtest.py
.venv/bin/python tests/test_flow.py
.venv/bin/python tests/test_store.py
.venv/bin/python tests/test_strategy.py
```

## 关键配置（`config.py`）

| 参数 | 默认 | 说明 |
|------|------|------|
| `symbol` / `signal_sec_type` / `signal_exchange` | ES / FUT / CME | 信号合约 |
| `option_symbol` / `option_sec_type` / `option_exchange` | SPX / IND / CBOE | 期权标的 |
| `bar_seconds` | 60 | bar 周期 |
| `divergence_lookback` | 10 | 背离回看 bar 数 |
| `min_price_move` / `min_cvd_gap` | 0.05 / 1.0 | 背离触发阈值 |
| `spread_width` | 50 | 价差宽度 |
| `strike_band` / `target_delta` | 120 / 0.30 | 行权价选择 |
| `min/max_entry_credit` | 1.00 / 30.00 | 权利金范围 |
| `take_profit_pct` / `stop_loss_pct` | 0.30 / 1.00 | 止盈止损 |
| `profit_time_seconds` / `hard_time_seconds` | 300 / 600 | 时间离场 |
| `contracts` | 1 | 手数 |

## 实盘注意事项

- **SPX 期权最小报价单位**：权利金 ≥ \$3 为 0.05，< \$3 为 0.10，入场/离场限价已按此取整（`Executor._snap_tick`）
- **Riskless combination 拒绝**：IBKR 可能把信用价差组合单判为「riskless combination」拒绝（Error 201）。已用 `order.advancedErrorOverride="COMBOPAYOUT"`（Transmit anyway）绕过
- **指数期权链**：`reqSecDefOptParamsAsync` 的 `futFopExchange` 需传空串，0DTE 优先选周选 SPXW 链
- **数据订阅**：实时行情需 `US Securities Snapshot and Futures Value Bundle`（NP,L1）

## 状态持久化

`cvd_state.jsonl`（已 gitignore）逐行记录：
- `{"k":"bar",...}` 每分钟已完成 bar（含 CVD 累计值）
- `{"k":"pos",...}` / `{"k":"pos_done"}` 开仓 / 平仓

重启 / 断线重连时回放当日 bar 重建 CVD，并从 IBKR 同步未跟踪持仓（孤儿持仓自动市价平掉）。
