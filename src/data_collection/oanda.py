"""OANDA v20 API — 行情数据采集（Parquet 存储）"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd

from src.utils.config_loader import config

logger = logging.getLogger(__name__)

OANDA_FIELDS = ["time", "open", "high", "low", "close", "volume"]

# OANDA v20 API granularity 映射
# 本地用 D1/H4, OANDA 用 D/H4
GRANULARITY_MAP = {
    "M5": "M5",
    "M15": "M15",
    "H1": "H1",
    "H4": "H4",
    "D1": "D",
    "D": "D",
    "W1": "W",
}


class OandaClient:
    """OANDA v20 REST API 客户端"""

    def __init__(self):
        self.cfg = config.load()
        self.api_key = self.cfg["oanda"]["api_key"]
        self.account_id = self.cfg["oanda"]["account_id"]
        self.base_url = self.cfg["oanda"]["base_url"]
        self.symbol = self.cfg["project"]["symbol"].replace("/", "_")  # EUR/USD → EUR_USD
        self.data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "forex"
        self.client = config.get_http_client()

        # 检查 API Key 是否已配置
        self._configured = self.api_key and "YOUR_" not in self.api_key

        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def fetch_candles(
        self,
        granularity: str = "H1",
        count: int = 200,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        symbol: Optional[str] = None,
    ) -> List[dict]:
        """获取 K 线数据 — 只返回已收盘的完整 K 线
        防止 look-ahead bias: 只取 complete=True 的 K 线

        Args:
            granularity: M5, M15, H1, H4, D1
            count: 最大条数 (<=5000)
            from_time: 起始时间 (UTC)
            to_time: 结束时间 (UTC)
            symbol: 品种 (如 XAU_USD)，默认用配置的 EUR_USD
        """
        if not self._configured:
            raise ConnectionError(
                "OANDA API Key not configured. "
                "Edit config/config.local.yaml with your OANDA API Key"
            )
        sym = symbol or self.symbol
        url = f"{self.base_url}/v3/instruments/{sym}/candles"
        # 转换粒度: D1 → D, H4 → H4, 等等
        oanda_granularity = GRANULARITY_MAP.get(granularity, granularity)
        params = {
            "granularity": oanda_granularity,
            "price": "M",
            "count": min(count, 5000),
        }
        if from_time:
            params["from"] = from_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        if to_time:
            params["to"] = to_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        resp = self.client.get(url, headers=self._headers, params=params)
        resp.raise_for_status()
        data = resp.json()

        candles = []
        for c in data.get("candles", []):
            # ⚠️ 只取 complete=True 的 K 线（防 look-ahead bias）
            if c["complete"]:
                mid = c["mid"]
                candles.append({
                    "time": c["time"],
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": int(c["volume"]),
                })
        return candles

    def save_candles(self, candles: List[dict], granularity: str,
                     symbol: Optional[str] = None) -> Path:
        """保存 K 线到 Parquet（增量去重）"""
        sym = symbol or self.symbol
        tf_dir = self.data_dir / granularity
        tf_dir.mkdir(parents=True, exist_ok=True)
        filepath = tf_dir / f"{sym}_{granularity}.parquet"

        new_df = pd.DataFrame(candles)
        if new_df.empty:
            return filepath

        new_df["time"] = pd.to_datetime(new_df["time"])
        new_df.set_index("time", inplace=True)
        # ⚠️ 时区统一 UTC
        if new_df.index.tz is None:
            new_df.index = new_df.index.tz_localize("UTC")
        else:
            new_df.index = new_df.index.tz_convert("UTC")

        if filepath.exists():
            old_df = pd.read_parquet(filepath)
            combined = pd.concat([old_df, new_df])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined.sort_index(inplace=True)
            combined.to_parquet(filepath)
        else:
            new_df.to_parquet(filepath)

        logger.info(f"✅ 已保存 {len(candles)} 条 {granularity} -> {filepath}")
        return filepath

    def update_all_timeframes(self, count: int = 200) -> dict:
        """更新所有配置的时间周期"""
        results = {}
        for tf in self.cfg["collection"]["timeframes"]:
            try:
                candles = self.fetch_candles(granularity=tf, count=count)
                path = self.save_candles(candles, tf)
                results[tf] = {"count": len(candles), "path": str(path)}
            except Exception as e:
                logger.error(f"❌ 采集 {tf} 失败: {e}")
                results[tf] = {"error": str(e)}
        return results

    def load_parquet(self, granularity: str, symbol: Optional[str] = None) -> pd.DataFrame:
        """从本地 Parquet 加载数据（含完整性校验）"""
        sym = symbol or self.symbol
        filepath = self.data_dir / granularity / f"{sym}_{granularity}.parquet"
        if not filepath.exists():
            candles = self.fetch_candles(granularity=granularity, count=500, symbol=sym)
            if not candles:
                raise FileNotFoundError(f"无法获取 {granularity} 数据")
            df = pd.DataFrame(candles)
            # 与 save_candles 一致: time 列设为 UTC 索引(否则调用方 dt.hour 崩溃)
            df["time"] = pd.to_datetime(df["time"])
            df.set_index("time", inplace=True)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
        else:
            df = pd.read_parquet(filepath)

        # 数据完整性校验
        issues = self._validate_dataframe(df, granularity, filepath)
        if issues:
            logger.warning(
                f"⚠️ {granularity} 数据校验发现问题 ({filepath}): {'; '.join(issues)}"
            )
        return df

    @staticmethod
    def _validate_dataframe(
        df: pd.DataFrame, granularity: str, filepath: Path
    ) -> list:
        """校验 DataFrame 数据完整性，返回问题列表"""
        issues = []
        required_cols = {"open", "high", "low", "close", "volume"}
        missing = required_cols - set(df.columns)
        if missing:
            issues.append(f"缺失列: {missing}")

        if df.empty:
            issues.append("DataFrame 为空")
            return issues

        # NaN 检查
        nan_cols = [c for c in required_cols & set(df.columns) if df[c].isna().any()]
        if nan_cols:
            nan_count = df[nan_cols].isna().sum().sum()
            issues.append(f"{nan_count} 个 NaN 值 (列: {nan_cols})")

        # 过期检查：最后一条数据是否太旧
        if isinstance(df.index, pd.DatetimeIndex) and len(df) > 0:
            from datetime import datetime, timezone, timedelta
            last_ts = df.index.max()
            now = pd.Timestamp.now(tz=last_ts.tz if last_ts.tz else "UTC")
            max_age = {
                "M5": timedelta(hours=2),
                "M15": timedelta(hours=4),
                "H1": timedelta(hours=8),
                "H4": timedelta(hours=16),
                # D1 放宽到 4 天：覆盖周末休市（周五收盘 → 周一早上 ≈ 80h）
                "D1": timedelta(days=4),
            }.get(granularity, timedelta(days=7))
            if now - last_ts > max_age:
                age_h = round((now - last_ts).total_seconds() / 3600, 1)
                issues.append(f"数据过期 ({age_h}h 前，阈值 {max_age})")

        # 价格异常检查 (EUR/USD 合理范围 0.80~1.80)
        if "close" in df.columns:
            close = df["close"]
            anomalous = close[(close < 0.80) | (close > 1.80)]
            if len(anomalous) > 0:
                issues.append(f"{len(anomalous)} 条异常收盘价 (范围外: {anomalous.min():.4f}~{anomalous.max():.4f})")

        # OHLC 逻辑检查: high >= max(open, close), low <= min(open, close)
        if all(c in df.columns for c in ("high", "low", "open", "close")):
            bad_high = (df["high"] < df[["open", "close"]].max(axis=1)).sum()
            bad_low = (df["low"] > df[["open", "close"]].min(axis=1)).sum()
            if bad_high > 0:
                issues.append(f"{bad_high} 条 high < max(open,close)")
            if bad_low > 0:
                issues.append(f"{bad_low} 条 low > min(open,close)")

        return issues
