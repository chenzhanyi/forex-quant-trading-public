"""资讯新闻采集 — RSS Feeds + EUR/USD 关键词过滤

支持代理（config.yaml 中 network.enabled 控制）
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

import feedparser
import pandas as pd

from src.utils.config_loader import config

logger = logging.getLogger(__name__)

# EUR/USD 相关关键词（覆盖利率、经济数据、央行、地缘、贸易）
EURUSD_KEYWORDS = [
    # 货币对 / 央行
    "EUR/USD", "euro", "dollar", "ECB", "FOMC", "Fed",
    "eurozone", "欧元", "美元", "欧洲央行", "美联储",
    "Bundesbank", "Lagarde", "Powell", "拉加德", "鲍威尔", "central bank",
    # 利率政策
    "interest rate", "利率", "rate hike", "加息", "rate cut", "降息",
    "tightening", "easing", "monetary policy", "货币政策",
    # 经济数据（主要影响 EUR/USD 的）
    "Non-Farm", "NFP", "非农", "CPI", "inflation", "通胀",
    "HICP", "PPI", "GDP", "PMI", "retail sales", "零售",
    "unemployment", "失业", "jobless claims", "初请",
    "consumer confidence", "消费者信心",
    # 德国（欧元区核心）
    "German", "德国", "ZEW", "Ifo",
    # 贸易 / 地缘
    "tariff", "关税", "trade war", "贸易战", "trade balance",
    "Middle East", "中东", "oil price", "油价", "crude",
    "geopolitical", "地缘",
]

# 黄金相关关键词（扩展: 基本面同时覆盖三品种）
GOLD_KEYWORDS = [
    "gold", "XAU", "bullion", "gold price", "precious metal",
    "黄金", "金价", "贵金属",
]

# 澳元相关关键词
AUD_KEYWORDS = [
    "AUD/USD", "AUD", "Australian dollar", "RBA", "Australia",
    "澳元", "澳洲", "澳联储",
]

# 外汇 RSS Feed 源（按可用性排序）
FOREX_RSS_FEEDS = [
    # 外汇专项（需代理，但覆盖最精准）
    "https://www.investing.com/rss/news_301.rss",              # ✅ forex news
    "https://www.forexlive.com/feed/news",                     # ✅ 知名外汇资讯
    "https://www.dailyfx.com/feeds/rss/forex-news",            # ⚠️ 可能被墙
    "https://www.fxstreet.com/rss/forex-news",                 # ⚠️ 可能被墙
    # 综合财经（通常不需代理，补充宏观视角）
    "https://feeds.marketwatch.com/marketwatch/topstories",    # ✅ 通用财经
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",   # ✅ CNBC 全球市场
]


class _NoProxyContext:
    """空上下文管理器 — 无需代理时使用"""
    def __enter__(self): return self
    def __exit__(self, *args): pass


class _ProxyContext:
    """上下文管理器：临时设置/恢复代理环境变量

    feedparser 底层用 urllib，只能通过环境变量走代理。
    在 with 块内设置代理，退出时恢复原值，不污染其他模块。
    """
    def __init__(self, proxy: str):
        import os
        self._os = os
        self._proxy = proxy
        self._saved_http = os.environ.get("http_proxy")
        self._saved_https = os.environ.get("https_proxy")

    def __enter__(self):
        import os
        os.environ["http_proxy"] = self._proxy
        os.environ["https_proxy"] = self._proxy
        return self

    def __exit__(self, *args):
        for key, saved in [("http_proxy", self._saved_http),
                           ("https_proxy", self._saved_https)]:
            if saved is None:
                self._os.environ.pop(key, None)
            else:
                self._os.environ[key] = saved


class NewsCollector:
    """外汇资讯采集器 — RSS + 关键词过滤"""

    def __init__(self):
        self.cfg = config.load()
        self.data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "news"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        feedparser.USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

    def _get_proxy(self) -> Optional[str]:
        """获取代理 URL（仅当网络代理开启时）"""
        net = self.cfg.get("network", {})
        if net.get("enabled"):
            return net.get("http", "")
        return None

    def _proxy_candidates(self) -> List[Optional[str]]:
        """代理候选序列: 默认通道(Clash/直连) → 专用代理(中控台 mode 决定)"""
        from src.utils.network import get_proxy_candidates
        return get_proxy_candidates(default_proxy=self._get_proxy())

    def fetch_feeds(self, feed_urls: Optional[List[str]] = None) -> List[Dict]:
        """采集 RSS Feed 中的 EUR/USD 相关文章

        Args:
            feed_urls: RSS 源列表，默认使用 FOREX_RSS_FEEDS

        Returns:
            [{"title", "link", "published", "summary", "source", "collected_at"}, ...]
        """
        if feed_urls is None:
            feed_urls = FOREX_RSS_FEEDS

        articles = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for url in feed_urls:
            # 解析失败时按候选序列切换代理重试(默认通道 → 专用代理)
            for proxy in self._proxy_candidates():
                ctx = _ProxyContext(proxy) if proxy else _NoProxyContext()
                try:
                    with ctx:
                        feed = feedparser.parse(url)
                except Exception as e:
                    logger.warning(f"RSS 源 {url} 采集失败: {str(e)[:60]}")
                    continue  # 换代理重试
                if feed.bozo and not feed.entries:
                    logger.warning(f"RSS 源解析失败{'(专用代理)' if proxy else ''}: {url}")
                    continue  # 换代理重试

                for entry in feed.entries:
                    title = entry.get("title", "")
                    summary = entry.get("summary", "") or entry.get("description", "")
                    published = entry.get("published", "")
                    text = f"{title} {summary}"

                    if self._is_relevant(text):
                        articles.append({
                            "title": title.strip(),
                            "link": entry.get("link", ""),
                            "published": published,
                            "summary": summary.strip()[:500],
                            "source": url,
                            "collected_at": now_iso,
                            "tags": ",".join(self._match_tags(text)),
                        })
                logger.info(f"✅ RSS {url}: {len([a for a in articles if a['source']==url])} 条相关")
                break  # 成功, 不再换代理

        return articles

    def save(self, articles: List[Dict]) -> Path:
        """保存文章到 CSV"""
        today = datetime.now().strftime("%Y%m%d")
        filepath = self.data_dir / f"news_{today}.csv"

        df = pd.DataFrame(articles)
        if not df.empty:
            # 去重
            df.drop_duplicates(subset=["title"], inplace=True)
            df.to_csv(filepath, index=False, encoding="utf-8")
            logger.info(f"✅ 新闻已保存: {filepath} ({len(df)} 条)")
        else:
            # 空文件也保存（标记已运行）
            df.to_csv(filepath, index=False, encoding="utf-8")
            logger.info(f"📭 新闻为空: {filepath}")

        return filepath

    def collect_and_save(self) -> Path:
        """一键采集并保存"""
        articles = self.fetch_feeds()
        return self.save(articles)

    @staticmethod
    def _is_relevant(text: str) -> bool:
        """检查文本是否与 EUR/USD 相关"""
        text_lower = text.lower()
        return any(kw.lower() in text_lower for kw in EURUSD_KEYWORDS)

    @staticmethod
    def _match_tags(text: str) -> list:
        """标记文章涉及哪些品种: eur/gold/aud(可多标签)"""
        text_lower = text.lower()
        tags = []
        if any(kw.lower() in text_lower for kw in EURUSD_KEYWORDS):
            tags.append("eur")
        if any(kw.lower() in text_lower for kw in GOLD_KEYWORDS):
            tags.append("gold")
        if any(kw.lower() in text_lower for kw in AUD_KEYWORDS):
            tags.append("aud")
        return tags


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    nc = NewsCollector()
    articles = nc.fetch_feeds()
    print(f"找到 {len(articles)} 条相关文章:")
    for a in articles[:5]:
        print(f"  • {a['title']}")
    nc.save(articles)
