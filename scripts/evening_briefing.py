"""晚间简报 — crontab 入口（替换 cron_evening.sh）"""
import subprocess
from datetime import datetime
from pathlib import Path

from src.strategy.signal import SignalGenerator
from src.strategy.trend import TrendAnalyzer
from src.data_collection.calendar import EconomicCalendar
from src.data_collection.oanda import OandaClient

now = datetime.now()
weekday = now.isoweekday()  # 1=周一, 5=周五, 6=周六

print("=" * 10 + " 晚间简报 " + "=" * 10)
print(f"⏰ {now.strftime('%Y-%m-%d %H:%M')} GMT+8")

# 1. 今日信号回顾
print()
print("📊 今日信号")
gen = SignalGenerator()
sig = gen.generate()
output = gen.format_markdown(sig).replace("📊 EUR/USD", "  ")
for line in output.split("\n"):
    print(f"  {line.strip()}")

# 2. 当前价格
print()
print("💰 当前价格")
try:
    c = OandaClient()
    d = c.fetch_candles("H1", 1)
    if d:
        print(f"  EUR/USD: {d[-1]['close']:.5f}")
except Exception:
    print("  获取失败")

# 3. 今夜/明晨事件预警
print()
print("📅 事件预警")
cal = EconomicCalendar()
ev = cal.fetch()
eur = cal.get_eur_usd_high_impact(ev)
high = [e for e in eur if e.get("impact", "").lower() == "high"]
if high:
    print(f"  ⚠️ 高影响 ({len(high)}):")
    for e in high[:5]:
        print(f"    {e['date']} {e['currency']} {e['event'][:40]}")
else:
    print("  今夜无重大事件")

# 4. 大势
print()
t = TrendAnalyzer()
print(t.format_one_liner())

# 5. 周五跳空
if weekday == 5:
    print()
    print("⚠️ 周五检查")
    weekend = [
        e for e in eur
        if e.get("impact", "").lower() in ("high", "medium")
    ]
    if weekend:
        print(f"  周末前后有 {len(weekend)} 个事件:")
        for e in weekend[:5]:
            print(f"    {e['date']} {e['event'][:40]}")
        print("  ⚠️ 如有挂单未触发，考虑撤单防跳空")
    else:
        print("  ✅ 周末无重大事件，可正常持仓")

# 推送飞书
lines = []
out_dir = Path("output") / "signals"
files = sorted(out_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
if files:
    with open(files[0]) as f:
        msg = (f.read()[:1000] + "\n\n🔍 详情见中控面板 http://127.0.0.1:5001")
    subprocess.run(["bash", "scripts/feishu_push.sh", msg])
