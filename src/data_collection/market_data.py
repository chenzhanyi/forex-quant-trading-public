"""品种行情刷新 — OANDA 主源 + Twelve Data 备用（黄金/澳元/欧元通用）

每个品种引擎在评估前调用 refresh_symbol_data, 保证本地 parquet 数据新鲜:
  - OANDA 拉取(200根) → 保存
  - OANDA 失败 或 数据停更(周末除外) → Twelve Data 补缺口(只补本地缺失时间戳)
  - 专用代理: always=全程代理; auto=直连失败后代理重试; off=不用
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

TFS = ("M15", "H1", "H4")


def _market_closed() -> bool:
    """外汇市场休市(周末) — 休市期数据必然"过期", 不是数据源故障, 不触发备用源"""
    now = datetime.now(timezone.utc)
    wd, h = now.weekday(), now.hour
    return wd == 5 or (wd == 4 and h >= 21) or (wd == 6 and h < 21)


def _is_data_fresh(df, tf: str) -> bool:
    """本地 parquet 最后K线是否在新鲜度阈值内"""
    if df is None or df.empty:
        return False
    last_ts = df.index.max()
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize("UTC")
    age = (datetime.now(timezone.utc) - last_ts).total_seconds()
    max_age = {"M15": 4 * 3600, "H1": 8 * 3600, "H4": 16 * 3600}.get(tf, 16 * 3600)
    return age <= max_age


def _proxy_candidates() -> list:
    """OANDA 代理尝试序列: (use_proxy, 描述)"""
    from src.utils.network import get_dedicated_proxy_url, get_proxy_settings
    proxy_url = get_dedicated_proxy_url()
    mode = get_proxy_settings().get("mode", "off")
    if proxy_url and mode == "always":
        return [(True, "专用代理")]
    if proxy_url and mode == "auto":
        return [(False, "默认通道"), (True, "专用代理")]
    return [(False, "默认通道")]


def refresh_symbol_data(symbol_oanda: str, tfs=TFS) -> dict:
    """刷新单个品种的各周期数据 — OANDA 主源 + TwelveData 备用

    Args:
        symbol_oanda: OANDA 品种格式, 如 XAU_USD / AUD_USD / EUR_USD
    Returns:
        {tf: {"source": "oanda"/"twelvedata"/"skip", "fresh": bool}}
    """
    from src.data_collection.oanda import OandaClient
    from src.data_collection.twelvedata import TwelveDataClient

    display = symbol_oanda.replace("_", "/")  # XAU_USD → XAU/USD
    result = {}
    for tf in tfs:
        ok = False
        # ── OANDA 主源(按中控台代理模式尝试) ──
        for use_proxy, tag in _proxy_candidates():
            try:
                o = OandaClient(use_proxy=use_proxy)
                d = o.fetch_candles(tf, 200, symbol=symbol_oanda)
                if d:
                    o.save_candles(d, tf, symbol=symbol_oanda)
                    ok = True
                    break
            except Exception as e:
                logger.debug(f"{display} {tf} OANDA({tag})失败: {str(e)[:60]}")
        if ok:
            # 新鲜度检查: OANDA 正常返回但数据停更 → 备用源补齐(周末跳过)
            fresh = True
            if not _market_closed():
                try:
                    df = OandaClient().load_parquet(tf, symbol=symbol_oanda)
                    if not _is_data_fresh(df, tf):
                        _fill_from_backup(tf, symbol_oanda, display, o)
                        fresh = False
                except Exception:
                    pass
            result[tf] = {"source": "oanda", "fresh": fresh}
            continue

        # ── OANDA 全失败 → TwelveData 接管 ──
        filled = _fill_from_backup(tf, symbol_oanda, display, None)
        result[tf] = {"source": "twelvedata" if filled else "none", "fresh": filled}
    return result


def _fill_from_backup(tf: str, symbol_oanda: str, display: str, oanda_client=None) -> bool:
    """Twelve Data 补缺口(只补本地缺失的时间戳, 不覆盖 OANDA 已有K线)"""
    try:
        import pandas as pd
        from src.data_collection.twelvedata import TwelveDataClient
        from src.data_collection.oanda import OandaClient
        td = TwelveDataClient()  # 自动模式: 直连失败自动走专用代理
        if not td.is_configured():
            return False
        d = td.fetch_candles(tf, 200, symbol=display)
        if not d:
            return False
        o = oanda_client or OandaClient()
        try:
            existing = o.load_parquet(tf, symbol=symbol_oanda)
            existing_idx = set(pd.to_datetime(existing.index).tz_localize(None))
        except Exception:
            existing_idx = set()
        new_only = [c for c in d
                    if pd.Timestamp(c["time"]).tz_localize(None) not in existing_idx]
        if not new_only:
            logger.debug(f"{display} {tf}: TwelveData 无缺口需补")
            return True
        o.save_candles(new_only, tf, symbol=symbol_oanda)
        logger.info(
            f"📡 TwelveData 备用源补充 {display} {tf}: 缺口 {len(new_only)} 根 "
            f"(最新 {new_only[-1]['time'][:16]} UTC)"
        )
        return True
    except Exception as e:
        logger.warning(f"📡 TwelveData 备用源 {display} {tf} 补充失败: {str(e)[:60]}")
        return False
