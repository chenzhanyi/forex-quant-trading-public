#!/bin/bash
# ============================================================
# 外汇量化交易 — 复盘脚本
# ============================================================
# 用法: bash scripts/cron_review.sh [天数]
#   默认: 7 天
#   例:   bash scripts/cron_review.sh 14
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

DAYS="${1:-7}"
mkdir -p output/reviews output/logs

LOG="output/logs/review_$(date +%Y%m%d).log"

echo "=========================================="
echo "📋 EUR/USD 复盘报告（近 ${DAYS} 天）"
echo "    $(date '+%Y-%m-%d %H:%M') GMT+8"
echo "=========================================="

python3 -c "
from src.strategy.review import ReviewEngine
engine = ReviewEngine()
report = engine.generate_review(days=${DAYS})
print(report)
engine.save_review(days=${DAYS})
print()
print(f'✅ 复盘报告已保存至 output/reviews/')
" 2>&1 | tee "$LOG"

echo "=========================================="
