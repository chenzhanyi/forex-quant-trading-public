"""大势判断 — D1 + H4 50 SMA 方向分析"""
import logging
from typing import Dict

import pandas as pd

from src.analysis.indicators import Indicators
from src.data_collection.oanda import OandaClient
from src.utils.config_loader import config

logger = logging.getLogger(__name__)


class TrendAnalyzer:
    """大势分析器

    按你的交易系统:
    D1 50 SMA + H4 200 SMA（经典多周期共振）→ 一致才交易，分歧就观望
    """

    def __init__(self):
        self.cfg = config.load()
        self.ind = Indicators()
        self.oanda = OandaClient()
        self.symbol = self.cfg["project"]["symbol"].replace("/", "_")

    def analyze(self) -> Dict:
        """分析 D1 + H4 趋势

        Returns:
            {
              "D1": {"direction": "UP"/"DOWN"/"SIDEWAYS", "sma_value": float, "price": float},
              "H4": {...},
              "overall": "BULLISH"/"BEARISH"/"CONFLICT"/"NEUTRAL",
              "can_trade": bool,
              "reason": str,
              "sma_period": int
            }
        """
        d1_sma = self.cfg["strategy"]["trend"]["d1_sma_period"]
        h4_sma = self.cfg["strategy"]["trend"]["h4_sma_period"]
        slope_bars = self.cfg["strategy"]["trend"]["slope_bars"]

        results = {"d1_sma_period": d1_sma, "h4_sma_period": h4_sma}

        # D1: 50 SMA
        d1_df = self._load_data("D1")
        if not d1_df.empty and len(d1_df) >= d1_sma + 5:
            d1_df = self.ind.add_sma(d1_df, d1_sma)
            d1_col = f"SMA{d1_sma}"
            d1_dir = self.ind.get_ma_direction(d1_df, d1_col, slope_bars)
            results["D1"] = {
                "direction": d1_dir,
                "sma_value": round(float(d1_df[d1_col].iloc[-1]), 5),
                "price": round(float(d1_df["close"].iloc[-1]), 5),
            }
        else:
            results["D1"] = {"direction": "UNKNOWN", "sma_value": 0, "price": 0}

        # H4: 200 SMA（经典多周期共振）
        h4_df = self._load_data("H4")
        if not h4_df.empty and len(h4_df) >= h4_sma + 5:
            h4_df = self.ind.add_sma(h4_df, h4_sma)
            h4_col = f"SMA{h4_sma}"
            h4_dir = self.ind.get_ma_direction(h4_df, h4_col, slope_bars)
            results["H4"] = {
                "direction": h4_dir,
                "sma_value": round(float(h4_df[h4_col].iloc[-1]), 5),
                "price": round(float(h4_df["close"].iloc[-1]), 5),
            }
        else:
            results["H4"] = {"direction": "UNKNOWN", "sma_value": 0, "price": 0}

        d1_dir = results.get("D1", {}).get("direction", "UNKNOWN")
        h4_dir = results.get("H4", {}).get("direction", "UNKNOWN")

        if d1_dir == "UP" and h4_dir == "UP":
            results["overall"] = "BULLISH"
            results["can_trade"] = True
            results["reason"] = f"D1 50SMA+H4 200SMA 一致看多 📈"
        elif d1_dir == "DOWN" and h4_dir == "DOWN":
            results["overall"] = "BEARISH"
            results["can_trade"] = True
            results["reason"] = f"D1 50SMA+H4 200SMA 一致看空 📉"
        elif d1_dir == "UP" and h4_dir == "SIDEWAYS":
            results["overall"] = "BULLISH_CAUTIOUS"
            results["can_trade"] = True
            results["reason"] = f"D1 50SMA 看多，H4 200SMA 横盘 — 谨慎做多"
        elif d1_dir == "DOWN" and h4_dir == "SIDEWAYS":
            results["overall"] = "BEARISH_CAUTIOUS"
            results["can_trade"] = True
            results["reason"] = f"D1 50SMA 看空，H4 200SMA 横盘 — 谨慎做空"
        else:
            results["overall"] = "CONFLICT"
            results["can_trade"] = False
            results["reason"] = (
                f"D1={d1_dir}, H4={h4_dir} — 趋势分歧，观望 ⏸"
            )

        return results

    def format_one_liner(self, result: Dict = None) -> str:
        """输出一句话趋势判断（MVP 核心功能）"""
        if result is None:
            result = self.analyze()

        d1 = result.get("D1", {})
        h4 = result.get("H4", {})

        lines = [
            "📊 EUR/USD 大势判断 — D1 50SMA / H4 200SMA",
            f"📈 D1 50SMA: {d1.get('direction', '?')}  "
            f"(值={d1.get('sma_value', 0):.5f}, 价={d1.get('price', 0):.5f})",
            f"📈 H4 200SMA: {h4.get('direction', '?')}  "
            f"(值={h4.get('sma_value', 0):.5f}, 价={h4.get('price', 0):.5f})",
            f"💡 {result.get('reason', '分析中')}",
        ]

        if result.get("can_trade"):
            lines.append("✅ 今日方向明确 — 可寻找入场机会")
        else:
            lines.append("⏸ 观望 — 等待趋势一致")

        return "\n".join(lines)

    def _load_data(self, tf: str) -> pd.DataFrame:
        """加载指定周期的 K 线数据"""
        try:
            df = self.oanda.load_parquet(tf)
            if not df.empty and "close" in df.columns:
                return df
        except Exception:
            pass
        # fallback: 从 OANDA 获取
        candles = self.oanda.fetch_candles(granularity=tf, count=500)
        if candles:
            df = pd.DataFrame(candles)
            df["time"] = pd.to_datetime(df["time"])
            df.set_index("time", inplace=True)
            return df
        return pd.DataFrame()
