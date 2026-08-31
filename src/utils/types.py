"""类型定义 — 核心数据结构的 TypedDict，提升 IDE 支持和代码可读性"""
from typing import TypedDict, Optional


class TrendTimeframeResult(TypedDict):
    """单个时间周期的趋势分析结果"""
    direction: str          # "UP" / "DOWN" / "SIDEWAYS" / "UNKNOWN"
    sma_value: float
    price: float


class TrendResult(TypedDict, total=False):
    """大势分析完整结果"""
    D1: TrendTimeframeResult
    H4: TrendTimeframeResult
    d1_sma_period: int
    h4_sma_period: int
    overall: str            # BULLISH / BEARISH / CONFLICT / NEUTRAL / BULLISH_CAUTIOUS / BEARISH_CAUTIOUS
    can_trade: bool
    reason: str


class SentimentResult(TypedDict):
    """基本面情绪分析结果"""
    score: float            # -1.0 ~ +1.0
    label: str              # 看多 / 偏多 / 中性 / 偏空 / 看空
    bullish_count: int
    bearish_count: int


class HistorySummary(TypedDict, total=False):
    """信号历史统计摘要"""
    total: int
    trade_signals: int
    observe: int
    latest: Optional[dict]
    message: str
