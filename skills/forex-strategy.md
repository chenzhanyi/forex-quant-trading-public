<!--
skill: forex-strategy
description: EUR/USD 交易策略完整参数、规则与工作流参考
origin: 改编自 Hermes skill "forex-daily-signal"
usage: |
  作为 Claude CLI 周复盘/策略分析 prompt 的前缀上下文。
  关联脚本: scripts/run_weekly_review.sh, scripts/cron_morning.sh
  关联模块: src/strategy/signal.py, src/strategy/trend.py, src/strategy/entry.py
-->

# EUR/USD 交易策略参考

## 账户参数

| 参数 | 值 |
|------|-----|
| 账户余额 | $100 |
| 杠杆 | 1:100 |
| 固定仓位 | 0.01 手 (1,000 单位) |
| 每 pip 价值 | $0.10 |
| 止损 25 pips | $2.50 (账户 2.5%) |
| 止损 50 pips | $5.00 (账户 5.0%) |

## 策略架构

```
D1 50SMA ──→ 大势方向判断（做多/做空/观望）
    │
H4 200SMA ──→ 中期趋势确认 + 斜率动量过滤
    │
M15 5EMA/15EMA ──→ 入场信号（金叉做多/死叉做空）+ K线形态确认
    │
风控层: ATR波动率(M15) / R:R≥2 / Fibonacci回调(H4) / 追高保护(H4)
```

> 📌 入场唯一时间框架: M15。D1+H4 负责趋势方向，M15 负责入场执行。H1 不在入场链路中。

## 趋势判断 (Trend)

- **D1 50SMA**: 价格 > SMA = UP, 价格 < SMA = DOWN
- **H4 200SMA**: 同 D1 逻辑，与 D1 共振确认大势
- **共振规则**:
  - D1 UP + H4 UP → 明确看多
  - D1 DOWN + H4 DOWN → 明确看空
  - D1 UP + H4 SIDEWAYS → 谨慎看多
  - D1 DOWN + H4 SIDEWAYS → 谨慎看空
  - 其他组合 → 观望

## 入场条件 (Entry) — M15 四步入场法

> 📌 D1+H4 判大势方向 → M15 四步精准入场。止损用 M15 波段极值点，止盈看 H4 前高/低 + Fib 延伸。

### 做空四步

```
1️⃣ M15 反弹到最高点（swing high）→ 标记止损参考
2️⃣ EMA5/EMA15 出现死叉 → 反弹结束确认
3️⃣ 死叉后小回弹但不破前高 → retest 有效
4️⃣ 回弹处出现反转形态（黄昏星/射击之星/阴吞阳）→ 入场
```

- 止损 = M15 swing high + 3 pips
- 止盈 TP1 = H4 前低, TP2 = Fib 161.8% 延伸

### 做多四步（镜像）

```
1️⃣ M15 回调到最低点（swing low）→ 标记止损参考
2️⃣ EMA5/EMA15 出现金叉 → 回调结束确认
3️⃣ 金叉后小回踩但不破前低 → retest 有效
4️⃣ 回踩处出现反转形态（启明星/锤子线/阳吞阴）→ 入场
```

- 止损 = M15 swing low - 3 pips
- 止盈 TP1 = H4 前高, TP2 = Fib 161.8% 延伸

### 过滤条件

| 过滤器 | 来源 | 说明 |
|--------|------|------|
| H4 斜率动量 | H4 200SMA | 斜率 < slope_momentum → 震荡不交易 |
| ATR 波动率 | M15 | ATR < 20均×0.8 → 无量不交易 |
| 追高保护 | H4 200SMA | 入场价距 SMA > 1.5×ATR → 不追 |
| RSI 极值 | M15 | RSI<25(做空)/RSI>75(做多) → 极端不追 |
| 止损宽度 | M15 波段 | SL > 40pips → 不适合 $100 账户 |

## 风控参数 (Risk)

| 参数 | 值 | 说明 |
|------|-----|------|
| rr_min_ratio | 2.0 | 最小盈亏比 |
| risk_per_trade | 0.02 | 单笔风险上限 (2%) |
| stop_loss.method | widest | ATR/结构/EMA 取最宽 |
| stop_loss.atr_multiplier | 1.5 | ATR 止损倍数 |
| stop_loss.fallback_pips | 20 | 兜底止损 |

## 特殊规则

- **周五规则**: 周五不生成新交易信号（`friday_no_trade: true`）
- **重大事件静默**: 重大经济事件前 24h 降低信号置信度（`warning_hours: 24`）
- **连续亏损保护**: 连续 3 笔亏损后冷却 24h

## 监控时段 (GMT+8)

| 时间 | 活动 |
|------|------|
| 05:00 | 后台数据采集 |
| 07:00-08:00 | 🔴 生成早间信号 |
| 09:00 | 中国开盘检查 |
| 15:00 | 欧洲开盘扫描 |
| 22:00 | 🔴 晚间简报 + 持仓检查 |
