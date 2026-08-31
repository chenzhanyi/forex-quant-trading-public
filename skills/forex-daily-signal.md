<!--
⚠️ DEPRECATED — Hermes 专用版本，已不再使用。
👉 本地化替代: skills/forex-strategy.md
👉 周复盘脚本: bash scripts/run_weekly_review.sh
-->

# Forex Daily Signal (Hermes 原版 · 已弃用)

## Description
Generate and deliver EUR/USD daily trading signal for the user at 08:00 GMT+8.

## Usage
Run every trading day morning at 08:00 GMT+8 to generate the daily signal. The user reads it and decides whether to place orders manually.

## Workflow
1. Ensure OANDA data is up to date (run data collection first)
2. Run trend analysis (D1 + H4 50 SMA)
3. Check entry signal (M15 5EMA/15EMA) — ⚠️ 此文件已弃用，参见 skills/forex-strategy.md
4. Apply risk check ($100 / 0.01 lot / max loss 1%)
5. Format as Markdown signal report
6. Deliver to user (Telegram / Hermes chat)

## Commands
```bash
# Full signal generation
cd /path/to/外汇量化交易 && python -c "
from src.strategy.signal import SignalGenerator
gen = SignalGenerator()
sig = gen.generate_daily()
print(gen.format_markdown(sig))
gen.save_signal(sig)
"
```

## User Account
- Balance: $100
- Leverage: 1:100
- Position: 0.01 lot (1,000 units) fixed
- Pip value: $0.10

## Monitoring Windows (GMT+8)
| Time | Activity |
|------|----------|
| 05:00 | Background data collection (US close) |
| 07:00-08:00 | 🔴 **Generate morning signal** |
| 09:00 | China open check |
| 15:00 | Europe open scan |
| 22:00 | 🔴 Evening briefing + position check |

## Key Rules
- D1 + H4 must agree before trading
- ATR volatility filter: skip if ATR < 20-bar average × 0.8
- R:R must be ≥ 2.0
- Max loss per trade ≤ $5.00 (5% of account)
- Stop trading after 3 consecutive losses (24h cooldown)
