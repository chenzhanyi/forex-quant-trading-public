"""Twelve Data 行情数据源 — OANDA 备用（免费 800 次/天, 8 次/分钟）

接口与 OandaClient.fetch_candles 对齐, 返回相同结构的 candle dict 列表,
可直接用 OandaClient.save_candles 合并进同一 parquet 文件。

免费额度注意:
  - 8 次/分钟 → 每次调用间 sleep 1 秒
  - 800 次/天  → 备用模式(仅在 OANDA 数据过期时调用)足够
  - 时区: 必须传 timezone=UTC, 与本地 parquet(UTC) 对齐
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx

from src.utils.config_loader import config

logger = logging.getLogger(__name__)

# 本地周期 → Twelve Data interval
INTERVAL_MAP = {
    "M5": "5min",
    "M15": "15min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1day",
    "D": "1day",
}

# 调用间最小间隔(秒) — 免费版 8 次/分钟限制
MIN_INTERVAL_SEC = 1.0


class TwelveDataClient:
    """Twelve Data REST 客户端(备用数据源)

    Args:
        use_proxy: None=按中控台模式自动(off→直连; always→代理;
                   auto→先直连, 失败再用代理重试一次)
                   True/False 强制走/不走专用代理。
    专用代理地址复用 oanda.get_dedicated_proxy_url()(同一份中控台配置)。
    """

    def __init__(self, use_proxy: Optional[bool] = None):
        cfg = config.load()
        self.api_key = cfg.get("twelvedata", {}).get("api_key", "")
        if not self.api_key or "YOUR_" in self.api_key:
            self.api_key = ""
        self.base_url = "https://api.twelvedata.com"
        self._last_call = 0.0
        self._configured = bool(self.api_key)
        self._use_proxy = use_proxy

    def _proxy_candidates(self) -> List[Optional[str]]:
        """按中控台配置给出要尝试的代理列表(公共决策函数):
        [None]=仅直连; [None, url]=先直连失败再代理(auto); [url]=仅代理"""
        from src.utils.network import get_proxy_candidates, get_dedicated_proxy_url

        proxy_url = get_dedicated_proxy_url()
        if self._use_proxy is True:
            return [proxy_url] if proxy_url else [None]
        if self._use_proxy is False:
            return [None]
        # 自动: 公共候选序列(默认通道=None → 失败再专用代理)
        return get_proxy_candidates(default_proxy=None)

    def _throttle(self) -> None:
        """免费版 8 次/分钟 — 强制调用间隔"""
        elapsed = time.time() - self._last_call
        if elapsed < MIN_INTERVAL_SEC:
            time.sleep(MIN_INTERVAL_SEC - elapsed)
        self._last_call = time.time()

    def fetch_candles(
        self,
        granularity: str = "H1",
        count: int = 200,
        symbol: Optional[str] = None,
    ) -> List[dict]:
        """获取 K 线 — 返回与 OandaClient.fetch_candles 相同格式

        Returns:
            [{"time": "2026-08-29T12:45:00+00:00", "open": float,
              "high": float, "low": float, "close": float, "volume": int}]
        """
        if not self._configured:
            raise ConnectionError("Twelve Data API Key 未配置 (config.local.yaml → twelvedata.api_key)")

        sym = symbol or config.load()["project"]["symbol"]  # 如 EUR/USD
        interval = INTERVAL_MAP.get(granularity)
        if interval is None:
            raise ValueError(f"不支持的周期: {granularity}")

        params = {
            "symbol": sym,
            "interval": interval,
            "outputsize": min(max(count, 1), 5000),
            "apikey": self.api_key,
            "timezone": "UTC",
        }
        # 按模式依次尝试: 直连/专用代理(失败自动切换, 保证备用源可用性)
        last_err = None
        for proxy in self._proxy_candidates():
            try:
                self._throttle()
                resp = httpx.get(
                    f"{self.base_url}/time_series",
                    params=params,
                    timeout=30,
                    proxy=proxy,
                )
                data = resp.json()
                last_err = None
                break
            except Exception as e:
                last_err = e
                if proxy:
                    logger.info(f"🌐 TwelveData 直连失败, 改走专用代理重试: {str(e)[:50]}")
                continue
        if last_err is not None:
            raise last_err
        if data.get("status") != "ok":
            raise ConnectionError(
                f"Twelve Data 错误: {data.get('code')} {data.get('message')}"
            )

        now = datetime.now(timezone.utc)
        interval_min = {"5min": 5, "15min": 15, "1h": 60, "4h": 240, "1day": 1440}[interval]

        candles = []
        for v in data.get("values", []):
            try:
                ts = datetime.fromisoformat(v["datetime"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if not ts.tzinfo:
                ts = ts.replace(tzinfo=timezone.utc)
            # 周末过滤(仅日内周期): 外汇周五21:00 UTC收市/周日21:00开市,
            # Twelve Data 周末仍推平盘假K线 — 必须剔除, 否则污染本地数据
            if interval != "1day":
                wd, h = ts.weekday(), ts.hour
                if wd == 5 or (wd == 4 and h >= 21) or (wd == 6 and h < 21):
                    continue
            # 丢弃未收盘的最后一根(与 OANDA complete=True 行为一致)
            if ts + timedelta(minutes=interval_min) > now:
                continue
            candles.append({
                "time": ts.isoformat(),
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
                "volume": 0,  # 外汇无成交量字段, 填 0 保持列结构
            })

        # 倒序 → 正序
        candles.reverse()
        return candles

    def is_configured(self) -> bool:
        return self._configured
