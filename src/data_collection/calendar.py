"""经济日历 — ForexFactory 数据采集

优先: JSON API (nfs.faireconomy.media)
降级: HTML 解析 (forexfactory.com/calendar)
"""
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# 影响 EUR/USD 的高重要性事件关键词
HIGH_IMPACT_EVENTS = [
    "Non-Farm", "CPI", "GDP", "Interest Rate", "FOMC",
    "ECB", "Unemployment", "NFP", "PPI", "Retail Sales",
    "PMI", "Industrial Production", "Inflation", "Housing",
]

# 重大事件关键词（可能长期影响趋势，不仅短暂波动）
MAJOR_EVENT_KEYWORDS = [
    # 央行利率决议（最重要的驱动因素）
    "Interest Rate", "利率", "Rate Decision", "利率决议",
    "FOMC", "Federal Funds", "联邦基金",
    "ECB", "ECB Press Conference", "Monetary Policy",
    # 就业（NFP 每月最重要数据）
    "Non-Farm", "NFP", "非农", "Employment Change",
    "Unemployment", "失业率", "Jobless Claims",
    # 通胀
    "CPI", "Inflation", "通胀", "PPI", "PCE",
    "HICP", "消费者物价",
    # 经济增长
    "GDP", "国内生产总值", "Economic Growth",
    "Retail Sales", "零售销售",
    "PMI", "Manufacturing PMI", "Services PMI",
    "Industrial Production", "工业产出",
    # 央行讲话 / 会议纪要
    "Speech", "讲话", "Minutes", "会议纪要", "Testimony",
    # 消费者 / 信心
    "Consumer Confidence", "Sentiment",
    # 贸易
    "Trade Balance", "贸易帐", "Tariff",
]


class EconomicCalendar:
    """经济日历采集器"""

    JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    HTML_URL = "https://www.forexfactory.com/calendar?week=this"

    def __init__(self):
        self.data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "calendar"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _get_scraper(self, proxy: Optional[str] = None):
        """获取 cloudscraper 实例（绕过 Cloudflare）; proxy 为 None 时走默认通道"""
        import cloudscraper
        scraper = cloudscraper.create_scraper()
        if proxy:
            scraper.proxies = {"http": proxy, "https": proxy}
        return scraper

    def fetch(self) -> List[Dict]:
        """获取经济日历，JSON 优先，HTML 降级"""
        events = self._fetch_json()
        if events:
            return events
        logger.info("JSON API 不可用，降级到 HTML 解析")
        return self._fetch_html()

    def _fetch_json(self) -> List[Dict]:
        """从 JSON API 获取（带重试 + 指数退避 + 缓存去重 + 代理自动切换）

        网络层失败时按中控台模式自动切换专用代理重试(auto: 默认→代理)。
        """
        from src.utils.network import get_proxy_candidates

        # 1 小时内重复调用直接跳过，避免 429
        cache_marker = self.data_dir / ".json_last_fetch"
        if cache_marker.exists():
            try:
                last_ts = float(cache_marker.read_text().strip())
                if time.time() - last_ts < 3600:
                    logger.debug("日历 JSON API: 1h 内已请求，跳过")
                    return []
            except (ValueError, OSError):
                pass

        max_retries = 3
        # 外层: 代理候选(默认通道 → 专用代理); 内层: HTTP 重试/429 换 session
        candidates = get_proxy_candidates()
        for proxy in candidates:
            try:
                scraper = self._get_scraper(proxy)
            except ImportError:
                logger.warning("cloudscraper 未安装, 日历 JSON 跳过 (pip3 install cloudscraper)")
                return []
            for attempt in range(max_retries):
                try:
                    r = scraper.get(self.JSON_URL, timeout=30)
                    if r.status_code == 200:
                        raw = r.json()
                        events = []
                        for item in raw:
                            events.append({
                                "date": item.get("date", ""),
                                "time": item.get("time", ""),
                                "currency": item.get("currency", ""),
                                "event": item.get("event", ""),
                                "impact": str(item.get("impact", "")),
                                "previous": item.get("previous", ""),
                                "forecast": item.get("forecast", ""),
                                "actual": item.get("actual", ""),
                            })
                        cache_marker.write_text(str(time.time()))
                        logger.info(f"日历(JSON): {len(events)} 条")
                        return events
                    elif r.status_code == 429:
                        wait = 2 ** attempt  # 1s / 2s / 4s
                        logger.warning(f"日历(JSON): HTTP 429 (attempt {attempt + 1}/{max_retries}), {wait}s 后重试")
                        time.sleep(wait)
                        scraper = self._get_scraper(proxy)  # 换 session
                    else:
                        logger.warning(f"日历(JSON): HTTP {r.status_code}")
                        break  # 非 429 不重试
                except Exception as e:
                    logger.warning(f"日历(JSON){'[专用代理]' if proxy else ''}: {str(e)[:60]}")
                    if proxy is None and len(candidates) > 1:
                        break  # 默认通道网络层失败 → 换专用代理
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)

        # 请求失败也写入标记，避免 1h 内重复撞 429
        cache_marker.write_text(str(time.time()))
        return []

    def _fetch_html(self) -> List[Dict]:
        """从 HTML 页面解析（默认通道失败自动切专用代理）"""
        from src.utils.network import get_proxy_candidates

        candidates = get_proxy_candidates()
        for proxy in candidates:
            try:
                scraper = self._get_scraper(proxy)
            except ImportError:
                logger.warning("cloudscraper 未安装, 日历 HTML 跳过")
                return []
            try:
                r = scraper.get(self.HTML_URL, timeout=30)
                r.raise_for_status()
                r.encoding = "ISO-8859-1"
                break
            except Exception as e:
                logger.error(f"日历(HTML){'[专用代理]' if proxy else ''}: {str(e)[:60]}")
                if proxy is not None or len(candidates) == 1:
                    return []
                continue  # 换专用代理重试

        try:
            soup = BeautifulSoup(r.text, "lxml")
        except Exception:
            soup = BeautifulSoup(r.text, "html.parser")

        events = []
        current_date = ""

        # ForexFactory HTML 结构
        for row in soup.select("tr.calendar__row"):
            classes = row.get("class", [])

            # 日期行
            date_cell = row.select_one("td.calendar__date")
            if date_cell:
                current_date = date_cell.get_text(strip=True)
                continue

            # 事件行
            currency_cell = row.select_one("td.calendar__currency")
            event_cell = row.select_one("td.calendar__event")
            if not currency_cell or not event_cell:
                continue

            event = {
                "date": current_date,
                "time": "",
                "currency": currency_cell.get_text(strip=True).upper(),
                "event": event_cell.get_text(strip=True),
                "impact": self._parse_impact(row),
                "previous": self._cell_text(row, "previous"),
                "forecast": self._cell_text(row, "forecast"),
                "actual": self._cell_text(row, "actual"),
            }
            events.append(event)

        logger.info(f"日历(HTML): {len(events)} 条")
        return events

    def get_eur_usd_high_impact(self, events: Optional[List[Dict]] = None) -> List[Dict]:
        """过滤出影响 EUR/USD 的事件"""
        if events is None:
            events = self.fetch()
        return [e for e in events if e.get("currency", "").upper() in ("EUR", "USD")]

    def _parse_event_datetime(self, evt: Dict) -> Optional[datetime]:
        """解析事件的日期+时间为 UTC datetime（修复：合并 date 和 time 字段）"""
        date_str = evt.get("date", "")
        time_str = evt.get("time", "")
        if not date_str:
            return None

        parsed_date = None
        matched_fmt = None
        for fmt in ["%Y-%m-%d", "%b%d", "%m/%d/%Y"]:
            try:
                parsed_date = datetime.strptime(date_str, fmt)
                matched_fmt = fmt
                break
            except ValueError:
                continue

        if parsed_date is None:
            return None

        # %b%d 格式不含年份，strptime 默认 1900 年，修正为当前年份
        if matched_fmt == "%b%d":
            parsed_date = parsed_date.replace(year=datetime.now().year)

        # 解析时间（如 "14:30" 或 "T14:30:00" 或 "All Day"）
        hour, minute = 0, 0
        if time_str and ":" in time_str:
            try:
                parts = time_str.replace("T", "").split(":")
                hour, minute = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                pass

        return parsed_date.replace(
            hour=hour, minute=minute, second=0, tzinfo=timezone.utc
        )

    def has_major_event_soon(self, hours_ahead: int = 24) -> Dict:
        """检查未来 N 小时内是否有重大事件（利率决议/FOMC/非农等）

        重大事件可能长期影响趋势，不仅仅是短暂波动。

        Returns:
            {"has_event": bool, "events": [str]} 或 {"has_event": False, "events": []}
        """
        events = self.get_eur_usd_high_impact()
        now = datetime.now(timezone.utc)
        major = []

        for evt in events:
            # 只检查高影响事件
            if evt.get("impact", "").lower() != "high":
                continue
            # 检查事件名是否含重大关键词
            evt_name = evt.get("event", "")
            if not any(
                kw.lower() in evt_name.lower() for kw in MAJOR_EVENT_KEYWORDS
            ):
                continue
            # 检查时间窗口
            evt_dt = self._parse_event_datetime(evt)
            if evt_dt is None:
                continue
            delta = (evt_dt - now).total_seconds()
            if 0 <= delta <= hours_ahead * 3600:
                major.append(
                    f"{evt.get('currency','')} {evt_name} @ {evt.get('date','')} {evt.get('time','')}"
                )

        return {"has_event": len(major) > 0, "events": major}

    def save(self, events: List[Dict]) -> Path:
        """保存到 JSON"""
        today = datetime.now().strftime("%Y%m%d")
        filepath = self.data_dir / f"calendar_{today}.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
        logger.info(f"✅ 日历已保存: {filepath} ({len(events)} 条)")
        return filepath

    def collect_and_save(self):
        """采集并保存，自动降级"""
        events = self.fetch()
        if not events:
            try:
                import akshare as ak
                df = ak.news_economic_calendar()
                if df is not None and not df.empty:
                    events = []
                    for _, r in df.iterrows():
                        events.append({
                            "date": str(r.get("date", ""))[:10],
                            "time": str(r.get("time", "")),
                            "currency": str(r.get("country", "")).upper()[:3],
                            "event": str(r.get("event", "")),
                            "impact": str(r.get("impact", "")),
                        })
                    logger.info(f"日历(akshare): {len(events)} 条")
            except Exception as e:
                logger.warning(f"日历(akshare): {e}")

        # 只保存有有效货币数据的文件（防 HTML 降级覆盖好缓存）
        if events and any(e.get("currency", "") for e in events):
            self.save(events)
            eur = self.get_eur_usd_high_impact(events)
            high = [e for e in eur if e.get("impact", "").lower() == "high"]
            if high:
                logger.info(f"⚠️ EUR/USD 高影响事件: {len(high)} 个")
                for e in high:
                    logger.info(f"   {e['date']} {e['currency']} {e['event']} [{e['impact']}]")
        return self.data_dir / f"calendar_{datetime.now().strftime('%Y%m%d')}.json"

    @staticmethod
    def _parse_impact(row) -> str:
        """解析事件重要性（从 CSS class 判断）"""
        imp_td = row.select_one("td.calendar__impact")
        if not imp_td:
            return "Unknown"
        # ForexFactory 用 icon class 表示: red=高, ora=中, yel=低
        span = imp_td.select_one("span[class*='impact']")
        if span:
            cls = " ".join(span.get("class", []))
            if "red" in cls:
                return "High"
            elif "ora" in cls:
                return "Medium"
            elif "yel" in cls:
                return "Low"
        # 降级: 从 img alt 判断
        img = imp_td.select_one("img")
        if img:
            alt = img.get("alt", "").lower()
            if "high" in alt:
                return "High"
            elif "medium" in alt:
                return "Medium"
            elif "low" in alt:
                return "Low"
        return "Unknown"

    @staticmethod
    def _cell_text(row, field: str) -> str:
        cell = row.select_one(f"td.calendar__{field}")
        return cell.get_text(strip=True) if cell else ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cal = EconomicCalendar()
    cal.collect_and_save()
