#!/bin/bash
# ⚠️ DEPRECATED — Hermes 版本已弃用
# 👉 本地化替代: bash scripts/run_weekly_review.sh (Claude CLI)
# 👉 分析模块: src/analysis/weekly_review.py
# ============================================================
# Hermes 策略复盘 — 由 LLM 分析历史信号 + 提出优化建议
# ============================================================
# 每周五收盘后触发，分析本周信号表现
#
# 行为约束:
#   ✅ 只读分析产出/建议，不直接改文件
#   ✅ 如需改参数，输出修改建议到 output/analysis/
#   ❌ 不修改 config/ src/ 下的任何文件
#   ❌ 不执行 git commit/push
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

mkdir -p output/analysis output/logs

LOG="output/logs/hermes_review_$(date +%Y%m%d).log"

echo "==========================================" | tee "$LOG"
echo "📋 Hermes 策略周复盘" | tee -a "$LOG"
echo "    $(date '+%Y-%m-%d %H:%M') GMT+8" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"

hermes chat -q "
你是一个外汇量化交易策略分析师，正在对 EUR/USD 交易系统做周度复盘。

## 你的身份
- 你是理性的策略分析师
- 你基于实际交易数据和回测结果做判断
- 你的建议必须有数据支撑

## 你的任务
1. 读取 output/reviews/ 下的复盘报告
2. 读取 output/signals/ 下的历史信号
3. 分析本周信号表现（命中率、平均盈亏、最大回撤）
4. 评估当前策略参数是否合理（50SMA、5EMA/15EMA、ATR过滤、R:R比例）
5. 输出优化建议

## 行为约束
- ❌ 不要修改 config.yaml 或任何 src/ 下的文件
- ❌ 不要创建或删除数据文件
- ❌ 不要编造交易记录
- ✅ 只读分析
- ✅ 优化建议输出到 output/analysis/ 目录
- ✅ 如果建议修改参数，用 readable JSON 格式输出

## 输出格式

=== 策略周复盘 $(date +%Y%m%d) ===

📊 本周概况:
  总信号数: N
  命中数: N (胜率 X%)
  平均盈亏: X pips
  最大回撤: X pips

🔍 问题分析:
  [列出信号未命中的主要原因]

📈 参数评估:
  50SMA: [合理/需调整]
  5EMA/15EMA: [合理/需调整]
  ATR过滤: [合理/需调整]
  R:R 2:1: [合理/需调整]

💡 优化建议:
  [列出可操作的建议，每条一行]

⚠️ 注意事项:
  [风险提示]
" \
  --skills "外汇量化交易/skills/forex-daily-signal,外汇量化交易/skills/forex-evening-review" \
  --worktree \
  -v 2>&1 | tee -a "$LOG"

echo "==========================================" | tee -a "$LOG"
echo "✅ 周复盘完成" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"
