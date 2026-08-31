#!/bin/bash
# ============================================================
# 外汇量化交易 — 统一数据采集脚本（cron 调用）
# ============================================================
# 用法:
#   bash scripts/cron_collect.sh              # 采集所有
#   bash scripts/cron_collect.sh oanda        # 只采行情
#   bash scripts/cron_collect.sh calendar     # 只采日历
#   bash scripts/cron_collect.sh news         # 只采新闻
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

mkdir -p data/forex/{D1,H4,H1,M15,M5} data/news data/calendar output/logs

MODE="${1:-all}"
LOG="output/logs/collect_$(date +%Y%m%d_%H%M).log"

echo "========================================" | tee -a "$LOG"
echo "📊 数据采集 — $(date '+%Y-%m-%d %H:%M') GMT+8" | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"

collect_oanda() {
    echo "🔄 [行情] 采集 OANDA..." | tee -a "$LOG"
    python3 -c "
from src.data_collection.oanda import OandaClient
c = OandaClient()
for tf in ['D1','H4','H1','M15','M5']:
    try:
        data = c.fetch_candles(tf, 100)
        c.save_candles(data, tf)
        print(f'  {tf}: {len(data)} 条')
    except Exception as e:
        print(f'  {tf}: ❌ {e}')
" 2>&1 | tee -a "$LOG"
}

collect_calendar() {
    echo "🔄 [日历] 采集经济日历..." | tee -a "$LOG"
    python3 -c "
from src.data_collection.calendar import EconomicCalendar
cal = EconomicCalendar()
path = cal.collect_and_save()
events = cal.get_eur_usd_high_impact()
high = [e for e in events if e.get('impact','').lower()=='high']
print(f'  EUR/USD 高影响事件: {len(high)} 个')
for e in high[:5]:
    print(f'    {e[\"date\"]} {e[\"event\"]}')
" 2>&1 | tee -a "$LOG"
}

collect_news() {
    echo "🔄 [新闻] 采集 RSS 资讯..." | tee -a "$LOG"
    python3 -c "
from src.data_collection.news_collector import NewsCollector
nc = NewsCollector()
path = nc.collect_and_save()
print(f'  已保存: {path}')
" 2>&1 | tee -a "$LOG"
}

case "$MODE" in
    all)
        collect_oanda
        collect_calendar
        collect_news
        ;;
    oanda)    collect_oanda ;;
    calendar) collect_calendar ;;
    news)     collect_news ;;
    *)
        echo "未知模式: $MODE (可用: all, oanda, calendar, news)"
        exit 1
        ;;
esac

echo "========================================" | tee -a "$LOG"
echo "✅ 采集完成" | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"
