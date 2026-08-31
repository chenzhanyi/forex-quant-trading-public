"""信号生成器 — 整合大势 + 入场 + 风控 → 每日交易信号"""
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta

# 上海时间
SH_TZ = timezone(timedelta(hours=8))
from pathlib import Path
from typing import Optional, List

from src.strategy.trend import TrendAnalyzer
from src.strategy.entry import EntryDetector, EntrySignal, BUY, SELL, NONE
from src.strategy.risk import RiskManager, RiskResult
from src.utils.config_loader import config

logger = logging.getLogger(__name__)


@dataclass
class FinalSignal:
    """最终交易信号"""
    timestamp: str
    can_trade: bool
    skip_reason: str
    trend_overall: str
    trend_d1: str
    trend_h4: str
    direction: str
    entry_price: float
    entry_reason: str
    signal_type: str
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    rr_ratio: float
    sl_method: str
    position_units: int
    position_lots: float
    max_loss_amount: float
    max_loss_pips: float
    confidence: int
    holding_advice: str
    sentiment_label: str = "中性"
    sentiment_score: float = 0.0
    sentiment_source: str = "none"
    brief_context: str = ""  # 日历+基本面一句话简报
    symbol: str = "EUR/USD"  # 品种(复盘报告/日志标注用)


class SignalGenerator:
    """信号生成器 — 5 层决策流水线（含基本面情绪共振）"""

    def __init__(self):
        self.trend = TrendAnalyzer()
        self.entry = EntryDetector()
        self.risk = RiskManager()
        self.cfg = config.load()

    def _load_sentiment(self) -> dict:
        """加载今日基本面情绪（LLM 优先，关键词降级）

        Returns:
            {"label": str, "score": float, "source": str, "available": bool}
        """
        # 方案A: 今日 LLM 分析结果
        today = datetime.now().strftime("%Y%m%d")
        llm_path = Path(__file__).resolve().parent.parent.parent / "output" / "analysis" / f"sentiment_{today}.json"
        if llm_path.exists():
            try:
                import json
                data = json.loads(llm_path.read_text(encoding="utf-8"))
                label = str(data.get("label", "中性"))
                score = float(data.get("score", 0))
                source = data.get("source", "LLM")
                return {"label": label, "score": score, "source": source, "available": True}
            except Exception:
                pass

        # 方案B: 关键词打分降级
        try:
            from src.analysis.fundamentals import FundamentalsAnalyzer
            fa = FundamentalsAnalyzer()
            fund_dir = Path(__file__).resolve().parent.parent.parent / "data" / "fundamentals"
            events = []
            for f in sorted(fund_dir.rglob("*.json"), reverse=True)[:10]:
                import json
                with open(f) as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        events.extend(data)
            if events:
                result = fa.analyze_sentiment(events, 7)
                return {**result, "source": "keywords", "available": True}
        except Exception:
            pass

        return {"label": "中性", "score": 0, "source": "none", "available": False}

    def _check_flat_dup(self, direction: str, entry_price: float) -> str:
        """横盘期重复价位过滤 — 返回跳过原因, 空串=放行

        条件(同时满足才过滤):
          1. 近12根H4区间 < flat_range pips (横盘)
          2. 48h内最近的同向可交易信号入场价距当前 < dedup_pips pips

        趋势期(区间大)不拦 — 允许顺势加仓, 由48h限3单机制兜底。
        """
        entry_cfg = self.cfg.get("strategy", {}).get("entry", {})
        dedup_pips = float(entry_cfg.get("dedup_pips", 15))
        if dedup_pips <= 0:
            return ""
        flat_range = float(entry_cfg.get("flat_range", 40))

        # ① 横盘判定
        rng = None
        try:
            from src.data_collection.oanda import OandaClient
            h4 = OandaClient().load_parquet("H4")
            if h4.empty or len(h4) < 12:
                return ""
            w12 = h4.iloc[-12:]
            rng = (w12['high'].max() - w12['low'].min()) * 10000
            if rng >= flat_range:
                return ""  # 非横盘
        except Exception:
            return ""

        # ② 48h内最近的同向信号入场价
        last_price = None
        try:
            import json
            hist_path = Path(__file__).resolve().parent.parent.parent / "data" / "signal_history.jsonl"
            if not hist_path.exists():
                return ""
            cutoff = datetime.now(timezone.utc).timestamp() - 48 * 3600
            with open(hist_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        rec_ts = datetime.fromisoformat(rec.get("recorded_at", "")).timestamp()
                    except Exception:
                        continue
                    if rec_ts < cutoff or not rec.get("can_trade"):
                        continue
                    d = str(rec.get("direction", ""))
                    same_dir = (direction == BUY and "做多" in d) or \
                               (direction == SELL and "做空" in d)
                    if same_dir and rec.get("entry_price"):
                        last_price = float(rec["entry_price"])
        except Exception:
            return ""

        if last_price is None:
            return ""
        dist = abs(entry_price - last_price) * 10000
        if dist < dedup_pips:
            return (
                f"⛔ 横盘期重复信号(近12根H4区间{rng:.0f}pips<{flat_range:.0f}pips, "
                f"距48h内同向信号入场仅{dist:.0f}pips<{dedup_pips:.0f}pips), 不重复开仓"
            )
        return ""

    @staticmethod
    def _sentiment_direction(sentiment: dict) -> str:
        """将情绪分数映射为方向: bullish / bearish / neutral"""
        score = sentiment.get("score", 0)
        if score > 0.15:
            return "bullish"
        elif score < -0.15:
            return "bearish"
        return "neutral"

    @staticmethod
    def _trend_direction(trend_overall: str) -> str:
        """将趋势标签映射为方向"""
        if trend_overall in ("BULLISH", "BULLISH_CAUTIOUS"):
            return "bullish"
        elif trend_overall in ("BEARISH", "BEARISH_CAUTIOUS"):
            return "bearish"
        return "neutral"

    def generate(self) -> FinalSignal:
        """生成完整交易信号"""
        # 0️⃣ 提前加载基本面情绪 + 日历（即使观望也展示简报）
        sentiment = self._load_sentiment()
        cal_cfg = self.cfg["strategy"].get("calendar", {})
        cal_warning = False
        cal_events: list = []
        if cal_cfg.get("major_event_warning", True):
            try:
                from src.data_collection.calendar import EconomicCalendar
                cal = EconomicCalendar()
                major = cal.has_major_event_soon(
                    hours_ahead=cal_cfg.get("warning_hours", 24)
                )
                if major["has_event"]:
                    cal_warning = True
                    cal_events = major.get("events", [])
            except Exception as e:
                logger.debug(f"日历检查失败: {e}")
        brief = self._brief_context(sentiment, cal_events)

        # 0.5️⃣ 行情新鲜度检查 — 数据过期禁止出信号
        # 防 API 故障后用过时价格计算 SL/TP 并下单；周末/收盘后无新K线自然拦截
        try:
            from src.data_collection.oanda import OandaClient
            oc = OandaClient()
            m15_fresh = oc.load_parquet("M15")
            if not m15_fresh.empty:
                last_ts = m15_fresh.index.max()
                if last_ts.tzinfo is None:
                    last_ts = last_ts.tz_localize("UTC")
                age_min = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
                if age_min > 60:
                    return self._skip(
                        f"⛔ 行情数据过期（最后M15 K线 {age_min:.0f} 分钟前），暂停出信号",
                        {"reason": "行情数据过期", "D1": {}, "H4": {}},
                        sentiment, brief,
                    )
        except Exception as e:
            logger.debug(f"行情新鲜度检查跳过: {e}")

        # 1️⃣ 趋势信息（仅用于展示，不拦截。entry.py 自己做趋势过滤）
        try:
            trend = self.trend.analyze()
        except Exception:
            trend = {"reason": "分析中", "D1": {"direction": "?"}, "H4": {"direction": "?"}, "overall": "?", "can_trade": True}

        # 1.5️⃣ 周五整日禁（可配 friday_no_trade=true 时）→ 否则留到数据黑洞窗口拦
        now = datetime.now(SH_TZ)
        if cal_cfg.get("friday_no_trade", True) and now.weekday() == 4:
            return self._skip("周五不交易（用户规则）", trend, sentiment, brief)

        # 1.5.1️⃣ 重大数据黑洞窗口（非农等数据时段禁开仓，共 EURUSD/黄金）
        try:
            from src.utils.trade_blackout import is_blacked_out
            blacked, reason = is_blacked_out(now)
            if blacked:
                return self._skip(reason, trend, sentiment, brief)
        except Exception as e:
            logger.debug(f"数据黑洞检查失败: {e}")

        # 1.6️⃣ 重大事件前静默期：FOMC/非农/CPI 前4h~后1h 禁止开仓
        if cal_warning:
            # 检查是否在静默窗口内（事件前4h ~ 后1h）
            try:
                from src.data_collection.calendar import EconomicCalendar
                cal = EconomicCalendar()
                urgent = cal.has_major_event_soon(hours_ahead=4)  # 4h 内
                if urgent["has_event"]:
                    return self._skip(
                        f"⛔ 重大事件临近（4h内），暂停开仓: {urgent['events'][:2]}",
                        trend, sentiment, brief,
                    )
            except Exception:
                pass  # 无法获取详细时间，仍按 cal_warning 减分处理

        # 2️⃣ 入场信号（6 条件叠加法）
        entry = self.entry.detect()
        if entry is None:
            return self._skip(
                "6条件未同时满足 (H4+SMA200+EMA+K线形态+RSI(可配)+欧美盘)，继续观察",
                trend, sentiment, brief,
            )

        # 2.1️⃣ 横盘联动重复价位过滤: 近12根H4区间<flat_range(横盘) 且
        # 距48h内最近同向信号入场价<dedup_pips → 不重复开仓
        # (2026-08实盘教训: 横盘期一天6单同价位扎堆, 暴跌时全部扫止损;
        #  回测90天: 44信号+383p → 29信号+330p, 胜率63%→68%)
        dup_reason = self._check_flat_dup(entry.direction, entry.entry_price)
        if dup_reason:
            return self._skip(dup_reason, trend, sentiment, brief)

        # 2.5️⃣ 获取 H4 止盈目标
        h4_targets = self.entry.get_h4_targets(entry.direction)

        # 3️⃣ 风控检查（止损=波段极值点, 止盈=H4目标）
        risk = self.risk.calculate(
            entry.direction, entry.entry_price,
            entry.stop_loss, h4_targets,
        )
        if not risk.passed:
            return self._skip(risk.reason, trend, sentiment, brief)

        # 4️⃣ 综合置信度
        # 基础分: EntryDetector 评分
        conf = entry.confidence + 1  # +1 归一化到 3~6 区间
        # 趋势一致性加分
        if trend["overall"] in ("BULLISH", "BEARISH"):
            conf += 1  # D1+H4 一致看多/看空 → +1
        # 波段止损合理加分（止损不太宽）
        sl_pips = abs(entry.stop_loss - entry.entry_price) * 10000
        if sl_pips <= 20:
            conf += 1  # 止损紧凑 → +1
        # 重大事件预警减分
        if cal_warning:
            conf -= 1  # 重大事件临近（利率决议/FOMC/非农）→ -1

        # 时段权重: 欧美重叠 (19:00-23:00 CST) 流动性最优 → +1
        #           亚洲盘 (08:00-14:00 CST) 无量横盘假突破 → -1
        now_cst = datetime.now(SH_TZ)
        cst_hour = now_cst.hour
        if 19 <= cst_hour <= 23:
            conf += 1  # 欧美盘重叠，真实动量
            sent_note_extra = " | 🌍 欧美重叠时段"
        elif 8 <= cst_hour <= 14:
            conf -= 1  # 亚洲盘，假信号高发
            sent_note_extra = " | 🌏 亚洲盘（注意假突破风险）"
        else:
            sent_note_extra = ""

        # 5️⃣ 基本面情绪共振（LLM 优先，关键词降级）
        sent_dir = self._sentiment_direction(sentiment)
        trend_dir = self._trend_direction(trend["overall"])
        sent_note = ""
        if sent_dir != "neutral" and trend_dir != "neutral":
            if sent_dir == trend_dir:
                conf += 1  # 基本面与技术面共振 → +1
                sent_note = f" | 📰 基本面共振 ({sentiment['label']} score={sentiment['score']:.2f})"
            else:
                conf -= 1  # 基本面与技术面背离 → -1
                sent_note = f" | ⚠️ 基本面背离 ({sentiment['label']} score={sentiment['score']:.2f})"
        elif sent_dir != "neutral":
            sent_note = f" | 📰 基本面{sentiment['label']} (score={sentiment['score']:.2f})"
        # 中性或无数据 → 不影响置信度

        conf = max(1, min(conf, 5))  # 下限 1 星（让你知道有信号但需谨慎）

        # 5️⃣ 持仓建议（含出场规则）
        dir_label = "做多" if entry.direction == BUY else "做空"
        tp1_pips = abs(risk.take_profit_1 - entry.entry_price) * 10000
        sl_pips = risk.max_loss_pips
        # 保本价: 1:1 R:R 时移动止损到入场价
        breakeven_pips = sl_pips  # 1:1 R:R 即盈亏平衡点
        breakeven_price = entry.entry_price
        holding = (
            f"🎯 入场: {entry.entry_price:.5f}\n"
            f"🛑 止损: {risk.stop_loss:.5f} (-{sl_pips:.0f}pips, H4 ATR×2.5)\n"
            f"🏁 止盈: {risk.take_profit_1:.5f} (+40pips)\n"
            f"📐 R:R: {risk.rr_ratio:.1f} | 仓位: {risk.position_units}单位"
        )

        return FinalSignal(
            timestamp=datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M GMT+8"),
            can_trade=True,
            skip_reason="",
            trend_overall=trend["reason"],
            trend_d1=trend["D1"].get("direction", "?"),
            trend_h4=trend["H4"].get("direction", "?"),
            direction=f"{'🟢' if entry.direction == BUY else '🔴'} {dir_label}",
            entry_price=entry.entry_price,
            entry_reason=(
                entry.reason
                + (" | ⚠️ 重大事件预警" if cal_warning else "")
                + sent_note
                + sent_note_extra
            ),
            signal_type=entry.signal_type,
            stop_loss=risk.stop_loss,
            take_profit_1=risk.take_profit_1,
            take_profit_2=risk.take_profit_2,
            rr_ratio=risk.rr_ratio,
            sl_method=risk.sl_method,
            position_units=risk.position_units,
            position_lots=risk.position_lots,
            max_loss_amount=risk.max_loss_amount,
            max_loss_pips=risk.max_loss_pips,
            confidence=conf,
            holding_advice=holding,
            sentiment_label=sentiment["label"],
            sentiment_score=sentiment["score"],
            sentiment_source=sentiment["source"],
            brief_context=brief,
        )

    def _brief_context(self, sentiment: dict, cal_events: list) -> str:
        """生成日历 + 基本面一句话简报，用于早间/晚间推送"""
        lines = []

        # 📅 日历预警
        if cal_events:
            sample = cal_events[0]
            evt_name = sample if isinstance(sample, str) else sample.get("event", str(sample))
            lines.append(f"📅 日历预警: 未来24h有 {len(cal_events)} 个重大事件（{evt_name[:30]}...），谨慎入场")
        else:
            lines.append("📅 日历: 未来24h无重大事件，可正常交易")

        # 📰 基本面
        if sentiment.get("available"):
            label = sentiment["label"]
            score = sentiment["score"]
            # 尝试从 LLM 结果中截取简短理由
            reason = ""
            llm_path = Path(__file__).resolve().parent.parent.parent / "output" / "analysis" / f"sentiment_{datetime.now().strftime('%Y%m%d')}.json"
            if llm_path.exists():
                try:
                    import json
                    data = json.loads(llm_path.read_text(encoding="utf-8"))
                    reason = data.get("reason", "")
                except Exception:
                    pass
            if reason and len(reason) > 80:
                reason = reason[:80] + "..."
            prefix = "📰" if abs(score) > 0.3 else "📰"
            if reason:
                lines.append(f"{prefix} 基本面{label}(score={score:.2f}): {reason}")
            else:
                lines.append(f"{prefix} 基本面{label}(score={score:.2f})")
        else:
            lines.append("📰 基本面: 今日暂无分析数据")

        return "\n".join(lines)

    def format_markdown(self, signal: FinalSignal) -> str:
        """格式化为飞书友好的消息格式"""
        if not signal.can_trade:
            return self._fmt_skip(signal)

        stars = "★" * signal.confidence + "☆" * (5 - signal.confidence)
        sl_pips = signal.max_loss_pips
        tp1_pips = abs(signal.take_profit_1 - signal.entry_price) * 10000
        tp2_pips = abs(signal.take_profit_2 - signal.entry_price) * 10000
        brief_section = f"\n{signal.brief_context}\n" if signal.brief_context else ""

        return (
            "📊 EUR/USD 建仓建议\n"
            f"⏰ {signal.timestamp}\n"
            f"📈 {signal.trend_overall}\n"
            f"{brief_section}\n"
            f"💡 {signal.direction}\n"
            f"📌 入场: {signal.entry_price:.5f}\n"
            f"🎯 TP1: {signal.take_profit_1:.5f} (+{tp1_pips:.0f} pips) [50%]\n"
            f"🎯 TP2: {signal.take_profit_2:.5f} (+{tp2_pips:.0f} pips) [50%]\n"
            f"🛑 止损: {signal.stop_loss:.5f} (-{sl_pips:.0f} pips)\n"
            f"📐 方法: {signal.sl_method} | R:R {signal.rr_ratio:.1f}\n"
            f"\n"
            f"💰 {signal.position_units} 单位 ({signal.position_lots:.4f} 手)\n"
            f"⚠️ 最大亏损: ${signal.max_loss_amount:.2f}\n"
            f"\n"
            f"📋 {signal.entry_reason}\n"
            f"📏 出场规则:\n{signal.holding_advice}\n"
            f"\n"
            f"🏷 可信度: {stars} ({signal.confidence}/5)\n"
        )

    def _fmt_skip(self, signal: FinalSignal) -> str:
        brief_section = f"\n{signal.brief_context}\n" if signal.brief_context else ""
        return (
            "📊 EUR/USD 每日观察\n"
            f"⏰ {signal.timestamp}\n"
            f"📈 {signal.trend_overall}\n"
            f"{brief_section}\n"
            f"💡 建议: 观望\n"
            f"📋 {signal.skip_reason}\n"
        )

    def _skip(self, reason: str, trend: dict, sentiment: dict = None, brief: str = "") -> FinalSignal:
        if sentiment is None:
            sentiment = {"label": "中性", "score": 0, "source": "none"}
        # 将基本面情绪附到跳过原因中
        if sentiment.get("available") and sentiment.get("label") != "中性":
            reason += f" | 📰 基本面: {sentiment['label']} (score={sentiment['score']:.2f})"
        return FinalSignal(
            timestamp=datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M GMT+8"),
            can_trade=False,
            skip_reason=reason,
            trend_overall=trend.get("reason", "分析中"),
            trend_d1=trend.get("D1", {}).get("direction", "?"),
            trend_h4=trend.get("H4", {}).get("direction", "?"),
            direction="观望", entry_price=0, entry_reason="",
            signal_type="", stop_loss=0, take_profit_1=0, take_profit_2=0,
            rr_ratio=0, sl_method="",
            position_units=0, position_lots=0,
            max_loss_amount=0, max_loss_pips=0,
            confidence=0, holding_advice="",
            sentiment_label=sentiment.get("label", "中性"),
            sentiment_score=sentiment.get("score", 0.0),
            sentiment_source=sentiment.get("source", "none"),
            brief_context=brief,
        )

    @staticmethod
    def _trend_label(overall: str) -> str:
        if "BULLISH" in overall:
            return "看多"
        if "BEARISH" in overall:
            return "看空"
        return "中性"

    def save(self, signal: FinalSignal) -> Path:
        """保存信号到文件（Markdown 给人看 + JSON 给程序读）"""
        import json
        out_dir = Path(self.cfg["output"]["signals_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(SH_TZ).strftime("%Y%m%d_%H%M%S")
        mode = "signal" if signal.can_trade else "observe"
        base = out_dir / f"{ts}_{mode}"

        # Markdown（人读）
        md_path = base.with_suffix(".md")
        md_path.write_text(self.format_markdown(signal), encoding="utf-8")

        # JSON（程序读 — 结构化 FinalSignal 数据）
        json_path = base.with_suffix(".json")
        sig_dict = asdict(signal)
        json_path.write_text(
            json.dumps(sig_dict, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return md_path
