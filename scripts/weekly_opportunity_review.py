#!/usr/bin/env python3
"""错失机会闭环分析 — 每周运行，从 missed_*.json 提取规律，输出优化建议

用法:
    python scripts/weekly_opportunity_review.py [--weeks 4]
    python scripts/weekly_opportunity_review.py --json  # 输出 JSON 格式
"""

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

PROJ = Path(__file__).resolve().parent.parent


def load_opportunities(weeks: int = 4) -> List[Dict]:
    """加载最近 N 周的错失机会"""
    opp_dir = PROJ / "data" / "opportunities"
    if not opp_dir.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    opportunities = []
    for json_path in sorted(opp_dir.rglob("missed_*.json")):
        # 按文件日期过滤
        try:
            date_str = json_path.stem.replace("missed_", "")
            file_date = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
            if file_date < cutoff:
                continue
        except ValueError:
            pass
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                opportunities.extend(data)
        except Exception as e:
            logger.warning(f"读取 {json_path} 失败: {e}")
    return opportunities


def analyze(opportunities: List[Dict]) -> Dict:
    """分析错失机会的特征规律"""
    if not opportunities:
        return {
            "total": 0, "weeks": "?", "trend_distribution": {},
            "move_direction": {"up": 0, "down": 0},
            "pips_distribution": {"45-60": 0, "60-80": 0, "80-100": 0, "100+": 0},
            "skip_reasons": {}, "ema_gap": {"near_cross": 0, "far_from_cross": 0},
            "session_distribution": {"asia_08_14": 0, "eu_15_18": 0, "us_overlap_19_23": 0, "other": 0},
        }

    total = len(opportunities)

    # ── 1. 趋势分布 ──
    trend_counter = Counter()
    for op in opportunities:
        d1 = op.get("signal_d1", "?")
        h4 = op.get("signal_h4", "?")
        trend_counter[f"D1={d1}/H4={h4}"] += 1

    # ── 2. 波动方向 ──
    up = sum(1 for o in opportunities if o.get("move_direction") == "up")
    down = sum(1 for o in opportunities if o.get("move_direction") == "down")

    # ── 3. 波动幅度分布 ──
    pips_ranges = {"45-60": 0, "60-80": 0, "80-100": 0, "100+": 0}
    for op in opportunities:
        pips = op.get("max_move_pips", 0)
        if pips < 60:
            pips_ranges["45-60"] += 1
        elif pips < 80:
            pips_ranges["60-80"] += 1
        elif pips < 100:
            pips_ranges["80-100"] += 1
        else:
            pips_ranges["100+"] += 1

    # ── 4. 跳过原因分析（决策树核心） ──
    skip_patterns = Counter()
    for op in opportunities:
        reason = op.get("skip_reason", "")
        # 提取关键模式
        if "5EMA/15EMA" in reason or "EMA" in reason:
            skip_patterns["EMA 未交叉"] += 1
        elif "ATR" in reason.lower():
            skip_patterns["ATR 波动率过低"] += 1
        elif "SMA" in reason or "追" in reason:
            skip_patterns["距 SMA 太远（追高/低保护）"] += 1
        elif "反转" in reason or "形态" in reason:
            skip_patterns["反转形态评分不足"] += 1
        elif "周五" in reason:
            skip_patterns["周五规则"] += 1
        elif "事件" in reason or "静默" in reason:
            skip_patterns["重大事件静默期"] += 1
        else:
            skip_patterns["其他/趋势分歧"] += 1

    # ── 5. 特征关联：EMA 接近交叉 vs 远离交叉 ──
    near_cross = 0  # EMA5 和 EMA15 差距 < 0.0005（即将交叉）
    far_from_cross = 0
    for op in opportunities:
        feats = op.get("features", {})
        ema5 = feats.get("h1_ema5", 0)
        ema15 = feats.get("h1_ema15", 0)
        if ema5 and ema15:
            gap = abs(ema5 - ema15)
            if gap < 0.0005:
                near_cross += 1
            else:
                far_from_cross += 1

    # ── 6. RSI 检查 ──
    rsi_filtered = 0
    for op in opportunities:
        feats = op.get("features", {})
        # 这些观望信号的 RSI 是否在超买/超卖区（说明 RSI 过滤有效）
        pass  # RSI 是新增的，历史数据中无此字段，后续积累

    # ── 7. 时段分布 ──
    hour_counter = Counter()
    for op in opportunities:
        sig_time = op.get("signal_time", "")
        try:
            dt = datetime.fromisoformat(sig_time)
            # 转北京时间
            cst_hour = (dt.hour + 8) % 24
            hour_counter[cst_hour] += 1
        except Exception:
            pass

    asian = sum(c for h, c in hour_counter.items() if 8 <= h <= 14)
    eu = sum(c for h, c in hour_counter.items() if 15 <= h <= 18)
    us_overlap = sum(c for h, c in hour_counter.items() if 19 <= h <= 23)
    other = total - asian - eu - us_overlap

    return {
        "total": total,
        "weeks": "?",
        "trend_distribution": dict(trend_counter.most_common()),
        "move_direction": {"up": up, "down": down},
        "pips_distribution": pips_ranges,
        "skip_reasons": dict(skip_patterns.most_common()),
        "ema_gap": {"near_cross": near_cross, "far_from_cross": far_from_cross},
        "session_distribution": {
            "asia_08_14": asian,
            "eu_15_18": eu,
            "us_overlap_19_23": us_overlap,
            "other": other,
        },
    }


def generate_recommendations(stats: Dict) -> List[str]:
    """根据统计数据生成优化建议"""
    recs = []
    total = stats.get("total", 0)
    if total == 0:
        return ["📭 暂无错失机会数据，系统刚开始追踪，积累中..."]
    if total < 5:
        recs.append("⚠️ 样本量不足（<5），建议继续积累数据")
        return recs

    # 1. 最大跳过原因
    skip = stats.get("skip_reasons", {})
    top_skip = max(skip, key=skip.get) if skip else ""
    top_pct = f"{skip[top_skip]/total*100:.0f}%" if top_skip else "N/A"

    if "EMA" in top_skip:
        recs.append(
            f"🔴 EMA 未交叉是最大的错失原因 ({top_pct})。"
            f"建议: 降低 min_pattern_score（当前 1.5→1.2）或增加反转形态的权重"
        )
    elif "SMA" in top_skip:
        recs.append(
            f"🟡 追高/低保护过滤了 {top_pct} 的潜在盈利。"
            f"建议: 增大 config 中 max_distance_from_h4_sma（当前 1.5→2.0）"
        )
    elif "ATR" in top_skip:
        recs.append(
            f"🟡 ATR 波动率过滤了 {top_pct} 的潜在盈利。"
            f"建议: 降低 min_atr_ratio（当前 0.8→0.6）"
        )
    elif "事件" in top_skip or "静默" in top_skip:
        recs.append(
            f"🟢 重大事件静默期阻止了 {top_pct} 信号，这些是正确的防御性过滤，无需调整"
        )

    # 2. EMA 接近交叉
    ema = stats.get("ema_gap", {})
    if ema.get("near_cross", 0) > total * 0.5:
        recs.append(
            f"💡 {ema['near_cross']}/{total} 的错失信号中 EMA5/EMA15 已接近交叉。"
            f"建议: 引入 'EMA 间距 < 0.0003 时提前入场' 的逻辑"
        )

    # 3. 时段
    session = stats.get("session_distribution", {})
    if session.get("asia_08_14", 0) > total * 0.5:
        recs.append(
            f"🌏 超过一半的错失发生在亚洲盘，这些多为假突破，过滤是正确的"
        )
    if session.get("us_overlap_19_23", 0) > total * 0.3:
        recs.append(
            f"🌍 欧美重叠时段错失较多，建议在此时间段降低过滤门槛"
        )

    # 4. 波动方向 vs 趋势
    direction = stats.get("move_direction", {})
    trend = stats.get("trend_distribution", {})
    bearish_missed = sum(c for k, c in trend.items() if "DOWN" in k)
    bullish_missed = sum(c for k, c in trend.items() if "UP" in k)
    if direction.get("up", 0) > direction.get("down", 0) and bearish_missed > bullish_missed:
        recs.append(
            "⚠️ 趋势看空但错失上涨: 说明趋势判断与价格实际走势出现系统性偏差，"
            "检查 D1/H4 斜率判定阈值是否需要调整"
        )

    return recs


def print_report(stats: Dict, recs: List[str]) -> None:
    """打印可读报告"""
    print("=" * 55)
    print(f"📊 错失机会闭环分析 — {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 55)
    total = stats.get("total", 0)
    print(f"\n总样本: {total} 个错失信号\n")
    if total == 0:
        print("\n💡 优化建议:")
        for i, rec in enumerate(recs, 1):
            print(f"  {i}. {rec}")
        print("\n" + "=" * 55)
        return

    print("📈 趋势分布:")
    for trend, cnt in stats.get("trend_distribution", {}).items():
        bar = "█" * min(cnt, 20)
        print(f"  {trend}: {bar} ({cnt})")

    print(f"\n📉 波动方向: ↑{stats['move_direction']['up']} ↓{stats['move_direction']['down']}")

    print("\n📏 波动幅度:")
    for rng, cnt in stats.get("pips_distribution", {}).items():
        if cnt > 0:
            print(f"  {rng}pips: {cnt} 次")

    print("\n🔍 跳过原因（决策树根节点）:")
    for reason, cnt in stats.get("skip_reasons", {}).items():
        pct = f"{cnt/stats['total']*100:.0f}%"
        print(f"  {reason}: {cnt} ({pct})")

    print("\n⏰ 时段分布:")
    session = stats.get("session_distribution", {})
    print(f"  亚洲盘 08-14: {session['asia_08_14']}")
    print(f"  欧洲盘 15-18: {session['eu_15_18']}")
    print(f"  欧美重叠 19-23: {session['us_overlap_19_23']}")

    if stats.get("ema_gap", {}).get("near_cross", 0) > 0:
        print(f"\n📐 EMA 接近度: 近交叉 {stats['ema_gap']['near_cross']}, 远离 {stats['ema_gap']['far_from_cross']}")

    print("\n💡 优化建议:")
    for i, rec in enumerate(recs, 1):
        print(f"  {i}. {rec}")

    print("\n" + "=" * 55)


if __name__ == "__main__":
    import sys
    weeks = 4
    output_json = False
    for arg in sys.argv[1:]:
        if arg.startswith("--weeks="):
            weeks = int(arg.split("=")[1])
        elif arg == "--json":
            output_json = True

    opps = load_opportunities(weeks)
    stats = analyze(opps)
    if "weeks" in stats:
        stats["weeks"] = str(weeks)

    recs = generate_recommendations(stats)

    if output_json:
        print(json.dumps({"stats": stats, "recommendations": recs}, ensure_ascii=False, indent=2))
    else:
        print_report(stats, recs)
