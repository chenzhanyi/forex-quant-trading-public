#!/bin/bash
# ============================================================
# 外汇量化交易 — 早间信号（cron 07:30 GMT+8）
# 生成信号 + 推送飞书
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

mkdir -p output/signals output/logs

LOG="output/logs/morning_$(date +%Y%m%d).log"

echo "========================================" | tee "$LOG"
echo "📊 EUR/USD 交易信号" | tee -a "$LOG"
echo "    $(date '+%Y-%m-%d %H:%M') GMT+8" | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"

# 生成信号
python3 -c "
from src.strategy.signal import SignalGenerator
gen = SignalGenerator()
sig = gen.generate()
output = gen.format_markdown(sig)
gen.save(sig)
print(output)

# 添加基本面
from src.analysis.fundamentals import FundamentalsAnalyzer
fa = FundamentalsAnalyzer()
print()
print(fa.generate_summary(7))
" 2>&1 | tee "$LOG.tmp"

cat "$LOG.tmp" | tee -a "$LOG"

# 推送到飞书（取前 1500 字）
python3 -c "
import subprocess
with open('$LOG.tmp') as f:
    content = f.read()
msg = content[:1500]
subprocess.run(['bash', 'scripts/feishu_push.sh', msg])
" 2>&1 | tee -a "$LOG"

rm -f "$LOG.tmp"

echo "========================================" | tee -a "$LOG"
