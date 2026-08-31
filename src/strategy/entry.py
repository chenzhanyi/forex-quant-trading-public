"""入场信号 — 多条件叠加法（回测验证 72%胜率）

条件:
  做空: H4<200SMA + M15 EMA5<EMA15 + 空头形态 + RSI30-60 + 欧美盘
  做多: H4>200SMA + M15 EMA5>EMA15 + 多头形态 + RSI30-60 + 欧美盘

止损: H4 ATR × 2.5
止盈: 40 pips 固定
"""
import logging
from dataclasses import dataclass
from typing import Optional, Dict

import pandas as pd

from src.analysis.indicators import Indicators
from src.data_collection.oanda import OandaClient
from src.utils.config_loader import config

logger = logging.getLogger(__name__)

BUY = "BUY"
SELL = "SELL"
NONE = "NONE"

# 配置
TP_PIPS = 40          # 固定止盈 40 pips
SL_ATR_MULT = 2.5     # H4 ATR 倍数
RSI_LO, RSI_HI = 30, 60
SESSION_START, SESSION_END = 15, 23  # 欧美盘 CST


@dataclass
class EntrySignal:
    """入场信号"""
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    sl_pips: float
    tp_pips: float
    rr_ratio: float
    signal_type: str          # bearish_pattern / bullish_pattern
    reason: str
    pattern: str
    confidence: int = 3


class EntryDetector:
    """多条件叠加入场检测"""

    def __init__(self):
        self.cfg = config.load()
        self.ind = Indicators()
        self.oanda = OandaClient()
        self._rsi = self._load_rsi_cfg()

    def _load_rsi_cfg(self) -> dict:
        """RSI 过滤器配置: 中控台优先 > YAML 兜底 > 默认关闭(原始EURUSD设计)"""
        try:
            from src.utils.dashboard_settings import load as ui_load
            ui = ui_load().get("eurusd", {})
            if ui and "rsi_enabled" in ui:
                return {"enabled": bool(ui["rsi_enabled"]),
                        "lo": float(ui.get("rsi_lo", RSI_LO)),
                        "hi": float(ui.get("rsi_hi", RSI_HI))}
        except Exception:
            pass
        y = self.cfg.get("strategy", {}).get("entry", {}).get("rsi", {})
        return {"enabled": bool(y.get("enabled", False)),
                "lo": float(y.get("lo", RSI_LO)),
                "hi": float(y.get("hi", RSI_HI))}

    # ═══════════════════════════
    # 公开 API
    # ═══════════════════════════

    def detect(self, trend_overall: str = "") -> Optional[EntrySignal]:
        """双向检测，返回第一个满足条件的信号"""
        self._rsi = self._load_rsi_cfg()   # 每次实时读面板, 避免仅重启生效
        sell = self._detect_sell()
        if sell: return sell
        return self._detect_buy()

    # ═══════════════════════════
    # 做空
    # ═══════════════════════════

    def _detect_sell(self) -> Optional[EntrySignal]:
        """6 条件做空检测"""
        m15 = self._prepare_m15()
        if m15 is None: return None

        bar = m15.iloc[-1]
        p = float(bar['close'])
        dt = bar.name

        # ① H4 收盘 < 200SMA
        h4_sma200 = self._get_h4_sma200()
        if h4_sma200 is None or p >= h4_sma200:
            return None

        # ② M15 EMA5 < EMA15
        e5 = float(bar['EMA5']); e15 = float(bar['EMA15'])
        if e5 >= e15:
            return None

        # ③ 空头形态
        pattern = self._detect_any_bearish(m15)
        if not pattern:
            return None

        # ④ RSI 过滤器 (可配, 默认关闭 — 原始EURUSD设计)
        rsi = float(bar.get('RSI14', 50))
        if self._rsi["enabled"] and (rsi < self._rsi["lo"] or rsi > self._rsi["hi"]):
            return None

        # ⑤ 欧美盘时段 (15:00-23:00 CST)
        hour_cst = (dt.hour + 8) % 24
        if hour_cst < SESSION_START or hour_cst > SESSION_END:
            return None

        # ⑥ 止损止盈
        h4_atr = self._get_h4_atr()
        if h4_atr is None: return None
        sl = round(p + h4_atr * SL_ATR_MULT, 5)
        tp = round(p - TP_PIPS / 10000, 5)
        sl_pips = round(h4_atr * SL_ATR_MULT * 10000, 1)
        rr = TP_PIPS / sl_pips if sl_pips > 0 else 0
        if rr < 1.0: return None  # R:R 至少 1:1

        logger.info(
            f"🔴 做空信号: {pattern} @ {p:.5f} | "
            f"SL={sl:.5f}({sl_pips:.0f}p) TP={tp:.5f}(40p) R:R={rr:.1f}"
        )
        return EntrySignal(
            direction=SELL, entry_price=p, stop_loss=sl, take_profit=tp,
            sl_pips=sl_pips, tp_pips=TP_PIPS, rr_ratio=round(rr, 2),
            signal_type="bearish_pattern", pattern=pattern,
            reason=f"{pattern}+H4<200SMA+EMA空{'| RSI'+str(round(rsi)) if self._rsi['enabled'] else ''}+欧美盘"
        )

    # ═══════════════════════════
    # 做多 (镜像)
    # ═══════════════════════════

    def _detect_buy(self) -> Optional[EntrySignal]:
        """6 条件做多检测"""
        m15 = self._prepare_m15()
        if m15 is None: return None

        bar = m15.iloc[-1]
        p = float(bar['close'])
        dt = bar.name

        h4_sma200 = self._get_h4_sma200()
        if h4_sma200 is None or p <= h4_sma200:
            return None

        e5 = float(bar['EMA5']); e15 = float(bar['EMA15'])
        if e5 <= e15:
            return None

        pattern = self._detect_any_bullish(m15)
        if not pattern:
            return None

        rsi = float(bar.get('RSI14', 50))
        if self._rsi["enabled"] and (rsi < self._rsi["lo"] or rsi > self._rsi["hi"]):
            return None

        hour_cst = (dt.hour + 8) % 24
        if hour_cst < SESSION_START or hour_cst > SESSION_END:
            return None

        h4_atr = self._get_h4_atr()
        if h4_atr is None: return None
        sl = round(p - h4_atr * SL_ATR_MULT, 5)
        tp = round(p + TP_PIPS / 10000, 5)
        sl_pips = round(h4_atr * SL_ATR_MULT * 10000, 1)
        rr = TP_PIPS / sl_pips if sl_pips > 0 else 0
        if rr < 1.0: return None

        logger.info(
            f"🟢 做多信号: {pattern} @ {p:.5f} | "
            f"SL={sl:.5f}({sl_pips:.0f}p) TP={tp:.5f}(40p) R:R={rr:.1f}"
        )
        return EntrySignal(
            direction=BUY, entry_price=p, stop_loss=sl, take_profit=tp,
            sl_pips=sl_pips, tp_pips=TP_PIPS, rr_ratio=round(rr, 2),
            signal_type="bullish_pattern", pattern=pattern,
            reason=f"{pattern}+H4>200SMA+EMA多{'| RSI'+str(round(rsi)) if self._rsi['enabled'] else ''}+欧美盘"
        )

    # ═══════════════════════════
    # 辅助方法
    # ═══════════════════════════

    def _prepare_m15(self) -> Optional[pd.DataFrame]:
        try:
            df = self.oanda.load_parquet("M15")
        except FileNotFoundError:
            return None
        if df.empty or len(df) < 20: return None
        df = self.ind.add_ema(df, 5)
        df = self.ind.add_ema(df, 15)
        df = self.ind.add_rsi(df, 14)
        return df

    def _get_h4_sma200(self) -> Optional[float]:
        try:
            h4 = self.oanda.load_parquet("H4")
            if h4.empty: return None
            h4 = self.ind.add_sma(h4, 200)
            return float(h4.iloc[-1]['SMA200'])
        except Exception:
            return None

    def _get_h4_atr(self) -> Optional[float]:
        try:
            h4 = self.oanda.load_parquet("H4")
            if h4.empty: return None
            h4 = self.ind.add_atr(h4, 14)
            return float(h4.iloc[-1]['ATR'])
        except Exception:
            return None

    def _detect_any_bearish(self, m15) -> str:
        """检测任意空头形态"""
        w = m15.iloc[-6:]
        if self.ind.is_bearish_reversal(w): return "射击之星"
        if self.ind.is_evening_star(w): return "黄昏星"
        if self.ind.is_bearish_harami(w): return "看跌孕线"
        if self.ind.is_decisive_bearish(w): return "坚决大阴"
        if len(w) >= 2:
            prev, last = w.iloc[-2], w.iloc[-1]
            if (float(last['close']) < float(last['open']) and
                float(prev['close']) > float(prev['open']) and
                float(last['close']) < float(prev['open'])):
                return "阴吞阳"
        return ""

    def _detect_any_bullish(self, m15) -> str:
        """检测任意多头形态"""
        w = m15.iloc[-6:]
        if self.ind.is_bullish_reversal(w): return "锤子线"
        if self.ind.is_morning_star(w): return "启明星"
        if self.ind.is_bullish_harami(w): return "看涨孕线"
        if self.ind.is_decisive_bullish(w): return "坚决大阳"
        if len(w) >= 2:
            prev, last = w.iloc[-2], w.iloc[-1]
            if (float(last['close']) > float(last['open']) and
                float(prev['close']) < float(prev['open']) and
                float(last['close']) > float(prev['open'])):
                return "阳吞阴"
        return ""

    def get_h4_targets(self, direction: str) -> Dict[str, float]:
        """简化为固定止盈"""
        return {"tp1": TP_PIPS / 10000, "tp2": TP_PIPS / 10000}
