"""基本面分析 — 中长线新闻过滤+存储

把采集回来的新闻按影响周期分类:
  🔴 长期 (weeks~months): 利率决议、CPI趋势、贸易政策、地缘政治
  🟡 中期 (days~weeks):   经济数据、就业报告、央行讲话
  🟢 短期 (hours~days):   日常波动、技术分析（过滤掉）

保存格式: data/fundamentals/YYYY-MM/
          ├── index.json           ← 月度索引
          ├── long_term.json       ← 长期影响事件
          └── medium_term.json     ← 中期影响事件
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# ── 长期影响关键词（利率、政策、战争） ──
LONG_TERM_KEYWORDS = [
    # 利率 / 货币政策
    r"interest rate", r"利率", r"rate decision", r"加息", r"降息",
    r"tighten", r"easing", r"量化宽松", r"QE", r"taper",
    r"联邦基金利率", r"基准利率",
    # 央行结构性变化
    r"ECB", r"Fed", r"美联储", r"欧洲央行", r"FOMC",
    r"货币政策", r"monetary policy",
    # 贸易 / 地缘政治
    r"tariff", r"关税", r"trade war", r"贸易战",
    r"sanction", r"制裁", r" geopolitical", r"地缘",
    r"conflict", r"冲突", r"war", r"战争",
    # 长期经济结构
    r"inflation trend", r"通胀趋势", r"deflation", r"通缩",
    r"recession", r"衰退", r"economic growth", r"经济增长",
    # 黄金长期 (央行购金/美元结构/避险)
    r"central bank gold", r"gold reserve", r"央行购金", r"购金",
    r"gold demand", r"de-dollarization", r"去美元化", r"safe haven", r"避险",
    # 澳元长期 (RBA政策/中国需求/大宗商品)
    r"RBA", r"澳联储", r"Reserve Bank of Australia", r"iron ore", r"铁矿石",
    r"commodity supercycle", r"大宗商品", r"China demand", r"中国需求",
    r"fiscal policy", r"财政政策",
    r"de-dollarization", r"去美元化",
    r"supply chain", r"供应链",
]

# ── 中期影响关键词（经济数据、就业、CPI） ──
MEDIUM_TERM_KEYWORDS = [
    # 通胀 / 就业
    r"CPI", r"通胀", r"inflation", r"消费者物价",
    r"PPI", r"生产者物价", r"PCE",
    r"Non-Farm", r"非农", r"NFP",
    r"unemployment", r"失业", r"就业",
    r"payroll", r"工资", r"wage",
    # GDP / 经济数据
    r"GDP", r"国内生产总值",
    r"retail sales", r"零售", r"consumer spending",
    r"industrial production", r"工业产出",
    r"PMI", r"采购经理人",
    # 央行官员讲话
    r"speech", r"讲话", r"remarks", r"发言",
    r"minutes", r"会议纪要", r"纪要",
    # 黄金中期 (金价驱动数据)
    r"gold price", r"金价", r"bullion", r"real yield", r"实际利率",
    r"ETF flows", r"gold holdings", r"黄金ETF",
    # 澳元中期 (澳洲数据)
    r"employment change", r"澳洲就业", r"AU CPI", r"澳洲通胀",
    r"trade balance", r"贸易帐", r"business confidence", r"NAB",
    r"testimony", r"听证",
    # 债务 / 信用
    r"debt", r"债务", r"deficit", r"赤字",
    r"credit", r"信用",
]

# ── 情绪关键词（看多 EUR/USD） ──
BULLISH_KEYWORDS = [
    r"hawkish", r"鹰派", r"加息", r"rate hike", r"tighten",
    r"强劲", r"strong", r"增长", r"growth", r"expansion",
    r"复苏", r"recovery", r"改善", r"improve", r"上升",
    r"乐观", r"optimistic", r"积极", r"positive",
    r"beat expectations", r"超预期", r"高于预期",
    r"raise forecast", r"上调",
    r"dovish ECB", r"欧洲央行偏鸽", r"ECB easing",
]

# ── 情绪关键词（看空 EUR/USD） ──
BEARISH_KEYWORDS = [
    r"dovish", r"鸽派", r"降息", r"rate cut", r"easing",
    r"疲软", r"weak", r"衰退", r"recession", r"contraction",
    r"下滑", r"decline", r"放缓", r"slowdown", r"下降",
    r"悲观", r"pessimistic", r"消极", r"negative",
    r"miss expectations", r"低于预期", r"不及预期",
    r"lower forecast", r"下调",
    r"hawkish ECB", r"欧洲央行偏鹰",
]

# ── 短期噪声关键词（过滤掉） ──
SHORT_TERM_FILTER = [
    r"technical analysis", r"技术分析",
    r"chart pattern", r"图表形态",
    r"support.*resistance", r"支撑.*阻力",
    r"scalp", r"day trading",
    r"pip", r"点",
    r"forex forecast", r"外汇预测",
    r"trading signal", r"交易信号",
]


class FundamentalsAnalyzer:
    """中长线基本面分析器"""

    def __init__(self):
        self.base_dir = Path(__file__).resolve().parent.parent.parent / "data" / "fundamentals"
        self.news_dir = Path(__file__).resolve().parent.parent.parent / "data" / "news"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def classify_article(self, title: str, summary: str) -> str:
        """分类文章的影响周期

        Returns:
            "long" | "medium" | "short" | "ignore"
        """
        text = f"{title} {summary}".lower()

        # 先检查是否短期噪声
        for pattern in SHORT_TERM_FILTER:
            if re.search(pattern, text, re.IGNORECASE):
                return "ignore"

        # 检查长期影响
        for pattern in LONG_TERM_KEYWORDS:
            if re.search(pattern, text, re.IGNORECASE):
                return "long"

        # 检查中期影响
        for pattern in MEDIUM_TERM_KEYWORDS:
            if re.search(pattern, text, re.IGNORECASE):
                return "medium"

        return "short"

    def process_news_file(self, csv_path: Path) -> Dict[str, List[Dict]]:
        """处理单日新闻文件，分类存储"""
        if not csv_path.exists():
            return {"long": [], "medium": [], "short": [], "ignore": []}

        try:
            df = pd.read_csv(csv_path)
        except Exception as e:
            logger.warning(f"读取新闻文件失败 {csv_path}: {e}")
            return {"long": [], "medium": [], "short": [], "ignore": []}

        result = {"long": [], "medium": [], "short": [], "ignore": []}

        for _, row in df.iterrows():
            title = str(row.get("title", ""))
            summary = str(row.get("summary", ""))
            category = self.classify_article(title, summary)

            entry = {
                "title": title,
                "summary": summary[:300],
                "source": str(row.get("source", "")),
                "published": str(row.get("published", "")),
                "collected_at": str(row.get("collected_at", "")),
                "category": category,
                "tags": str(row.get("tags", "")),  # eur/gold/aud, 逗号分隔
            }
            result[category].append(entry)

        return result

    def collect_and_classify(self) -> Dict:
        """采集最新新闻并分类"""
        # 先通过 news_collector 采集
        from src.data_collection.news_collector import NewsCollector
        nc = NewsCollector()
        articles = nc.fetch_feeds()
        nc.save(articles)

        # 读取刚刚保存的文件
        today = datetime.now().strftime("%Y%m%d")
        csv_path = self.news_dir / f"news_{today}.csv"
        return self.process_news_file(csv_path)

    def save_fundamentals(self, classified: Dict) -> Path:
        """将中长线基本面新闻保存到 fundamentals 目录

        保存结构: data/fundamentals/YYYY-MM/
          ├── long_term_YYYY-MM.json
          └── medium_term_YYYY-MM.json
        """
        now = datetime.now()
        month_dir = self.base_dir / now.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)

        prefix = now.strftime("%Y-%m")

        saved = {}
        for category in ["long", "medium"]:
            entries = classified.get(category, [])
            if not entries:
                continue

            path = month_dir / f"{category}_term_{prefix}.json"

            # 追加模式：读取已有 + 合并去重
            existing = []
            if path.exists():
                with open(path) as f:
                    existing = json.load(f)

            # 合并去重（按标题）— 新数据覆盖旧数据(补 tags 等新字段)
            merged = {e.get("title", ""): e for e in existing if e.get("title")}
            for e in entries:
                key = e.get("title", "")
                if key:
                    merged[key] = e  # 新采集覆盖: 补 tags
            unique = list(merged.values())

            with open(path, "w", encoding="utf-8") as f:
                json.dump(unique, f, ensure_ascii=False, indent=2)

            saved[category] = len(unique)
            logger.info(f"✅ {category}: {len(entries)} 条新, 共 {len(unique)} 条 -> {path}")

        return month_dir

    @staticmethod
    def analyze_sentiment(events: List[Dict], days: int = 7) -> Dict:
        """分析基本面情绪评分

        Returns:
            {"score": float,  # -1.0 ~ +1.0
             "label": str,    # 看多 / 偏多 / 中性 / 偏空 / 看空
             "bullish_count": int,
             "bearish_count": int}
        """
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(days=days)
        bull = 0
        bear = 0
        for e in events:
            # 按日期过滤，只统计最近 N 天的事件
            ts = e.get("timestamp") or e.get("date") or e.get("time")
            if ts:
                try:
                    from datetime import datetime as dt
                    event_dt = dt.fromisoformat(str(ts).replace("Z", "+00:00")) if isinstance(ts, str) else dt.fromtimestamp(float(ts))
                    if event_dt.replace(tzinfo=None) < cutoff:
                        continue
                except (ValueError, TypeError, OSError):
                    pass  # 无法解析日期的事件保留
            title = e.get("title", "") + " " + e.get("summary", "")
            for kw in BULLISH_KEYWORDS:
                if re.search(kw, title, re.IGNORECASE):
                    bull += 1
                    break
            for kw in BEARISH_KEYWORDS:
                if re.search(kw, title, re.IGNORECASE):
                    bear += 1
                    break
        total = bull + bear
        if total == 0:
            return {"score": 0, "label": "中性", "bullish_count": 0, "bearish_count": 0}
        score = (bull - bear) / max(total, 1)
        if score > 0.3:
            label = "看多"
        elif score > 0.1:
            label = "偏多"
        elif score < -0.3:
            label = "看空"
        elif score < -0.1:
            label = "偏空"
        else:
            label = "中性"
        return {"score": round(score, 2), "label": label, "bullish_count": bull, "bearish_count": bear}

    def generate_summary(self, days: int = 7, symbol: str = "EUR/USD") -> str:
        """生成近 N 天的基本面摘要(支持三品种)

        symbol: "EUR/USD" / "XAU/USD" / "AUD/USD" — 按新闻 tags 过滤;
                EUR 兼容历史数据(无 tags 字段视为 eur)。
        """
        tag_map = {"EUR/USD": "eur", "XAU/USD": "gold", "AUD/USD": "aud"}
        want_tag = tag_map.get(symbol, "eur")
        symbol_label = {"EUR/USD": "💶 EUR/USD", "XAU/USD": "🥇 XAU/USD",
                        "AUD/USD": "🦘 AUD/USD"}.get(symbol, symbol)
        lines = []
        lines.append(f"📊 {symbol_label} 基本面摘要（近{days}天）")

        long_events = []
        medium_events = []

        # 遍历 fundamentals 目录
        for month_dir in sorted(self.base_dir.iterdir()):
            if not month_dir.is_dir():
                continue

            for fpath in month_dir.glob("*_term_*.json"):
                category = "long" if "long_term" in fpath.name else "medium"
                try:
                    with open(fpath) as f:
                        entries = json.load(f)
                except Exception:
                    continue

                for entry in entries:
                    # 品种过滤: 无tags的历史数据视为eur(兼容旧数据)
                    tags = set((entry.get("tags") or "").split(",")) if entry.get("tags") else {"eur"}
                    if want_tag not in tags:
                        continue
                    if category == "long":
                        long_events.append(entry)
                    else:
                        medium_events.append(entry)

        # 输出
        lines.append("")
        lines.append(f"🔴 长期影响 ({len(long_events)} 条)")
        for e in long_events[-5:]:
            lines.append(f"  • {e['title'][:70]}")
            lines.append(f"    ({e.get('published','')[:10]})")

        lines.append("")
        lines.append(f"🟡 中期影响 ({len(medium_events)} 条)")
        for e in medium_events[-5:]:
            lines.append(f"  • {e['title'][:70]}")
            lines.append(f"    ({e.get('published','')[:10]})")

        lines.append("")

        return "\n".join(lines)

    def add_calendar_event(self, event: Dict):
        """手动添加经济日历事件到基本面

        例如 NFP 公布后，将实际值与预期对比存入 medium_term
        """
        month_dir = self.base_dir / datetime.now().strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        prefix = datetime.now().strftime("%Y-%m")
        path = month_dir / f"medium_term_{prefix}.json"

        entry = {
            "title": f"[经济数据] {event.get('currency','')} {event.get('event','')}",
            "summary": (
                f"实际: {event.get('actual','?')} | "
                f"预期: {event.get('forecast','?')} | "
                f"前值: {event.get('previous','?')}"
            ),
            "source": "ForexFactory",
            "published": f"{event.get('date','')} {event.get('time','')}",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "category": "medium",
        }

        existing = []
        if path.exists():
            with open(path) as f:
                existing = json.load(f)

        # 去重
        titles = {e.get("title", "") for e in existing}
        if entry["title"] not in titles:
            existing.append(entry)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            logger.info(f"✅ 日历事件已添加到基本面: {entry['title']}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    fa = FundamentalsAnalyzer()

    # 采集并分类
    classified = fa.collect_and_classify()
    print(f"分类结果:")
    print(f"  🔴 长期: {len(classified.get('long',[]))} 条")
    print(f"  🟡 中期: {len(classified.get('medium',[]))} 条")
    print(f"  🟢 短期: {len(classified.get('short',[]))} 条")
    print(f"  ⚪ 忽略: {len(classified.get('ignore',[]))} 条")

    # 保存
    fa.save_fundamentals(classified)

    # 输出摘要
    print()
    print(fa.generate_summary(days=7))
