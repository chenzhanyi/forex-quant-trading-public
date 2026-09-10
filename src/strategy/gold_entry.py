"""黄金 XAU/USD 入场检测 — 独立于 EURUSD

策略(= EUR/USD 六条件法, 品种换 XAU):
  - ① 趋势: H4 200SMA
  - ② 入场: M15 EMA5/EMA15 排列
  - ③ K线组合形态
  - ④ RSI 30-60 (回测证实 RSI 是关键过滤器)
  - ⑤ 时段: 仅欧美盘
  - ⑥ 止损: H4 ATR × 2.5 | 止盈: 固定 $100 | RR≥1.0

回测: 18信号 / 61%胜率 / +$560/90天 (优于 EUR/USD)
"""
import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.analysis.indicators import Indicators
from src.data_collection.oanda import OandaClient
from src.utils.config_loader import config

logger = logging.getLogger(__name__)

BUY = "BUY"
SELL = "SELL"

# 默认值（config gold 段可覆盖）
DEFAULT_SL_ATR_MULT = 2.5   # H4 ATR 止损倍数
DEFAULT_TP_USD = 100        # 固定止盈美元
RSI_LO, RSI_HI = 30, 60


@dataclass
class GoldSignal:
    """黄金入场信号"""
    direction: str            # BUY / SELL
    entry_price: float
    stop_loss: float
    take_profit: float
    sl_usd: float             # 止损美元
    tp_usd: float             # 止盈美元
    rr_ratio: float
    pattern: str
    reason: str


class GoldEntryDetector:
    """黄金入场检测器"""

    def __init__(self):
        self.cfg = config.load()
        self.gold_cfg = self.cfg.get("gold", {})
        self.ind = Indicators()
        self.oanda = OandaClient()
        self.symbol = self.gold_cfg.get("symbol", "XAU/USD")
        self.symbol_oanda = self.symbol.replace("/", "_")  # XAU_USD
        self.trend_sma = int(self.gold_cfg.get("trend_sma", 200))
        self.sl_atr_mult = float(self.gold_cfg.get("sl_atr_mult", DEFAULT_SL_ATR_MULT))
        self.tp_usd = float(self.gold_cfg.get("tp_usd", DEFAULT_TP_USD))
        self.rsi_cfg = self.gold_cfg.get("rsi", {})
        self.rsi_enabled = bool(self.rsi_cfg.get("enabled", True))
        self.rsi_lo = float(self.rsi_cfg.get("lo", RSI_LO))
        self.rsi_hi = float(self.rsi_cfg.get("hi", RSI_HI))
        self.session_only = bool(self.gold_cfg.get("session_only", True))

    def detect(self) -> Optional[GoldSignal]:
        """双向检测"""
        sell = self._detect_sell()
        if sell: return sell
        return self._detect_buy()

    # ── D1双K线强度参考(回测 --d1-bias 同款, 面板改动无需重启) ──

    @property
    def d1_bias(self) -> bool:
        """D1双K线强度参考开关: 中控台优先 > YAML 兜底(默认开)
        (回测170天: 收益+22% / 回撤-35%; 90天: 回撤-65%, 未结算浮亏-210→浮盈+54)"""
        try:
            from src.utils.dashboard_settings import load as ui_load
            ui = ui_load().get("gold", {})
            if ui and "d1_bias" in ui:
                return bool(ui["d1_bias"])
        except Exception:
            pass
        return bool(self.gold_cfg.get("d1_bias", True))

    def _d1_net_body(self, dt):
        """D1 最近两根已收盘K线净实体: >0偏多 / <0偏空 / None数据不足 —
        与回测 _d1_bias 同语义(数据不足=无倾向, 不拦)"""
        try:
            d1 = self.oanda.load_parquet("D1", symbol=self.symbol_oanda)
        except Exception:
            return None
        if d1 is None or len(d1) < 2:
            return None
        db = d1[d1.index <= dt]
        if len(db) < 2:
            return None
        return float((db.iloc[-2:]['close'] - db.iloc[-2:]['open']).sum())

    def _prepare_m15(self) -> Optional[pd.DataFrame]:
        try:
            df = self.oanda.load_parquet("M15", symbol=self.symbol_oanda)
        except FileNotFoundError:
            return None
        if df.empty or len(df) < 20: return None
        df = self.ind.add_ema(df, 5)
        df = self.ind.add_ema(df, 15)
        df = self.ind.add_rsi(df, 14)
        return df

    def _get_h4(self) -> Optional[pd.DataFrame]:
        """加载 H4 并算 SMA200 + ATR14"""
        try:
            h4 = self.oanda.load_parquet("H4", symbol=self.symbol_oanda)
        except Exception:
            return None
        if h4.empty: return None
        h4 = self.ind.add_sma(h4, self.trend_sma)
        h4 = self.ind.add_atr(h4, 14)
        return h4

    def _get_h4_meta(self):
        """返回 (h4_sma, h4_atr) 或 (None, None)"""
        h4 = self._get_h4()
        if h4 is None:
            return None, None
        last = h4.iloc[-1]
        return float(last[f"SMA{self.trend_sma}"]), float(last["ATR"])

    def _session_ok(self, dt) -> bool:
        if not self.session_only:
            return True
        hc = (dt.hour + 8) % 24
        return 15 <= hc <= 23

    def _build_signal(self, direction, p, dt, sma, atr, pattern, rsi):
        """构造信号: ATR止损 + 固定TP + RR≥1.0"""
        if atr is None or atr == 0:
            return None
        sl_usd = round(atr * self.sl_atr_mult, 2)
        if self.tp_usd / sl_usd < 1.0:  # RR≥1.0
            return None
        if direction == SELL:
            sl = round(p + sl_usd, 2)
            tp = round(p - self.tp_usd, 2)
        else:
            sl = round(p - sl_usd, 2)
            tp = round(p + self.tp_usd, 2)
        rr = round(self.tp_usd / sl_usd, 2)

        reason = f"{pattern}+H4{'<' if direction==SELL else '>'}SMA{self.trend_sma}+EMA{'空' if direction==SELL else '多'}"
        if self.rsi_enabled:
            reason += f"+RSI{rsi:.0f}"
        reason += "+欧美盘" if self.session_only else ""

        return GoldSignal(
            direction=direction, entry_price=p, stop_loss=sl, take_profit=tp,
            sl_usd=sl_usd, tp_usd=self.tp_usd, rr_ratio=rr,
            pattern=pattern, reason=reason,
        )

    def _detect_sell(self) -> Optional[GoldSignal]:
        m15 = self._prepare_m15()
        if m15 is None: return None
        bar = m15.iloc[-1]
        p = float(bar['close'])
        dt = bar.name

        sma, atr = self._get_h4_meta()
        if sma is None or p >= sma: return None

        # ①b D1双K线强度参考(与回测 --d1-bias 同款): 净偏多 → 不做空
        # 数据不足=无倾向不拦(与回测一致); 净实体=0 也双向放行
        if self.d1_bias:
            net = self._d1_net_body(dt)
            if net is not None and net > 0:
                return None

        e5 = float(bar['EMA5']); e15 = float(bar['EMA15'])
        if e5 >= e15: return None

        w = m15.iloc[-6:]
        pattern = self._detect_bearish_pattern(w)
        if not pattern: return None

        rsi = float(bar['RSI14'])
        if self.rsi_enabled and (rsi < self.rsi_lo or rsi > self.rsi_hi):
            return None

        if not self._session_ok(dt): return None

        sig = self._build_signal(SELL, p, dt, sma, atr, pattern, rsi)
        if sig:
            logger.info(f"🥇 黄金做空: {pattern} @ {p:.2f} SL={sig.stop_loss} TP={sig.take_profit} RSI={rsi:.0f}")
        return sig

    def _detect_buy(self) -> Optional[GoldSignal]:
        m15 = self._prepare_m15()
        if m15 is None: return None
        bar = m15.iloc[-1]
        p = float(bar['close'])
        dt = bar.name

        sma, atr = self._get_h4_meta()
        if sma is None or p <= sma: return None

        # ①b D1双K线强度参考(与回测 --d1-bias 同款): 净偏空 → 不做多
        # 数据不足=无倾向不拦(与回测一致); 净实体=0 也双向放行
        if self.d1_bias:
            net = self._d1_net_body(dt)
            if net is not None and net < 0:
                return None

        e5 = float(bar['EMA5']); e15 = float(bar['EMA15'])
        if e5 <= e15: return None

        w = m15.iloc[-6:]
        pattern = self._detect_bullish_pattern(w)
        if not pattern: return None

        rsi = float(bar['RSI14'])
        if self.rsi_enabled and (rsi < self.rsi_lo or rsi > self.rsi_hi):
            return None

        if not self._session_ok(dt): return None

        sig = self._build_signal(BUY, p, dt, sma, atr, pattern, rsi)
        if sig:
            logger.info(f"🥇 黄金做多: {pattern} @ {p:.2f} SL={sig.stop_loss} TP={sig.take_profit} RSI={rsi:.0f}")
        return sig

    def _detect_bearish_pattern(self, w) -> str:
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

    def _detect_bullish_pattern(self, w) -> str:
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