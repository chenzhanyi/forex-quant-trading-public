"""持仓追踪 + TP 接近时趋势再评估 + 锁利建议

用户实际出场策略（做空为例）:
  价格接近 TP1 → 检查 D1+H4 是否仍然看空
    ✅ 是 → 不急着止盈
           新 TP = 下一个 H4 低点 或 Fib 延伸
           新 SL = 当前价格上方的最近 M15/H4 波段高点（锁利）
           让仓位继续跑
    ❌ 否 → 趋势转弱，建议止盈

系统自动在 daemon 信号循环中检测并推送建议。
用户手动在 MT5 调整订单。
"""
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List

import pandas as pd

from src.analysis.indicators import Indicators
from src.data_collection.oanda import OandaClient
from src.strategy.trend import TrendAnalyzer
from src.utils.config_loader import config

logger = logging.getLogger(__name__)

PROJ = Path(__file__).resolve().parent.parent.parent
POSITIONS_FILE = PROJ / "data" / "active_positions.json"
SH_TZ = timezone(timedelta(hours=8))

# 触发再评估的 TP 接近阈值（价格距 TP 的百分比，达到即触发）
TP_NEAR_THRESHOLD = 0.30  # 30%：已走过 70% 的盈利空间


@dataclass
class ActivePosition:
    """活跃持仓"""
    signal_time: str           # 信号生成时间
    direction: str             # BUY / SELL
    entry_price: float
    stop_loss: float           # 当前止损
    take_profit_1: float       # TP1
    take_profit_2: float       # TP2
    sl_pips: float
    tp1_pips: float
    entry_reason: str          # 入场理由
    trend_at_entry: str        # 入场时大势
    created_at: str            # 记录创建时间
    adjustments: list = field(default_factory=list)  # 历史调整记录


class PositionManager:
    """持仓管理器 — 追踪活跃仓位 + 生成调整建议"""

    def __init__(self):
        self.ind = Indicators()
        self.oanda = OandaClient()
        self.trend = TrendAnalyzer()

    # ═══════════════════════════════════════════════
    # 持仓 CRUD
    # ═══════════════════════════════════════════════

    def open_position(self, signal) -> Optional[ActivePosition]:
        """记录新开仓（从 FinalSignal 创建）"""
        if not signal.can_trade:
            return None

        pos = ActivePosition(
            signal_time=signal.timestamp,
            direction="SELL" if "做空" in signal.direction else "BUY",
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            sl_pips=signal.max_loss_pips,
            tp1_pips=abs(signal.take_profit_1 - signal.entry_price) * 10000,
            entry_reason=signal.entry_reason,
            trend_at_entry=signal.trend_overall,
            created_at=datetime.now(SH_TZ).isoformat(),
        )

        positions = self.load_all()
        # 关闭同方向的旧仓位（同一方向只保留最新）
        positions = [p for p in positions if p.direction != pos.direction]
        positions.append(pos)
        self._save(positions)
        logger.info(f"📝 新开仓: {pos.direction} @ {pos.entry_price:.5f}")
        return pos

    def close_position(self, direction: str) -> bool:
        """平仓（按方向关闭）"""
        positions = self.load_all()
        new_list = [p for p in positions if p.direction != direction]
        if len(new_list) < len(positions):
            self._save(new_list)
            logger.info(f"📝 平仓: {direction}")
            return True
        return False

    def sync_actual_prices(self, mt4_positions: list) -> List[str]:
        """数据校准: 用 MT4 实际成交价/止损/止盈回写本地持仓记录

        mt4_positions: relay /positions 返回的列表(已按品种过滤)
        返回被回写的方向列表。
        """
        local = self.load_all()
        if not local or not mt4_positions:
            return []
        updated = []
        for p in local:
            actual = next(
                (m for m in mt4_positions
                 if m.get("type") == p.direction and m.get("open")),
                None,
            )
            if not actual:
                continue
            actual_open = float(actual.get("open") or 0)
            actual_sl = float(actual.get("sl") or 0)
            actual_tp = float(actual.get("tp") or 0)
            changed = False
            if actual_open and abs(actual_open - p.entry_price) > 0.00001:
                p.entry_price = actual_open
                changed = True
            if actual_sl and abs(actual_sl - p.stop_loss) > 0.00001:
                p.stop_loss = actual_sl
                changed = True
            if actual_tp and abs(actual_tp - p.take_profit_1) > 0.00001:
                p.take_profit_1 = actual_tp
                changed = True
            if changed:
                updated.append(p.direction)
        if updated:
            self._save(local)
        return updated

    def get_active(self, direction: str = None) -> Optional[ActivePosition]:
        """获取活跃持仓（指定方向无匹配时返回 None，不落到其它方向的持仓）"""
        positions = self.load_all()
        if direction:
            for p in positions:
                if p.direction == direction:
                    return p
            return None  # 指定方向无持仓 — 不能误报为"已有同向持仓"
        return positions[0] if positions else None

    def load_all(self) -> List[ActivePosition]:
        """加载所有活跃持仓"""
        if not POSITIONS_FILE.exists():
            return []
        try:
            data = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
            return [ActivePosition(**p) for p in data]
        except Exception:
            return []

    def _save(self, positions: List[ActivePosition]):
        POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        POSITIONS_FILE.write_text(
            json.dumps([asdict(p) for p in positions], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ═══════════════════════════════════════════════
    # TP 接近检测 + 趋势再评估
    # ═══════════════════════════════════════════════

    def check_tp_proximity(self) -> Optional[Dict]:
        """检查活跃持仓是否接近 TP1，生成调整建议

        在每个活跃仓位上:
          1. 获取当前价格
          2. 计算到 TP1 的距离百分比
          3. 如果已走过 >70%→ 触发再评估
          4. 检查趋势是否仍然支持持仓方向
          5. 生成: 建议锁利继续 / 建议止盈

        Returns:
            {"position": ActivePosition, "action": "trail"|"exit"|"hold",
             "current_price": float, "progress_pct": float,
             "trend": str, "new_sl": float, "new_tp": float,
             "suggestion": str} 或 None
        """
        pos = self.get_active()
        if pos is None:
            return None

        # 获取当前价格
        try:
            m15 = self.oanda.load_parquet("M15")
            current_price = float(m15.iloc[-1]["close"])
        except Exception:
            return None

        # 计算 TP 进度
        if pos.direction == "SELL":
            total_move = pos.entry_price - pos.take_profit_1  # 预期下跌幅度
            current_move = pos.entry_price - current_price     # 已下跌幅度
        else:
            total_move = pos.take_profit_1 - pos.entry_price
            current_move = current_price - pos.entry_price

        if total_move <= 0:
            return None  # TP 方向有问题

        progress_pct = current_move / total_move  # 0~1+

        # 已经超过 TP1
        if progress_pct >= 1.0:
            near_tp = True
        # 接近 TP1（30% 以内）
        elif progress_pct >= (1.0 - TP_NEAR_THRESHOLD):
            near_tp = True
        else:
            return None  # 还没到触发阈值

        # ══ 趋势再评估 ══
        trend = self.trend.analyze()
        trend_supports = False

        if pos.direction == "SELL":
            # 做空持仓 → 需要趋势仍然看空
            trend_supports = "BEARISH" in trend.get("overall", "")
        else:
            # 做多持仓 → 需要趋势仍然看多
            trend_supports = "BULLISH" in trend.get("overall", "")

        # ══ 生成建议 ══
        if trend_supports:
            # 趋势支持 → 锁利继续跑
            new_sl, new_tp = self._calc_trail_levels(pos, current_price)
            return {
                "position": pos,
                "action": "trail",
                "current_price": current_price,
                "progress_pct": round(progress_pct * 100),
                "trend": trend.get("reason", ""),
                "trend_supports": True,
                "new_sl": new_sl,
                "new_tp": new_tp,
                "suggestion": self._format_trail_suggestion(
                    pos, current_price, progress_pct, trend.get("reason", ""),
                    new_sl, new_tp
                ),
            }
        else:
            # 趋势不支持 → 建议止盈
            return {
                "position": pos,
                "action": "exit",
                "current_price": current_price,
                "progress_pct": round(progress_pct * 100),
                "trend": trend.get("reason", ""),
                "trend_supports": False,
                "new_sl": None,
                "new_tp": None,
                "suggestion": self._format_exit_suggestion(
                    pos, current_price, progress_pct, trend.get("reason", "")
                ),
            }

    # ═══════════════════════════════════════════════
    # 新 SL / TP 计算
    # ═══════════════════════════════════════════════

    def _calc_trail_levels(self, pos: ActivePosition,
                           current_price: float) -> tuple:
        """计算锁利后的新止损和新止盈

        做空:
          新 SL = 当前价格上方最近 M15 swing high + buffer（锁利）
          新 TP = H4 下一个波段低点 或 Fib 161.8%

        做多:
          新 SL = 当前价格下方最近 M15 swing low - buffer
          新 TP = H4 下一个波段高点 或 Fib 161.8%
        """
        try:
            m15 = self.oanda.load_parquet("M15")
            m15 = self.ind.add_ema(m15, 5)
            m15 = self.ind.add_ema(m15, 15)
        except Exception:
            return None, None

        # 新止损：当前价格附近的 M15 波段极值
        swings = self.ind.find_recent_swing_points_m15(m15, window=5, lookback=36)

        MIN_TRAIL_GAP = 0.00150  # 锁利 SL 距当前价至少 15pips（中长线不能太紧）

        # 追踪止损只用 H4 级别（M15 太窄，不适合中长线持仓）
        sl_h4 = self._h4_trail_sl(current_price, pos.direction)
        new_tp = self._next_h4_level(pos.direction, current_price)

        if sl_h4 is not None:
            if pos.direction == "SELL":
                gap_ok = (sl_h4 - current_price) >= MIN_TRAIL_GAP
                locks_profit = sl_h4 < pos.entry_price
                if gap_ok and locks_profit:
                    new_sl = sl_h4
                else:
                    new_sl = round(current_price + MIN_TRAIL_GAP, 5)
            else:
                gap_ok = (current_price - sl_h4) >= MIN_TRAIL_GAP
                locks_profit = sl_h4 > pos.entry_price
                if gap_ok and locks_profit:
                    new_sl = sl_h4
                else:
                    new_sl = round(current_price - MIN_TRAIL_GAP, 5)
        else:
            new_sl = (
                round(current_price + MIN_TRAIL_GAP, 5)
                if pos.direction == "SELL"
                else round(current_price - MIN_TRAIL_GAP, 5)
            )

        return new_sl, new_tp

    def _h4_trail_sl(self, current_price: float, direction: str) -> Optional[float]:
        """H4 级别的锁利止损候选"""
        try:
            h4 = self.oanda.load_parquet("H4")
            swings = self.ind.find_swing_points(h4, window=5)
            if swings is None:
                return None
            sh, sl = swings
            fib = self.ind.calc_fib_retracement(sh, sl)

            if direction == "SELL":
                # 用 H4 swing high 或 Fib 38.2%
                return round(max(sh + 0.00050, fib["0.382"]), 5)
            else:
                return round(min(sl - 0.00050, fib["0.618"]), 5)
        except Exception:
            return None

    def _next_h4_level(self, direction: str, current_price: float) -> Optional[float]:
        """找 H4 下一个止盈目标"""
        try:
            h4 = self.oanda.load_parquet("H4")
            swings = self.ind.find_swing_points(h4, window=5)
            if swings is None:
                return None

            swing_high, swing_low = swings

            if direction == "SELL":
                # 如果当前价格已跌破前低，用 Fib 延伸
                if current_price <= swing_low:
                    ext = self.ind.calc_fib_extension(swing_high, swing_low, "SELL")
                    return ext.get("1.618")
                return round(swing_low, 5)
            else:
                if current_price >= swing_high:
                    ext = self.ind.calc_fib_extension(swing_high, swing_low, "BUY")
                    return ext.get("1.618")
                return round(swing_high, 5)
        except Exception:
            return None

    # ═══════════════════════════════════════════════
    # H4 反转形态检测（出场信号）
    # ═══════════════════════════════════════════════

    def _detect_reversal_for(self, direction: str, tf: str = "H4") -> Optional[Dict]:
        """反转双重确认检测(共用): 反转形态 + 收盘突破该周期 200SMA

        回测170天结论: H1 周期最优(H4 双重确认90天0次触发, 形同虚设;
        H1 总收益 +56p 且浮亏敞口 -57p→-16p)。周期由 exit.reversal_tf 配置。

        做空持仓: 多头反转形态 AND 收盘价突破 200SMA → 反转
        做多持仓: 空头反转形态 AND 收盘价跌破 200SMA → 反转

        Returns:
            {"pattern": str, "close": float, "sma": float, "reason": str} 或 None
        """
        try:
            tfdf = self.oanda.load_parquet(tf)
            if tfdf.empty or len(tfdf) < 10:
                return None
            tfdf = self.ind.add_sma(tfdf, 200)
        except Exception:
            return None

        recent = tfdf.iloc[-4:]
        t_last = tfdf.iloc[-1]
        t_close = float(t_last["close"])
        t_sma200 = float(t_last["SMA200"])

        if direction == "SELL":
            pattern = self._detect_h4_bullish_reversal(recent)
            if pattern and t_close > t_sma200:
                return {
                    "pattern": pattern, "close": t_close, "sma": t_sma200,
                    "reason": (f"{tf} {pattern} + 收盘{t_close:.5f}突破SMA200"
                               f"({t_sma200:.5f}) → 下降趋势可能反转"),
                }
            elif pattern:
                logger.debug(
                    f"{tf} {pattern} 出现但未突破SMA200 "
                    f"({t_close:.5f} < {t_sma200:.5f})，不触发出场"
                )
        else:
            pattern = self._detect_h4_bearish_reversal(recent)
            if pattern and t_close < t_sma200:
                return {
                    "pattern": pattern, "close": t_close, "sma": t_sma200,
                    "reason": (f"{tf} {pattern} + 收盘{t_close:.5f}跌破SMA200"
                               f"({t_sma200:.5f}) → 上升趋势可能反转"),
                }
            elif pattern:
                logger.debug(
                    f"{tf} {pattern} 出现但未跌破SMA200 "
                    f"({t_close:.5f} > {t_sma200:.5f})，不触发出场"
                )
        return None

    @staticmethod
    def _reversal_tf() -> str:
        """反转检测周期(回测170天结论: H1最优, 默认H1)

        配置优先级: 中控台(dashboard_settings.exit) > config.yaml — 面板改动无需重启
        """
        try:
            exit_yaml = config.load().get("strategy", {}).get("exit", {})
            try:
                from src.utils.dashboard_settings import load as ui_load
                ui = ui_load().get("exit", {}) or {}
            except Exception:
                ui = {}
            tf = str({**exit_yaml, **ui}.get("reversal_tf", "H1")).upper()
            return tf if tf in ("H4", "H1", "M15") else "H1"
        except Exception:
            return "H1"

    def check_reversal_auto_close(self, min_profit_pips: float = 0.0) -> Optional[Dict]:
        """反转信号 + 浮盈 → 自动平仓指令（保本出场）

        检测复用双重确认(_detect_reversal_for, 周期由 exit.reversal_tf 配置)。
        仅在浮盈 >= min_profit_pips 时返回自动平仓指令;
        浮亏/微利时返回 None — 反转提醒走 check_h4_reversal 的建议路径。

        Returns:
            {"position": pos, "action": "auto_close", "pattern": str,
             "float_pips": float, "current_price": float, "reason": str}
            或 None
        """
        pos = self.get_active()
        if pos is None:
            return None

        # ① 反转双重确认
        reversal = self._detect_reversal_for(pos.direction, self._reversal_tf())
        if not reversal:
            return None

        # ② 浮盈判定
        try:
            m15 = self.oanda.load_parquet("M15")
            current_price = float(m15.iloc[-1]["close"])
        except Exception:
            return None
        if pos.direction == "SELL":
            float_pips = (pos.entry_price - current_price) * 10000
        else:
            float_pips = (current_price - pos.entry_price) * 10000

        if float_pips < min_profit_pips:
            logger.info(
                f"🔄 反转信号({reversal['pattern']})出现但浮盈 {float_pips:.1f}pips "
                f"< {min_profit_pips:.0f}pips，不自动平仓(维持建议推送)"
            )
            return None

        return {
            "position": pos,
            "action": "auto_close",
            "pattern": reversal["pattern"],
            "float_pips": round(float_pips, 1),
            "current_price": current_price,
            "reason": reversal["reason"],
        }

    def check_h4_reversal(self) -> Optional[Dict]:
        """检测 H4 反转 + SMA 突破双重确认，生成出场建议

        双重确认（避免正常反弹被误判为反转）:
          做空持仓: H4多头反转形态 AND 收盘价突破 H4 200SMA → 出场
          做多持仓: H4空头反转形态 AND 收盘价跌破 H4 200SMA → 出场

        Returns:
            {"position": ..., "action": "exit_reversal",
             "pattern": str, "reason": str, "suggestion": str}
            或 None
        """
        pos = self.get_active()
        if pos is None:
            return None

        reversal = self._detect_reversal_for(pos.direction, self._reversal_tf())
        if not reversal:
            return None

        return {
            "position": pos,
            "action": "exit_reversal",
            "pattern": reversal["pattern"],
            "reason": reversal["reason"],
            "suggestion": self._format_exit_reversal(
                pos, reversal["pattern"], pos.direction,
                reversal["close"], reversal["sma"],
            ),
        }

    def _detect_h4_bullish_reversal(self, df) -> str:
        """H4 多头反转形态检测"""
        if len(df) < 3:
            return ""
        # 启明星
        if self.ind.is_morning_star(df):
            return "启明星"
        # 锤子线
        if self.ind.is_bullish_reversal(df):
            last = df.iloc[-1]
            cl, op = float(last["close"]), float(last["open"])
            lower = min(op, cl) - float(last["low"])
            body = abs(cl - op)
            if lower >= body * 2 and cl > op:
                return "锤子线"
        # 阳吞阴
        if len(df) >= 2:
            prev, last = df.iloc[-2], df.iloc[-1]
            if (float(last["close"]) > float(last["open"]) and
                float(prev["close"]) < float(prev["open"]) and
                float(last["close"]) > float(prev["open"]) and
                float(last["open"]) < float(prev["close"])):
                return "阳吞阴"
        # 看涨孕线
        if self.ind.is_bullish_harami(df):
            return "看涨孕线"
        return ""

    def _detect_h4_bearish_reversal(self, df) -> str:
        """H4 空头反转形态检测"""
        if len(df) < 3:
            return ""
        if self.ind.is_evening_star(df):
            return "黄昏星"
        if self.ind.is_bearish_reversal(df):
            last = df.iloc[-1]
            cl, op = float(last["close"]), float(last["open"])
            upper = float(last["high"]) - max(op, cl)
            body = abs(cl - op)
            if upper >= body * 2 and cl < op:
                return "射击之星"
        if len(df) >= 2:
            prev, last = df.iloc[-2], df.iloc[-1]
            if (float(last["close"]) < float(last["open"]) and
                float(prev["close"]) > float(prev["open"]) and
                float(last["close"]) < float(prev["open"]) and
                float(last["open"]) > float(prev["close"])):
                return "阴吞阳"
        if self.ind.is_bearish_harami(df):
            return "看跌孕线"
        return ""

    def _format_exit_reversal(self, pos, pattern: str, direction: str,
                              price: float = 0, sma: float = 0) -> str:
        """格式化 H4 反转出场建议"""
        try:
            m15 = self.oanda.load_parquet("M15")
            current_price = float(m15.iloc[-1]["close"])
        except Exception:
            current_price = 0

        float_pips = (
            (pos.entry_price - current_price) if direction == "SELL"
            else (current_price - pos.entry_price)
        ) * 10000
        pips_sign = "+" if float_pips >= 0 else ""

        tf = self._reversal_tf()
        return (
            f"⚠️ 出场信号 — {tf} 反转 + SMA 突破（双重确认）\n"
            f"{'─' * 20}\n"
            f"📊 {tf} 形态: {pattern}\n"
            f"📈 {tf} 收盘: {price:.5f} "
            f"{'突破' if direction == 'SELL' else '跌破'} "
            f"SMA200({sma:.5f})\n"
            f"💡 含义: {'下降趋势可能反转，空头该离场了' if direction == 'SELL' else '上升趋势可能反转，多头该离场了'}\n"
            f"\n"
            f"💰 当前价: {current_price:.5f}\n"
            f"💵 浮动盈亏: {pips_sign}{float_pips:.1f} pips\n"
            f"\n"
            f"🛑 建议: 手动平仓或收紧止损到当前价附近"
        )

    # ═══════════════════════════════════════════════
    # 定期持仓状态卡片（定时推送用）
    # ═══════════════════════════════════════════════

    def get_status_card(self) -> Optional[str]:
        """生成持仓状态卡片 — 用于定时推送提醒

        包含: 入场/当前价/浮盈/TPSL距离/趋势/建议操作
        """
        pos = self.get_active()
        if pos is None:
            return None

        try:
            m15 = self.oanda.load_parquet("M15")
            current_price = float(m15.iloc[-1]["close"])
        except Exception:
            return None

        direction_cn = "🔴 做空" if pos.direction == "SELL" else "🟢 做多"

        # 浮动盈亏
        if pos.direction == "SELL":
            float_pips = round((pos.entry_price - current_price) * 10000, 1)
            progress_to_tp = (
                (pos.entry_price - current_price) / (pos.entry_price - pos.take_profit_1) * 100
                if (pos.entry_price - pos.take_profit_1) > 0 else 0
            )
            distance_to_sl = round((pos.stop_loss - current_price) * 10000, 1)
        else:
            float_pips = round((current_price - pos.entry_price) * 10000, 1)
            progress_to_tp = (
                (current_price - pos.entry_price) / (pos.take_profit_1 - pos.entry_price) * 100
                if (pos.take_profit_1 - pos.entry_price) > 0 else 0
            )
            distance_to_sl = round((current_price - pos.stop_loss) * 10000, 1)

        pips_sign = "+" if float_pips >= 0 else ""
        emoji = "📈" if float_pips >= 0 else "📉"

        # 趋势
        try:
            trend = self.trend.analyze()
            trend_line = trend.get("reason", "未知")[:60]
        except Exception:
            trend_line = "未知"

        # 距 TP 的 pips
        distance_to_tp = round(abs(pos.take_profit_1 - current_price) * 10000, 1)

        # 已持仓时间
        try:
            created = datetime.fromisoformat(pos.created_at)
            elapsed = datetime.now(SH_TZ) - created
            hours = elapsed.total_seconds() / 3600
            elapsed_str = f"{hours:.0f}h" if hours < 48 else f"{hours/24:.1f}天"
        except Exception:
            elapsed_str = "?"

        # 建议操作
        if progress_to_tp >= 70:
            suggestion = "⚡ 接近止盈！评估趋势决定锁利继续还是止盈"
        elif float_pips <= -pos.sl_pips * 0.7:
            suggestion = "⚠️ 接近止损！检查是否要手动平仓或调整止损"
        elif float_pips > 0:
            suggestion = "🔒 浮盈中，可将止损移至保本价锁定风险"
        else:
            suggestion = "⏳ 等待价格朝持仓方向移动"

        return (
            f"{emoji} 持仓状态 · {direction_cn}\n"
            f"{'─' * 20}\n"
            f"⏰ 已持: {elapsed_str}\n"
            f"💵 入场: {pos.entry_price:.5f}\n"
            f"💰 当前: {current_price:.5f}\n"
            f"📊 浮盈: {pips_sign}{float_pips:.1f} pips\n"
            f"\n"
            f"🛑 止损: {pos.stop_loss:.5f} (距 {distance_to_sl:.0f}pips)\n"
            f"🎯 止盈: {pos.take_profit_1:.5f} (距 {distance_to_tp:.0f}pips, 进度 {progress_to_tp:.0f}%)\n"
            f"📈 趋势: {trend_line}\n"
            f"\n"
            f"💡 {suggestion}"
        )

    # ═══════════════════════════════════════════════
    # 建议文案
    # ═══════════════════════════════════════════════

    def _format_trail_suggestion(self, pos, price, progress, trend,
                                  new_sl, new_tp) -> str:
        direction_cn = "做空" if pos.direction == "SELL" else "做多"
        sl_pips = abs(price - new_sl) * 10000 if new_sl else 0
        tp_pips = abs(new_tp - price) * 10000 if new_tp else 0

        return (
            f"📈 {direction_cn}持仓 — 建议锁利继续\n"
            f"{'─' * 20}\n"
            f"💰 当前价: {price:.5f}\n"
            f"📍 TP1 进度: {progress:.0f}%（接近止盈）\n"
            f"📊 趋势: {trend[:50]}\n"
            f"   → 趋势仍支持持仓 ✅\n"
            f"\n"
            f"🔒 建议新止损: {new_sl:.5f} ({sl_pips:.0f}pips)\n"
            f"   (锁利 — 移到{'入场价下方' if pos.direction == 'SELL' else '入场价上方'})\n"
            f"🎯 建议新止盈: {new_tp:.5f} (+{tp_pips:.0f}pips)\n"
            f"   (下一个 H4 目标)\n"
            f"\n"
            f"⚡ 操作: MT5 修改订单 → 新SL + 新TP → 让利润跑"
        )

    def _format_exit_suggestion(self, pos, price, progress, trend) -> str:
        direction_cn = "做空" if pos.direction == "SELL" else "做多"
        current_pips = abs(price - pos.entry_price) * 10000
        pips_sign = "+" if (
            (pos.direction == "SELL" and price < pos.entry_price) or
            (pos.direction == "BUY" and price > pos.entry_price)
        ) else ""

        return (
            f"⚠️ {direction_cn}持仓 — 建议止盈\n"
            f"{'─' * 20}\n"
            f"💰 当前价: {price:.5f}\n"
            f"📍 TP1 进度: {progress:.0f}%\n"
            f"💵 浮动盈亏: {pips_sign}{current_pips:.0f}pips\n"
            f"📊 趋势: {trend[:50]}\n"
            f"   → 趋势不再支持持仓 ❌\n"
            f"\n"
            f"🛑 建议: 手动平仓或等待 TP1 触发\n"
            f"   大方向已变，继续持仓风险增加"
        )
