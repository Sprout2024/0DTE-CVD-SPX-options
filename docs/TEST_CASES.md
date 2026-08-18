# 测试用例说明：最终定稿策略验证（`validate_final.py`）

## 用途

`validate_final.py` 是最终定稿策略（**震荡日 Iron Condor + 趋势日空仓**）的验证样例，
用真实历史数据回放，检验策略逻辑是否符合设计意图。

## 最终策略规则（被验证的对象）

| 趋势 regime | 动作 |
|------------|------|
| range（震荡） | 开 Iron Condor（铁鹰） |
| up / down（趋势） | 空仓（不交易） |

**Iron Condor 参数（`config.py`）：**

| 参数 | 值 | 含义 |
|------|-----|------|
| `iron_condor_offset` | 30 | 短腿离现价 30 点 |
| `iron_condor_wing` | 25 | 买腿在短腿外 25 点 |
| `iron_condor_take_profit_pct` | 0.70 | 净值 ≤ 0.30×权利 → 止盈（锁 70%） |
| `iron_condor_stop_loss_pct` | 0.50 | 净值 ≥ 1.5×权利 → 止损 |
| 时间限制 | 无 | 持有到 SL/TP，16:00 ET 全局强平兜底 |

## 运行

```bash
# 默认验证 08-17 已知 16 个信号
.venv/bin/python validate_final.py

# 自动检测某日信号（任意日期）
.venv/bin/python validate_final.py --date 2026-08-10 --detect

# 指定 IB 端口（拉取缺失的历史数据）
.venv/bin/python validate_final.py --port 4002
```

## 测试用例设计

### 用例 1：震荡日 → Iron Condor 开仓（覆盖 #1-12）

- **前置**：`regime == "range"`（小时级趋势分类为震荡）
- **动作**：对背离信号开 4 腿 Iron Condor（卖 OTM call/put 价差）
- **断言**：开出铁鹰持仓；入场权利金 = 组合净值（`sim.value`）

### 用例 2：震荡日铁鹰 → 止盈（锁 70%）

- **前置**：铁鹰净值衰减到 ≤ 0.30×权利
- **动作**：TP 平仓
- **断言**：`kind == "TP"`，单笔盈利 ≈ (权利金 − 0.30×权利金)×100 − 6.40

### 用例 3：震荡日铁鹰 → 16:00 全局强平

- **前置**：持有至 16:00 ET 仍未触发 TP/SL
- **动作**：EOD 强平
- **断言**：`kind == "EOD"`，按 16:00 净值结算（用例 1 的 09:48 信号即此场景，+$374.60）

### 用例 4：趋势日 → 空仓（覆盖 #13-16）

- **前置**：`regime == "down"`（或 up）
- **动作**：信号出现
- **断言**：**不开任何仓位**，输出 `空仓`，`traded` 不增加

### 用例 5：趋势判定（小时级）

- **前置**：`trend_lookback_hours=4`、`trend_slope_threshold=5.0`
- **动作**：对最近 4 小时收盘价做线性回归斜率
- **断言**：斜率 > +5 → up；< -5 → down；否则 range（`test_trend.py` 覆盖）

### 用例 6：数据不足

- **前置**：当日 bar 不足 4 小时
- **动作**：`trend.regime()`
- **断言**：返回 `"range"`（保守处理，数据不足不交易方向）

## 08-17 验证结果（基线）

| 项 | 值 |
|----|-----|
| 总信号 | 16 |
| 震荡日铁鹰 | 12 笔，**12/12 胜（100%）** |
| 趋势日空仓 | 4 笔（13:23 后判 down） |
| 铁鹰净盈亏 | **+$4,574.20**（含 $76.80 成本） |
| 离场分布 | 11 TP + 1 EOD |

## 断言（可用脚本输出人工核对 / 自动化）

```python
# 08-17 基线期望
traded == 12
wins == 12
skipped == 4
total_pnl == 4574.20          # 含成本
# 每笔离场 kind 属于 {"TP", "EOD"}，无 SL（08-17 震荡日翅膀未被击穿）
```

> 注：BS 定价器（IV 0.20）用于模拟铁鹰净值，绝对金额是模型参考值；
> 但「震荡日全胜 + 趋势日空仓」的结构性结论独立于 IV 假设。

## 相关单元测试

- `tests/test_trend.py`：小时级趋势分类（up/down/range）
- `tests/test_strategy.py::test_regime_gate`：regime 门控（趋势日空仓）
- `tests/test_flow.py`、`tests/test_store.py`：期权/持久化基础逻辑
