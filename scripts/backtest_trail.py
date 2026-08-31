#!/usr/bin/env python3
"""锁利追踪 vs 固定止盈 回测对比

在四步入场法的历史信号上，对比两种出场策略:
  A) 固定止盈: 到达 TP1 就平仓
  B) 锁利追踪: 接近 TP1 时检查趋势 → 趋势好就锁利+新TP继续跑

用法:
    python scripts/backtest_trail.py --days 30
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from src.analysis.indicators import Indicators
from src.data_collection.oanda import OandaClient

ind = Indicators()
oanda = OandaClient()
SH_TZ = timezone(timedelta(hours=8))
TP_NEAR = 0.70  # 70% 后触发再评估
MIN_TRAIL_GAP = 0.00150  # 锁利 SL 距当前价 ≥ 15pips
# 追踪止损只用 H4 级别（M15 太窄，不适合中长线）


def load_data():
    m15 = oanda.load_parquet("M15")
    h4 = oanda.load_parquet("H4")
    d1 = oanda.load_parquet("D1")
    d1 = ind.add_sma(d1, 50)
    h4 = ind.add_sma(h4, 200)
    m15 = ind.add_ema(m15, 5)
    m15 = ind.add_ema(m15, 15)
    return m15, h4, d1


def get_trend(d1, h4, dt):
    """判断大势"""
    d1_before = d1[d1.index <= dt]
    h4_before = h4[h4.index <= dt]
    if len(d1_before) == 0 or len(h4_before) == 0:
        return "NEUTRAL"
    d1_dir = "UP" if float(d1_before.iloc[-1]["close"]) > float(d1_before.iloc[-1].get("SMA50", 0)) else "DOWN"
    h4_dir = "UP" if float(h4_before.iloc[-1]["close"]) > float(h4_before.iloc[-1].get("SMA200", 0)) else "DOWN"
    if len(h4_before) >= 3:
        slope = ind.calculate_slope(h4_before["SMA200"], 3)
        if abs(slope) < 0.0003:
            h4_dir = "SIDEWAYS"
    if d1_dir == "UP" and h4_dir in ("UP", "SIDEWAYS"):
        return "BULLISH"
    elif d1_dir == "DOWN" and h4_dir in ("DOWN", "SIDEWAYS"):
        return "BEARISH"
    return "NEUTRAL"


def get_h4_trail_sl(h4, dt, direction):
    """H4 级别锁利止损候选"""
    try:
        iloc = h4.index.get_loc(dt)
    except KeyError:
        return None
    window = h4.iloc[:iloc + 1]
    swings = ind.find_swing_points(window, window=5)
    if swings is None:
        return None
    sh, sl = swings
    fib = ind.calc_fib_retracement(sh, sl)
    if direction == "SELL":
        return round(max(sh + 0.00050, fib["0.382"]), 5)
    else:
        return round(min(sl - 0.00050, fib["0.618"]), 5)


def get_next_swing(m15, h4, dt, direction):
    """获取当前时间之后的 M15 波段点（模拟实时可用的数据）"""
    try:
        iloc = m15.index.get_loc(dt)
    except KeyError:
        return None, None
    window = m15.iloc[:iloc + 1]
    swings = ind.find_recent_swing_points_m15(window, window=5, lookback=36)
    if swings is None:
        return None, None
    if direction == "SELL":
        return swings["swing_high"]  # (idx, price)
    return swings["swing_low"]


def get_next_h4_target(h4, dt, direction, current_price):
    """获取下一个 H4 目标"""
    try:
        iloc = h4.index.get_loc(dt)
    except KeyError:
        return None
    window = h4.iloc[:iloc + 1]
    swings = ind.find_swing_points(window, window=5)
    if swings is None:
        return None
    sh, sl = swings
    if direction == "SELL":
        if current_price <= sl:
            ext = ind.calc_fib_extension(sh, sl, "SELL")
            return ext.get("1.618")
        return sl
    else:
        if current_price >= sh:
            ext = ind.calc_fib_extension(sh, sl, "BUY")
            return ext.get("1.618")
        return sh


def sim_fixed_exit(entry, sl, tp1, direction, m15, entry_dt):
    """固定止盈模拟"""
    try:
        start = m15.index.get_loc(entry_dt) + 1
    except KeyError:
        return None, 0
    after = m15.iloc[start:]
    for _, bar in after.iterrows():
        h, l = float(bar["high"]), float(bar["low"])
        if direction == "SELL":
            if h >= sl:
                return "SL", -(sl - entry) * 10000
            if l <= tp1:
                return "TP1", (entry - tp1) * 10000
        else:
            if l <= sl:
                return "SL", -(entry - sl) * 10000
            if h >= tp1:
                return "TP1", (tp1 - entry) * 10000
    return "OPEN", 0


def sim_trail_exit(entry, sl, tp1, direction, m15, h4, d1, entry_dt):
    """锁利追踪模拟"""
    try:
        start = m15.index.get_loc(entry_dt) + 1
    except KeyError:
        return None, 0, []

    after = m15.iloc[start:]
    current_sl = sl
    current_tp = tp1
    total_move = abs(tp1 - entry)
    trail_events = []
    last_event_bar = -1

    for i, (_, bar) in enumerate(after.iterrows()):
        h, l, c = float(bar["high"]), float(bar["low"]), float(bar["close"])
        bar_time = after.index[i]

        # 止损检查
        if direction == "SELL":
            if h >= current_sl:
                pips = (entry - current_sl) * 10000 if current_sl < entry else -(current_sl - entry) * 10000
                trail_events.append(f"SL@{bar_time}:{current_sl:.5f}")
                return "SL", pips, trail_events
        else:
            if l <= current_sl:
                pips = (current_sl - entry) * 10000 if current_sl > entry else -(entry - current_sl) * 10000
                trail_events.append(f"SL@{bar_time}:{current_sl:.5f}")
                return "SL", pips, trail_events

        # TP 检查
        if direction == "SELL":
            if l <= current_tp:
                trail_events.append(f"TP@{bar_time}:{current_tp:.5f}")
                return "TP", (entry - current_tp) * 10000, trail_events
        else:
            if h >= current_tp:
                trail_events.append(f"TP@{bar_time}:{current_tp:.5f}")
                return "TP", (current_tp - entry) * 10000, trail_events

        # ── TP 接近检测 ──
        if direction == "SELL":
            progress = (entry - c) / total_move if total_move > 0 else 0
        else:
            progress = (c - entry) / total_move if total_move > 0 else 0

        if progress >= TP_NEAR and i > last_event_bar + 12:  # 至少隔 12 根再评估
            trend = get_trend(d1, h4, bar_time)
            if direction == "SELL" and "BEARISH" in trend:
                # 锁利继续
                old_sl = current_sl
                old_tp = current_tp
                # 新SL: 只用 H4 级别（中长线不看 M15 微波动）
                sl_h4 = get_h4_trail_sl(h4, bar_time, "SELL")
                if sl_h4 and sl_h4 < entry and (sl_h4 - c) >= MIN_TRAIL_GAP:
                    if sl_h4 < current_sl or current_sl == sl:
                        current_sl = sl_h4
                new_tp = get_next_h4_target(h4, bar_time, "SELL", c)
                if new_tp and new_tp < current_tp:
                    current_tp = new_tp

                trail_events.append(
                    f"🔒{bar_time}:{progress*100:.0f}% SL{old_sl:.5f}→{current_sl:.5f} TP{old_tp:.5f}→{current_tp:.5f}"
                )
                last_event_bar = i

            elif direction == "BUY" and "BULLISH" in trend:
                old_sl = current_sl
                old_tp = current_tp
                sl_h4 = get_h4_trail_sl(h4, bar_time, "BUY")
                if sl_h4 and sl_h4 > entry and (c - sl_h4) >= MIN_TRAIL_GAP:
                    if sl_h4 > current_sl or current_sl == sl:
                        current_sl = sl_h4
                new_tp = get_next_h4_target(h4, bar_time, "BUY", c)
                if new_tp and new_tp > current_tp:
                    current_tp = new_tp

                trail_events.append(
                    f"🔒{bar_time}:{progress*100:.0f}% SL{old_sl:.5f}→{current_sl:.5f} TP{old_tp:.5f}→{current_tp:.5f}"
                )
                last_event_bar = i

    # 走完 → 按最新价结算
    last_c = float(after.iloc[-1]["close"])
    if direction == "SELL":
        final_pips = (entry - last_c) * 10000
    else:
        final_pips = (last_c - entry) * 10000
    return "OPEN", final_pips, trail_events


# ════════════════════════════════════
# 主流程
# ════════════════════════════════════

if __name__ == "__main__":
    days = 30
    for a in sys.argv[1:]:
        if a.startswith("--days="):
            days = int(a.split("=")[1])

    print("📊 加载数据...")
    m15, h4, d1 = load_data()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    m15 = m15[m15.index >= pd.Timestamp(cutoff)]
    print(f"   M15={len(m15)}根, H4={len(h4)}根, D1={len(d1)}根")

    # 用之前的回测信号（手动指定已验证的入场点）
    signals = [
        {"time": "2026-07-06 03:15:00+00:00", "dir": "SELL", "entry": 1.14330,
         "sl": 1.14678, "tp1": 1.13775, "note": "射击之星"},
        {"time": "2026-07-10 14:45:00+00:00", "dir": "SELL", "entry": 1.14204,
         "sl": 1.14450, "tp1": 1.13775, "note": "阴吞阳"},
        {"time": "2026-07-13 13:15:00+00:00", "dir": "SELL", "entry": 1.14267,
         "sl": 1.14678, "tp1": 1.13775, "note": "乌云盖顶"},
        {"time": "2026-07-16 03:45:00+00:00", "dir": "SELL", "entry": 1.14672,
         "sl": 1.14972, "tp1": 1.13775, "note": "看跌孕线"},
    ]

    print(f"\n{'='*65}")
    print(f"📊 锁利追踪 vs 固定止盈 回测")
    print(f"{'='*65}")

    fixed_total = 0
    trail_total = 0
    trail_improvements = 0

    for s in signals:
        entry_dt = pd.Timestamp(s["time"])

        # A) 固定止盈
        fixed_outcome, fixed_pips = sim_fixed_exit(
            s["entry"], s["sl"], s["tp1"], s["dir"], m15, entry_dt
        )
        fixed_total += fixed_pips

        # B) 锁利追踪
        trail_outcome, trail_pips, events = sim_trail_exit(
            s["entry"], s["sl"], s["tp1"], s["dir"], m15, h4, d1, entry_dt
        )
        trail_total += trail_pips

        if trail_pips > fixed_pips:
            trail_improvements += 1

        # 打印
        diff = trail_pips - fixed_pips
        emoji = "✅" if diff > 0 else ("➖" if diff == 0 else "❌")
        print(f"\n{s['note']} {s['dir']} @ {s['entry']:.5f}")
        print(f"  固定止盈: {fixed_outcome:4s} {fixed_pips:+.1f}pips")
        print(f"  锁利追踪: {trail_outcome:4s} {trail_pips:+.1f}pips  {emoji} {diff:+.1f}pips")
        for e in events:
            print(f"    {e}")

    print(f"\n{'='*65}")
    print(f"📊 汇总对比")
    print(f"{'='*65}")
    print(f"  固定止盈总盈利: {fixed_total:+.1f} pips")
    print(f"  锁利追踪总盈利: {trail_total:+.1f} pips")
    print(f"  差额:           {trail_total - fixed_total:+.1f} pips")
    print(f"  胜出信号:       {trail_improvements}/{len(signals)}")
    print(f"  结论:           {'锁利追踪更优 🏆' if trail_total > fixed_total else '固定止盈更稳 🛡'}")
