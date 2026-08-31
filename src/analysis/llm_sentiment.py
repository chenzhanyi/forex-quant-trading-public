"""Claude LLM 新闻情绪分析器 — 基本面多空评分

用法:
    analyzer = LLMSentimentAnalyzer()
    result = analyzer.analyze(days=3)  # 分析最近 3 天新闻
    # → {"label": "看多", "score": 0.6, "reason": "...", "key_events": [...]}

结果自动保存到 output/analysis/sentiment_YYYYMMDD.json，
dashboard.py 的 /api/signal 会自动读取并覆盖关键词打分。
"""
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from src.utils.config_loader import config

logger = logging.getLogger(__name__)

# ── Claude 系统提示词 ──

SYSTEM_PROMPT = """你是一个专业的 EUR/USD 外汇基本面分析师。我会给你最近几天的财经新闻标题和摘要，请你分析这些新闻对 EUR/USD 的整体情绪是看多、看空还是中性。

分析维度（按重要性排序）:
1. 利率政策方向: ECB vs Fed 的加息/降息预期 → 利差变化
2. 经济数据好坏: GDP、CPI、就业、PMI 等 → 经济增长对比
3. 地缘政治风险: 中东、能源、贸易摩擦 → 避险情绪
4. 贸易政策: 美欧关税、贸易协议 → 对欧元区经济影响

请以严格的 JSON 格式回复，不要包含任何其他文字:
{
  "label": "看多",
  "score": 0.6,
  "reason": "本周 ECB 会议纪要偏鹰，市场对 9 月加息预期升温；同时美国非农数据大幅低于预期（5.7万 vs 预期11万），美元走弱。欧元区 CPI 回落至 2.8% 降低了滞胀担忧。整体利好 EUR/USD。",
  "key_events": ["ECB 会议纪要偏鹰", "美国非农大幅低于预期", "欧元区 CPI 回落"]
}

label 取值: "看多" / "偏多" / "中性" / "偏空" / "看空"
score 取值: -1.0(极度看空) ~ +1.0(极度看多)
reason: 50-150字的核心分析，引用具体事件和数据
key_events: 2-5个最重要的影响因素

如果新闻不足以做出判断，返回:
{"label": "中性", "score": 0, "reason": "近期新闻量不足，无法做出有效判断", "key_events": []}"""


class LLMSentimentAnalyzer:
    """Claude API 新闻情绪分析器"""

    def __init__(self):
        self.cfg = config.load()
        proj_dir = Path(__file__).resolve().parent.parent.parent
        self.news_dir = proj_dir / "data" / "news"
        self.out_dir = proj_dir / "output" / "analysis"
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # LLM CLI 配置 — 面板设置优先于 YAML 配置
        from src.utils.dashboard_settings import get_llm_config
        llm_cfg = get_llm_config()
        self.news_days = llm_cfg.get("news_days", 3)
        self.timeout = llm_cfg.get("timeout", 120)
        self.cli_command = llm_cfg.get("cli_command", "claude-doubao")
        self.cli_args = llm_cfg.get("cli_args", ["-p", "--dangerously-skip-permissions", "--print", "--output-format", "json"])
        self._configured = bool(self.cli_command)

    def analyze(self, days: int = None) -> Dict:
        """分析最近 N 天新闻的情绪

        Args:
            days: 分析最近几天（默认从 config 读取）

        Returns:
            {"label": str, "score": float, "reason": str, "key_events": list, "source": str}
        """
        if days is None:
            days = self.news_days

        if not self._configured:
            return self._fallback(
                "Claude CLI 未配置。请在 config.local.yaml 中设置:\n"
                "  llm:\n"
                "    cli_command: claude\n"
                "    cli_args: [\"--print\", \"--output-format\", \"json\"]"
            )

        # 1) 加载新闻
        articles = self._load_news(days)
        if not articles:
            return self._fallback(f"近 {days} 天无新闻数据")

        # 2) 组装完整 prompt（系统提示词 + 新闻内容）
        news_text = self._format_news(articles)
        if len(news_text) < 50:
            return self._fallback("新闻内容不足")

        full_prompt = SYSTEM_PROMPT + "\n\n" + news_text

        # 3) 调用 Claude CLI（prompt 通过 stdin 传入）
        try:
            cmd = [self.cli_command] + self.cli_args

            logger.info(
                f"执行 CLI: {self.cli_command} {' '.join(self.cli_args[:3])}... "
                f"(prompt={len(full_prompt)}chars)"
            )

            result = subprocess.run(
                cmd,
                input=full_prompt,       # ← 通过 stdin 传入 prompt
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(Path(__file__).resolve().parent.parent.parent),
            )

        except subprocess.TimeoutExpired:
            return self._fallback(f"Claude CLI 超时 ({self.timeout}s)，可在 config 调整 llm.timeout")
        except FileNotFoundError:
            return self._fallback(
                f"命令未找到: {self.cli_command}\n"
                "请在 config.local.yaml 中设置正确的 llm.cli_command"
            )
        except Exception as e:
            logger.error(f"Claude CLI 调用失败: {e}")
            return self._fallback(f"CLI 调用失败: {e}")

        response_text = result.stdout.strip()
        if not response_text:
            return self._fallback("Claude CLI 返回空结果，请检查 cli_command 和 cli_args 配置")

        # 4) 解析响应 — 处理 CLI 包装层和裸 JSON 两种情况
        try:
            outer = json.loads(response_text)
        except json.JSONDecodeError:
            outer = None

        # 情况A: CLI 返回了完整执行元数据（含 type/result/session_id）
        if isinstance(outer, dict) and "result" in outer:
            raw_result = outer["result"]
            # result 字段可能是 markdown 代码块包裹的 JSON
            inner = self._extract_json(raw_result)
            if inner is None:
                return self._fallback(f"无法从 CLI 元数据中提取 JSON: {raw_result[:200]}...")
            result = inner
        # 情况B: 直接是 JSON 对象
        elif isinstance(outer, dict) and "label" in outer:
            result = outer
        else:
            # 情况C: 纯文本，尝试提取 JSON
            inner = self._extract_json(response_text)
            if inner is None:
                return self._fallback(f"Claude 返回格式异常: {response_text[:200]}...")
            result = inner

        # 5) 补充字段并保存
        result["source"] = "Claude CLI"
        result["analyzed_at"] = datetime.now(timezone.utc).isoformat()
        result["news_count"] = len(articles)
        result["news_days"] = days

        self._save(result)
        logger.info(f"✅ Claude 分析完成: {result['label']} (score={result['score']})")
        return result

    def get_today_result(self) -> Optional[Dict]:
        """获取今天已有的分析结果"""
        today = datetime.now().strftime("%Y%m%d")
        path = self.out_dir / f"sentiment_{today}.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return None

    def _load_news(self, days: int) -> list:
        """加载最近 N 天的新闻（CSV + 分类基本面）"""
        from datetime import timedelta
        # 使用 UTC aware datetime，与 collected_at 时区一致
        cutoff = datetime.now(timezone.utc) - timedelta(days=days + 1)
        articles = []
        seen = set()

        # 1) 加载原始 RSS 新闻 CSV
        for csv_path in sorted(self.news_dir.glob("news_*.csv"), reverse=True):
            try:
                df = pd.read_csv(csv_path)
                for _, row in df.iterrows():
                    title = str(row.get("title", ""))
                    if not title or title in seen:
                        continue
                    collected = str(row.get("collected_at", ""))
                    if collected:
                        try:
                            ct = datetime.fromisoformat(collected)
                            if ct < cutoff:
                                continue
                        except (ValueError, TypeError):
                            pass  # 无法解析日期则不排除
                    seen.add(title)
                    articles.append({
                        "title": title,
                        "summary": str(row.get("summary", ""))[:300],
                        "published": str(row.get("published", ""))[:16],
                        "source": str(row.get("source", "")),
                    })
            except Exception as e:
                logger.debug(f"读取新闻文件失败 {csv_path}: {e}")

        # 2) 加载已分类的基本面数据（长/中期），提供更丰富的分析上下文
        fundamentals_dir = Path(__file__).resolve().parent.parent.parent / "data" / "fundamentals"
        for json_path in sorted(fundamentals_dir.rglob("*.json"), reverse=True):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for entry in data:
                        title = entry.get("title", "")
                        if not title or title in seen:
                            continue
                        ts = entry.get("collected_at") or entry.get("timestamp") or ""
                        if ts:
                            try:
                                ct = datetime.fromisoformat(str(ts))
                                if ct < cutoff:
                                    continue
                            except (ValueError, TypeError):
                                pass
                        seen.add(title)
                        articles.append({
                            "title": title,
                            "summary": str(entry.get("summary", ""))[:300],
                            "published": str(entry.get("published", "") or entry.get("date", ""))[:16],
                            "source": f"fundamentals/{json_path.parent.name}",
                        })
            except Exception as e:
                logger.debug(f"读取基本面文件失败 {json_path}: {e}")

        return articles

    def _format_news(self, articles: list) -> str:
        """格式化新闻列表为 prompt 文本"""
        lines = [f"以下是最近 {self.news_days} 天 EUR/USD 相关的财经新闻 ({len(articles)} 条):\n"]
        for i, a in enumerate(articles[:50], 1):  # 最多 50 条
            lines.append(f"{i}. [{a['published']}] {a['title']}")
            if a["summary"]:
                lines.append(f"   摘要: {a['summary']}")
            lines.append("")
        return "\n".join(lines)

    def _save(self, result: Dict) -> Path:
        """保存分析结果"""
        today = datetime.now().strftime("%Y%m%d")
        path = self.out_dir / f"sentiment_{today}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"✅ LLM 分析结果已保存: {path}")
        return path

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict]:
        """从文本中提取 JSON 对象（处理 markdown 代码块包裹等情况）"""
        import re

        # 尝试1: 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试2: 从 ```json ... ``` 代码块提取
        m = re.search(r'```(?:json)?\s*\n?(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试3: 从任意 { ... } 提取（取最外层）
        m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass

        return None

    def _fallback(self, reason: str) -> Dict:
        """降级返回（API 不可用 / 数据不足）"""
        return {
            "label": "中性",
            "score": 0,
            "reason": reason,
            "key_events": [],
            "source": "fallback",
        }
