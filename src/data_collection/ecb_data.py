"""ECB Data Portal API — 欧元区经济数据

注意: ECB 有两个 API 端点:
  data-api.ecb.europa.eu  → 部分数据可用（如汇率）
  sdw-wsrest.ecb.europa.eu → 全量数据（部分网络不可达）

当前可用: EUR/USD 汇率数据
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from src.utils.config_loader import config

logger = logging.getLogger(__name__)


class ECBData:
    """ECB 官方数据接口"""

    BASE_URL = "https://data-api.ecb.europa.eu/service/data"

    def __init__(self):
        self.client = config.get_http_client()
        self.data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "calendar"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def fetch_exchange_rate(self) -> Optional[pd.DataFrame]:
        """获取 EUR/USD 参考汇率（日线）

        EXR = Exchange Rates
        D = Daily
        USD.EUR = USD per EUR
        SP00.A = Spot rate
        """
        url = f"{self.BASE_URL}/EXR/D.USD.EUR.SP00.A"
        headers = {"Accept": "application/json"}
        params = {"startPeriod": "2024-01-01"}

        # 网络出口: 默认通道 → 专用代理(系统备用出口, 中控台 mode 决定)
        import httpx
        from src.utils.network import get_proxy_candidates
        data = None
        last_err = None
        for proxy in get_proxy_candidates():
            try:
                client = (httpx.Client(proxy=proxy, timeout=30) if proxy
                          else self.client)
                resp = client.get(url, headers=headers, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                last_err = None
                break
            except Exception as e:
                last_err = e
                if proxy:
                    logger.info(f"ECB 默认通道失败, 走专用代理重试: {str(e)[:50]}")
                continue
        if last_err is not None:
            logger.error(f"ECB 汇率采集失败: {last_err}")
            return None

        try:
            # 获取 series key（格式如 "0:0:0:0:0"）
            series_dict = data.get("dataSets", [{}])[0].get("series", {})
            if not series_dict:
                raise ValueError("无 series 数据")
            series_key = list(series_dict.keys())[0]
            observations = series_dict[series_key].get("observations", {})
            structure = data.get("structure", {}).get("dimensions", {}).get("observation", [{}])[0]
            time_values = structure.get("values", [])

            rows = []
            for period_idx, value_list in observations.items():
                period_name = time_values[int(period_idx)]["name"]
                value = value_list[0]
                if value is not None:
                    rows.append({"time": period_name, "value": float(value)})

            if rows:
                df = pd.DataFrame(rows)
                df["time"] = pd.to_datetime(df["time"])
                df.sort_values("time", inplace=True)
                logger.info(f"ECB 汇率: {len(df)} 条, 最新={df['value'].iloc[-1]:.5f}")
                return df
        except Exception as e:
            logger.error(f"ECB 汇率解析失败: {e}")

        return None

    def collect_and_save(self) -> Dict[str, Optional[Path]]:
        """采集并保存所有数据"""
        results = {}
        df = self.fetch_exchange_rate()
        if df is not None:
            path = self.data_dir / "ecb_eur_usd.parquet"
            df.to_parquet(path)
            results["exchange_rate"] = path
            logger.info(f"ECB 汇率已保存: {path}")
        else:
            results["exchange_rate"] = None
        return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ecb = ECBData()
    ecb.collect_and_save()
