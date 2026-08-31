"""错失机会追踪 — 回看观望信号中 >60pips 波动的情况，记录特征用于优化入场

用法:
    tracker = OpportunityTracker()
    report = tracker.scan(days=1, min_pips=60)
    # → 返回错失机会列表 + 保存特征到 data/opportunities/
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from src.data_collection.oanda import OandaClient

logger = logging.getLogger(__name__)

# 上海时区
SH_TZ = timezone(timedelta(hours=8))


class OpportunityTracker:
    """错失机会追踪器

    每天回看过去的观望信号，检查是否存在 >60pips 的显著波动。
    如果存在，记录当时的市场特征，积累数据用于优化入场策略。
    """

    def __init__(self):
        proj = Path(__file__).resolve().parent.parent.parent
        self.history_path = proj / "data" / "signal_history.jsonl"
        self.out_dir = proj / "data" / "opportunities"
        self.data_dir = proj / "data" / "forex"
        self.oanda = OandaClient()

    def scan(self, days: int = 1, min_pips: float = 60) -> Dict:
        """扫描过去 N 天的观望信号，找出错失的大波动机会

        Args:
            days: 回看天数
            min_pips: 最小波动阈值（pip）

        Returns:
            {"total_observe": int, "missed": int, "opportunities": [...], "summary": str}
        """
        signals = self._load_observe_signals(days)
        if not signals:
            return {"total_observe": 0, "missed": 0, "opportunities": [], "summary": "近{days}天无观望信号"}

        opportunities = []
        for sig in signals:
            result = self._check_signal(sig, min_pips)
            if result:
                opportunities.append(result)

        # 保存
        if opportunities:
            self._save(opportunities)

        summary = self._summarize(signals, opportunities, min_pips)
        return {
            "total_observe": len(signals),
            "missed": len(opportunities),
            "opportunities": opportunities,
            "summary": summary,
        }

    def _load_observe_signals(self, days: int) -> List[Dict]:
        """加载过去 N 天的观望信号"""
        if not self.history_path.exists():
            return []

        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        signals = []
        with open(self.history_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # 只取观望信号
                if rec.get("can_trade", False):
                    continue
                ts_str = rec.get("recorded_at") or rec.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str).timestamp()
                except (ValueError, TypeError):
                    continue
                if ts < cutoff:
                    continue

                signals.append(rec)
        return signals

    def _check_signal(self, sig: Dict, min_pips: float) -> Optional[Dict]:
        """检查单个观望信号是否有大波动"""
        sig_ts_str = sig.get("recorded_at") or sig.get("timestamp", "")
        try:
            sig_dt = datetime.fromisoformat(sig_ts_str)
        except (ValueError, TypeError):
            return None

        # 获取信号后的 H1 价格数据
        h1 = self._load_h1_since(sig_dt)
        if h1 is None or len(h1) < 3:
            return None

        # 信号时的价格
        sig_price = self._get_price_at(h1, sig_dt)
        if sig_price is None:
            sig_price = float(h1["close"].iloc[0])

        # 信号后的价格范围
        after = h1[h1.index > sig_dt.replace(tzinfo=None)] if len(h1) > 0 else h1
        if after.empty:
            after = h1.iloc[1:]

        if after.empty or len(after) < 2:
            return None

        max_price = float(after["high"].max())
        min_price = float(after["low"].min())
        max_up = round((max_price - sig_price) * 10000, 1)
        max_down = round((sig_price - min_price) * 10000, 1)
        max_move = max(max_up, max_down)

        if max_move < min_pips:
            return None  # 波动不够大

        # 方向
        direction = "up" if max_up >= max_down else "down"

        # ── 提取特征 ──
        features = self._extract_features(sig, sig_dt, h1)

        return {
            "signal_time": sig_dt.isoformat(),
            "signal_trend": sig.get("trend", ""),
            "signal_d1": sig.get("d1", ""),
            "signal_h4": sig.get("h4", ""),
            "skip_reason": sig.get("skip_reason", "")[:200],
            "signal_price": round(sig_price, 5),
            "max_price_after": round(max_price, 5),
            "min_price_after": round(min_price, 5),
            "max_move_pips": max_move,
            "move_direction": direction,
            "features": features,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def _extract_features(self, sig: Dict, sig_dt: datetime, h1: pd.DataFrame) -> Dict:
        """提取信号时刻的市场特征"""
        feats = {
            "trend_d1": sig.get("d1", "?"),
            "trend_h4": sig.get("h4", "?"),
            "trend_overall": sig.get("trend", ""),
            "confidence": sig.get("confidence", 0),
        }

        # 尝试获取 H1 技术指标
        try:
            from src.analysis.indicators import Indicators
            ind = Indicators()
            h1_indexed = ind.add_ema(h1, 5)
            h1_indexed = ind.add_ema(h1_indexed, 15)
            h1_indexed = ind.add_atr(h1_indexed, 14)

            last = h1_indexed.iloc[-1]
            feats["h1_ema5"] = round(float(last.get("EMA5", 0)), 5)
            feats["h1_ema15"] = round(float(last.get("EMA15", 0)), 5)
            feats["h1_close"] = round(float(last["close"]), 5)
            feats["h1_atr"] = round(float(last.get("ATR", 0)), 5)
            feats["h1_ema5_gt_ema15"] = bool(feats["h1_ema5"] > feats["h1_ema15"])

            # 斜率
            if len(h1_indexed) >= 5:
                slope_5 = ind.calculate_slope(h1_indexed["EMA5"], 3)
                slope_15 = ind.calculate_slope(h1_indexed["EMA15"], 3)
                feats["h1_ema5_slope"] = round(slope_5, 8)
                feats["h1_ema15_slope"] = round(slope_15, 8)
        except Exception:
            pass

        # 尝试获取 D1/H4 SMA 位置
        try:
            df_d1 = self.oanda.load_parquet("D1")
            df_d1 = Indicators.add_sma(df_d1, 50)
            d1_last = df_d1.iloc[-1]
            feats["d1_sma50"] = round(float(d1_last.get("SMA50", 0)), 5)
            feats["d1_close"] = round(float(d1_last["close"]), 5)
            feats["d1_price_vs_sma"] = "above" if feats["d1_close"] > feats["d1_sma50"] else "below"

            df_h4 = self.oanda.load_parquet("H4")
            df_h4 = Indicators.add_sma(df_h4, 200)
            h4_last = df_h4.iloc[-1]
            feats["h4_sma200"] = round(float(h4_last.get("SMA200", 0)), 5)
            feats["h4_close"] = round(float(h4_last["close"]), 5)
            feats["h4_price_vs_sma"] = "above" if feats["h4_close"] > feats["h4_sma200"] else "below"
        except Exception:
            pass

        return feats

    def _load_h1_since(self, dt: datetime) -> Optional[pd.DataFrame]:
        """加载指定时间之后的 H1 数据"""
        try:
            filepath = self.data_dir / "H1" / "EUR_USD_H1.parquet"
            if not filepath.exists():
                return None
            df = pd.read_parquet(filepath)
            start = dt - timedelta(hours=2)  # 包含信号前的蜡烛
            mask = pd.to_datetime(df.index) >= start
            return df[mask]
        except Exception as e:
            logger.debug(f"加载 H1 数据失败: {e}")
            return None

    @staticmethod
    def _get_price_at(df: pd.DataFrame, dt: datetime) -> Optional[float]:
        """获取最接近指定时间的收盘价"""
        try:
            idx = pd.to_datetime(df.index)
            # 找最近的一根
            diffs = abs(idx - dt.replace(tzinfo=None))
            closest = diffs.idxmin()
            return float(df.loc[closest, "close"])
        except Exception:
            return None

    def _save(self, opportunities: List[Dict]) -> Path:
        """保存错失机会记录"""
        now = datetime.now()
        month_dir = self.out_dir / now.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        filepath = month_dir / f"missed_{now.strftime('%Y%m%d')}.json"

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(opportunities, f, ensure_ascii=False, indent=2)
        logger.info(f"📊 错失机会已保存: {filepath} ({len(opportunities)} 条)")
        return filepath

    def _summarize(self, signals: List, opportunities: List, min_pips: float) -> str:
        """生成可读摘要"""
        total = len(signals)
        missed = len(opportunities)
        rate = f"{missed}/{total} ({missed/total*100:.0f}%)" if total > 0 else "N/A"

        lines = [
            f"📊 错失机会扫描 (>{min_pips}pips)",
            f"   近24h观望信号: {total}",
            f"   错失大波动: {rate}",
        ]

        if opportunities:
            # 统计规律
            trends = {}
            for op in opportunities:
                t = op.get("signal_trend", "?")
                key = t[:20] if t else "?"
                trends[key] = trends.get(key, 0) + 1

            lines.append("")
            lines.append("   📈 趋势分布:")
            for t, c in sorted(trends.items(), key=lambda x: -x[1]):
                lines.append(f"      {t}: {c} 次")

            # 方向分布
            up = sum(1 for o in opportunities if o.get("move_direction") == "up")
            down = sum(1 for o in opportunities if o.get("move_direction") == "down")
            lines.append(f"   📉 波动方向: ↑{up} ↓{down}")

            # 列出具体信号
            lines.append("")
            lines.append("   📋 详情:")
            for i, op in enumerate(opportunities[:5], 1):
                dt_str = op["signal_time"][:16].replace("T", " ")
                lines.append(
                    f"   {i}. {dt_str} | {op.get('signal_trend','?')[:30]} | "
                    f"{op['move_direction']} {op['max_move_pips']:.0f}pips"
                )

        return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tracker = OpportunityTracker()
    result = tracker.scan(days=1, min_pips=60)
    print(result["summary"])
