"""澳元 AUD/USD 入场检测 — 独立于 EURUSD 与黄金

策略 = EUR/USD 六条件法移植, 按 170 天回测(实盘口径: 限3单+10天结算窗口)
调参为"方案A" (回测: 84单 58%胜率 +533p, 月均+94p; 熔断贡献+349p):
  - ① 趋势: H4 200SMA (顺势)
  - ② 入场: M15 EMA5/EMA15 排列
  - ③ K线组合形态 (与 EURUSD 同款, 含宽松吞没)
  - ④ RSI: 关闭 (回测 0-100 无过滤最优)
  - ⑤ 时段: 8:00-16:00 北京时间 (悉尼盘+伦敦盘 — 澳元活跃时段;
        欧美盘 15-23 回测砍半收益, 不用)
  - ⑥ 止损: H4 ATR × 2.0 | 止盈: 固定 35p | RR≥1.0
  - ⑦ D1双K线强度参考(回测340/170/90天三窗口一致: 收益+55% / 胜率+11pp / 回撤-36%):
        最近两根已收盘D1净实体偏多→只放BUY, 偏空→只放SELL, 无倾向→双向放行

关键差异 vs EURUSD: TP40 对澳元偏大(波动低, 48h内磨不到);
SL 2.5 倍会让 RR 门槛拦死一半信号 → 2.0 倍 + TP35。
"""
import logging
import math
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.analysis.indicators import Indicators
from src.data_collection.oanda import OandaClient
from src.utils.config_loader import config

logger = logging.getLogger(__name__)

BUY = "BUY"
SELL = "SELL"

# 默认值（config aud 段可覆盖）— 方案A回测参数
DEFAULT_SL_ATR_MULT = 2.0   # H4 ATR 止损倍数
DEFAULT_TP_PIPS = 35        # 固定止盈 pips
SESSION_START, SESSION_END = 8, 16   # 北京时间


@dataclass
class AudSignal:
    """澳元入场信号"""
    direction: str            # BUY / SELL
    entry_price: float
    stop_loss: float
    take_profit: float
    sl_pips: float            # 止损 pips
    tp_pips: float            # 止盈 pips
    rr_ratio: float
    pattern: str
    reason: str


class AudEntryDetector:
    """澳元入场检测器"""

    def __init__(self):
        self.cfg = config.load()
        self.aud_cfg = self.cfg.get("aud", {})
        self.ind = Indicators()
        self.oanda = OandaClient()
        self.symbol = self.aud_cfg.get("symbol", "AUD/USD")
        self.symbol_oanda = self.symbol.replace("/", "_")  # AUD_USD
        self.trend_sma = int(self.aud_cfg.get("trend_sma", 200))
        self.sl_atr_mult = float(self.aud_cfg.get("sl_atr_mult", DEFAULT_SL_ATR_MULT))
        self.tp_pips = float(self.aud_cfg.get("tp_pips", DEFAULT_TP_PIPS))
        self.session_start = int(self.aud_cfg.get("session_start", SESSION_START))
        self.session_end = int(self.aud_cfg.get("session_end", SESSION_END))

    def detect(self) -> Optional[AudSignal]:
        """双向检测"""
        sell = self._detect_sell()
        if sell: return sell
        return self._detect_buy()

    # ── D1双K线强度参考(回测 --d1-bias 同款, 面板改动无需重启) ──

    @property
    def d1_bias(self) -> bool:
        """D1双K线强度参考开关: 中控台优先 > YAML 兜底(默认开)
        (回测340/170/90天三窗口一致: 收益+55% / 胜率+11pp / 回撤-36%)"""
        try:
            from src.utils.dashboard_settings import load as ui_load
            ui = ui_load().get("aud", {})
            if ui and "d1_bias" in ui:
                return bool(ui["d1_bias"])
        except Exception:
            pass
        return bool(self.aud_cfg.get("d1_bias", True))

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
        return df

    def prepare_h1(self) -> Optional[pd.DataFrame]:
        """H1 + SMA200 (反转出场检测用, 引擎结算复用)"""
        try:
            h1 = self.oanda.load_parquet("H1", symbol=self.symbol_oanda)
        except Exception:
            return None
        if h1.empty: return None
        h1 = self.ind.add_sma(h1, 200)
        return h1

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
        """返回 (h4_df, h4_sma, h4_atr) 或 (None, None, None)"""
        h4 = self._get_h4()
        if h4 is None:
            return None, None, None
        last = h4.iloc[-1]
        sma = float(last[f"SMA{self.trend_sma}"])
        atr = float(last["ATR"])
        # NaN防护(与回测 `if not (h4_sma > 0): continue` 对齐):
        # 数据不足时 NaN 比较恒为 False 会放行趋势检查 → 显式判 None
        if math.isnan(sma):
            sma = None
        return h4, sma, atr

    def h4_flat_range(self, h4_df=None, bars: int = 12) -> Optional[float]:
        """近 bars 根 H4 区间(pips) — 横盘判定用(引擎 flat 过滤复用)"""
        h4 = h4_df if h4_df is not None else self._get_h4()
        if h4 is None or len(h4) < bars:
            return None
        w = h4.iloc[-bars:]
        return (float(w['high'].max()) - float(w['low'].min())) * 10000

    def _session_ok(self, dt) -> bool:
        hc = (dt.hour + 8) % 24
        # 支持跨午夜(8-16 不跨, 保持简单: 直接区间判断)
        return self.session_start <= hc <= self.session_end

    def _build_signal(self, direction, p, dt, atr, pattern) -> Optional[AudSignal]:
        """构造信号: ATR×2.0 止损 + 固定 35p 止盈 + RR≥1.0"""
        if atr is None or not (atr > 0):
            return None
        sl_pips = round(atr * self.sl_atr_mult * 10000, 1)
        if self.tp_pips / sl_pips < 1.0:  # RR≥1.0
            return None
        if direction == SELL:
            sl = round(p + sl_pips / 10000, 5)
            tp = round(p - self.tp_pips / 10000, 5)
        else:
            sl = round(p - sl_pips / 10000, 5)
            tp = round(p + self.tp_pips / 10000, 5)
        rr = round(self.tp_pips / sl_pips, 2)

        reason = (f"{pattern}+H4{'<' if direction == SELL else '>'}SMA{self.trend_sma}"
                  f"+EMA{'空' if direction == SELL else '多'}"
                  f"+亚欧盘{self.session_start}-{self.session_end}")

        return AudSignal(
            direction=direction, entry_price=p, stop_loss=sl, take_profit=tp,
            sl_pips=sl_pips, tp_pips=self.tp_pips, rr_ratio=rr,
            pattern=pattern, reason=reason,
        )

    def _detect_sell(self) -> Optional[AudSignal]:
        m15 = self._prepare_m15()
        if m15 is None: return None
        bar = m15.iloc[-1]
        p = float(bar['close'])
        dt = bar.name

        _, sma, atr = self._get_h4_meta()
        if sma is None or p >= sma: return None

        # ⑦ D1双K线强度参考(与回测 --d1-bias 同款): 净偏多 → 不做空
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

        if not self._session_ok(dt): return None

        sig = self._build_signal(SELL, p, dt, atr, pattern)
        if sig:
            logger.info(f"🦘 澳元做空: {pattern} @ {p:.5f} SL={sig.stop_loss} TP={sig.take_profit}")
        return sig

    def _detect_buy(self) -> Optional[AudSignal]:
        m15 = self._prepare_m15()
        if m15 is None: return None
        bar = m15.iloc[-1]
        p = float(bar['close'])
        dt = bar.name

        _, sma, atr = self._get_h4_meta()
        if sma is None or p <= sma: return None

        # ⑦ D1双K线强度参考(与回测 --d1-bias 同款): 净偏空 → 不做多
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

        if not self._session_ok(dt): return None

        sig = self._build_signal(BUY, p, dt, atr, pattern)
        if sig:
            logger.info(f"🦘 澳元做多: {pattern} @ {p:.5f} SL={sig.stop_loss} TP={sig.take_profit}")
        return sig

    # 形态检测 — 与回测/EURUSD 同款(含宽松吞没分支)

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

    def detect_reversal_h1(self, direction: str, h1_df=None) -> str:
        """H1 反转出场检测 — 与回测 sim_reversal_exit 同款双重确认

        反转形态 + H1 收盘突破 200SMA → 返回形态名, 否则 ""
        (引擎按浮盈≥阈值决定是否平仓)
        """
        h1 = h1_df if h1_df is not None else self.prepare_h1()
        if h1 is None or len(h1) < 201:
            return ""
        last = h1.iloc[-1]
        sma = float(last['SMA200'])
        c = float(last['close'])
        # 持仓 SELL → 查多头反转(收盘上破200SMA); BUY → 查空头反转(收盘下破)
        if direction == SELL:
            if c <= sma:
                return ""
            w = h1.iloc[-6:]
            if self.ind.is_morning_star(w): return "启明星"
            if self.ind.is_bullish_reversal(w): return "锤子线"
            if self.ind.is_bullish_harami(w): return "看涨孕线"
            if len(w) >= 2:
                prev, ls = w.iloc[-2], w.iloc[-1]
                if (float(ls['close']) > float(ls['open']) and
                    float(prev['close']) < float(prev['open']) and
                    float(ls['close']) > float(prev['open'])):
                    return "阳吞阴"
        else:
            if c >= sma:
                return ""
            w = h1.iloc[-6:]
            if self.ind.is_evening_star(w): return "黄昏星"
            if self.ind.is_bearish_reversal(w): return "射击之星"
            if self.ind.is_bearish_harami(w): return "看跌孕线"
            if len(w) >= 2:
                prev, ls = w.iloc[-2], w.iloc[-1]
                if (float(ls['close']) < float(ls['open']) and
                    float(prev['close']) > float(prev['open']) and
                    float(ls['close']) < float(prev['open'])):
                    return "阴吞阳"
        return ""
