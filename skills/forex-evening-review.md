<!--
⚠️ DEPRECATED — Hermes 专用版本，已不再使用。
👉 本地化替代: skills/forex-review-workflow.md
👉 周复盘脚本: bash scripts/run_weekly_review.sh
-->

# Forex Evening Review (Hermes 原版 · 已弃用)

## Description
Post-trading day review and analysis for EUR/USD trades. The user reports their operations, and the system generates a structured review report.

## Usage
Run after the user reports their trading operations for the day (typically around 22:00 GMT+8). Covers signal accuracy, entry precision, and risk management.

## Workflow
1. User tells Hermes their operations (e.g., "bought 0.01 at 1.08650, TP at 1.09000")
2. Load today's morning signal for comparison
3. Compare: signal direction vs actual market movement
4. Evaluate entry point accuracy
5. Check risk metrics (max drawdown, SL hit rate)
6. Generate structured review report
7. Save to output/reviews/

## Review Report Format
```
────────────────────────────────
📋 EUR/USD — 每日复盘
────────────────────────────────
📅 复盘日期:  2026-07-04
─── 信号回顾 ──────────────────
📈 大势判断:  D1↑ H4↑ 看多 ✅
💡 建议方向:  做多 ✅ 正确
📌 实际走势:  最低1.08620→最高1.08900 (+28pips)
📊 结果:      命中 ✅

─── 操作记录 ──────────────────
用户操作:     已入场 @1.08650
持仓状态:     TP1已到，剩余持有
浮盈:         +25 pips

─── 风控检查 ──────────────────
最大浮亏:     $1.00 (账户1%) ✅
止损:         未触发 ✅
R:R:          1:2.2 ✅

─── 明日展望 ──────────────────
D1 50SMA仍向上，大势不变
继续寻找做多机会
关注: 20:30美国CPI
────────────────────────────────
```

## Commands
```bash
# Quick review generation
cd /path/to/外汇量化交易 && python -c "
from src.strategy.review import DailyReview
r = DailyReview()
print(r.generate_markdown())
"
```

## Key Metrics Tracked
- Signal hit rate (daily/weekly/monthly)
- Average pip gain/loss per trade
- Win rate
- Max drawdown
- R:R achieved vs planned
