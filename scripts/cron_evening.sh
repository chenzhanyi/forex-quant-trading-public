#!/bin/bash
# ============================================================
# 外汇量化交易 — 晚间简报（22:00 GMT+8 触发）
# ============================================================
# 检查内容:
#   1. 今日信号回顾（是否触发）
#   2. 挂单有效性（价格是否跑太远）
#   3. 深夜重大事件预警
#   4. 周五: 周末跳空风险检查
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

mkdir -p output/signals output/logs

LOG="output/logs/evening_$(date +%Y%m%d).log"
WEEKDAY=$(date +%u)  # 1=周一, 5=周五, 6=周六, 7=周日

echo "========================================" | tee "$LOG"
echo "🌙 EUR/USD 晚间简报" | tee -a "$LOG"
echo "    $(date '+%Y-%m-%d %H:%M') GMT+8" | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"

# 1. 今日信号回顾
echo "" | tee -a "$LOG"
echo "─── 今日信号回顾 ─────────────────" | tee -a "$LOG"

LATEST_SIGNAL=$(ls -t output/signals/*.md 2>/dev/null | head -1)
if [ -n "$LATEST_SIGNAL" ]; then
    echo "   最新信号: $(basename "$LATEST_SIGNAL")" | tee -a "$LOG"
    # 提取方向
    DIRECTION=$(grep -oE '(BUY|SELL|做多|做空|观望)' "$LATEST_SIGNAL" | head -1)
    echo "   方向: $DIRECTION" | tee -a "$LOG"
else
    echo "   今日无信号" | tee -a "$LOG"
fi

# 2. 当前价格
echo "" | tee -a "$LOG"
echo "─── 当前价格 ────────────────────" | tee -a "$LOG"
python3 -c "
from src.data_collection.oanda import OandaClient
c = OandaClient()
try:
    d = c.fetch_candles('H1', 1)
    if d:
        print(f'   EUR/USD 当前: {d[-1][\"close\"]:.5f}')
        print(f'   时间: {d[-1][\"time\"][:19]} UTC')
except Exception as e:
    print(f'   获取失败: {e}')
" 2>&1 | tee -a "$LOG"

# 3. 深夜事件预警（22:00 之后到次日 08:00 之间的事件）
echo "" | tee -a "$LOG"
echo "─── 深夜/明晨事件预警 ────────────" | tee -a "$LOG"
python3 -c "
from src.data_collection.calendar import EconomicCalendar
cal = EconomicCalendar()
ev = cal.fetch()
eur = cal.get_eur_usd_high_impact(ev)
high = [e for e in eur if e.get('impact','').lower() == 'high']
if high:
    print(f'   高影响事件 ({len(high)} 个):')
    for e in high:
        print(f'     ⚠️ {e[\"date\"]} {e[\"currency\"]} {e[\"event\"][:40]}')
else:
    medium = [e for e in eur if e.get('impact','').lower() == 'medium']
    if medium:
        print(f'   中影响事件 ({len(medium)} 个):')
        for e in medium[:3]:
            print(f'     🟡 {e[\"date\"]} {e[\"currency\"]} {e[\"event\"][:40]}')
    else:
        print('   今夜无重大事件')
" 2>&1 | tee -a "$LOG"

# 4. 大势检查
echo "" | tee -a "$LOG"
echo "─── 大势检查 ────────────────────" | tee -a "$LOG"
python3 -c "
from src.strategy.trend import TrendAnalyzer
t = TrendAnalyzer()
print(t.format_one_liner())
" 2>&1 | tee -a "$LOG"

# 5. 周五：周末跳空风险
echo "" | tee -a "$LOG"
if [ "$WEEKDAY" = "5" ]; then
    echo "⚠️  周五提醒: 检查明后天有无重大数据" | tee -a "$LOG"
    python3 -c "
from src.data_collection.calendar import EconomicCalendar
cal = EconomicCalendar()
ev = cal.fetch()
# 检查周六/周日/周一的事件
import re
weekend = [e for e in ev if e.get('currency','') in ('EUR','USD') and e.get('impact','').lower() in ('high','medium')]
if weekend:
    print(f'   周末前后有 {len(weekend)} 个事件:')
    for e in weekend:
        print(f'     {e[\"date\"]} {e[\"event\"][:40]}')
    print('   ⚠️ 建议: 如有挂单未触发，考虑撤单防跳空')
else:
    print('   ✅ 周末无重大事件，可正常持仓')
" 2>&1 | tee -a "$LOG"
else
    echo "   周中正常持仓" | tee -a "$LOG"
fi

echo "" | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"

# 推送到飞书
python3 -c "
import subprocess
with open('$LOG') as f:
    c = f.read()
subprocess.run(['bash', 'scripts/feishu_push.sh', c[:1500]])
" 2>/dev/null
