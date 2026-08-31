# 会话记录 CHATLOG

> 记录每轮会话的关键活动和决策,便于异常退出后接上。
> 最新的在前。格式: 日期 — 类别 | 摘要

---

## 2026-08-21 — 黄金策略修复 & 回测 (本次会话)

### 会话目标
用户意外退出后接上,继续完善黄金交易策略稳定性 + 回测验证。

### 完成事项

1. **GBP/USD 回测探索** → **放弃**
   - 用户: gbpusd 也不看 RSI
   - 尝试: 固定SL / ATR止损 / 强形态 / EMA斜率 / 50SMA vs 200SMA,全方向皆亏
   - 结论: 无RSI对GBP/USD无正期望,删除脚本和数据

2. **黄金引擎稳定性修复** (commit)
   - 原子写入 `_save_state()`(防崩溃损坏)
   - 同bar内SL/TP用K线方向判断先后
   - `evaluate()` 复用detector减少重复加载

3. **回测无look-ahead确认** ✅
   - EMA用ewm递归、SMA/ATR用rolling,只依赖过去
   - H4数据 `h4[h4.index <= dt]` 正确截断

4. **黄金回测 → 发现RSI是关键**
   - 忠实还原用户策略(EUR/USD去RSI): -$165/90天, 20%胜率
   - 用户框架扫描(50-200点SL, RR2-3): 全亏
   - 加回RSI + ATR止损×2.5 + TP$100: **+$560/90天, 61%胜率**
   - **决定: 黄金改回带RSI的EUR/USD同机制并提交**

5. **数据获取**
   - 获取 XAU/USD M15/H4 数据 (OANDA, 走代理)
   - 获取过 GBP/USD 数据(后因放弃删除)

### 关键决策
- 黄金从"去RSI"改为"带RSI" — 回测数据驱动
- 止损从固定$15改为H4 ATR×2.5
- 止盈从SL×RR改为固定$100

### 待办
- [ ] 用户确认黄金实盘启用新策略
- [ ] 黄金模块当前 dashboard 状态 enabled=True, auto_trade=True(用户配置,需注意)

---

## 之前会话(概要,从git history重建)
- 黄金XAU/USD独立交易模块 (commit c3d370e)
- 黄金单笔手数可配置 lots=0.01 (commit eb9a600)
- 持仓缓存残留修复 (commit e0056ea)
- 中控台/日历/基本面超时中断处理 (commits 35be360, e4cade4)
- 防重复下单、MT4心跳、订单过期等 (commits a9bfebc, b3e289e, ba6a42f, 5fc9ee5)