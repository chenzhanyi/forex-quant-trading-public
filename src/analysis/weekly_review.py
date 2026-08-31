"""Claude CLI 驱动的周度策略复盘分析器

用法:
    # Python API
    from src.analysis.weekly_review import WeeklyReviewAnalyzer
    analyzer = WeeklyReviewAnalyzer()
    result = analyzer.analyze(weeks=1)
    print(result["report"])

    # 命令行
    python -m src.analysis.weekly_review --weeks 1
    python -m src.analysis.weekly_review --json  # 输出 JSON

设计:
    - 复用中控台配置的 Claude CLI（claude-doubao）
    - 将本地 skills/*.md 作为上下文拼入 prompt
    - 预采集信号/复盘数据，通过 stdin 传给 Claude
    - 结果保存到 output/analysis/weekly_review_YYYYMMDD.md
"""

import json
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJ = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = PROJ / "skills"

# ── Claude 系统提示词（策略复盘角色） ──

STRATEGY_REVIEW_SYSTEM_PROMPT = """你是一个专业的 EUR/USD 外汇量化交易策略分析师，正在进行周度策略复盘。

你的分析必须:
1. 基于我提供的实际信号数据和复盘报告，不编造任何数据
2. 从策略参数层面分析问题根因（而非市场随机性）
3. 给出可操作的、有优先级的优化建议
4. 区分「需要立即修复的问题」和「可以观察的改进方向」

## 分析框架

### 第一步: 数据总览
- 统计本周信号总数、入场/观望比例、趋势分布
- 对比上周数据，找变化趋势

### 第二步: 问题诊断
- 识别信号未命中的根因（趋势误判? 入场过严? 过滤器过激?）
- 分析是否有多重过滤同时拦截的情况（系统性过保守）
- 检查基本面与技术面是否出现背离

### 第三步: 参数评估
逐项评估每个策略参数是否合理:
- D1 50SMA 趋势判断
- H4 200SMA 斜率动量阈值 (slope_momentum)
- M15 5EMA/15EMA 入场条件（唯一入场时间框架）
- ATR 波动率过滤 (min_atr_ratio)
- 追高保护距离 (max_distance_from_h4_sma)
- K线形态评分阈值 (min_pattern_score)
- Fibonacci 回调验证
- R:R 比例要求
- 周五规则 / 重大事件静默期

### 第四步: 优化建议
- 标注优先级: 🔴紧急 🟠高 🟡中 🟢低
- 每个建议必须说明: 当前值 → 建议值 → 预期效果 → 风险

## 输出要求
请严格按照以下 Markdown 格式输出，不要添加额外说明文字:

=== 策略周复盘 YYYYMMDD ===

📊 本周概况:
  ...

🔍 问题分析:
  ...

📈 参数评估:
  ...

💡 优化建议:
  ...

⚠️ 注意事项:
  ..."""


class WeeklyReviewAnalyzer:
    """Claude CLI 驱动的周度策略复盘"""

    def __init__(self):
        self.proj = PROJ
        self.signals_dir = self.proj / "output" / "signals"
        self.reviews_dir = self.proj / "output" / "reviews"
        self.out_dir = self.proj / "output" / "analysis"
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # LLM CLI 配置 — 与 llm_sentiment.py 完全一致的加载逻辑
        from src.utils.dashboard_settings import get_llm_config
        llm_cfg = get_llm_config()
        self.cli_command = llm_cfg.get("cli_command", "claude-doubao")
        self.cli_args = llm_cfg.get("cli_args", ["-p", "--dangerously-skip-permissions", "--print", "--output-format", "json"])
        self.timeout = llm_cfg.get("timeout", 300)  # 周复盘需要更长超时
        self._configured = bool(self.cli_command)

    # ── 公开 API ──

    def analyze(self, weeks: int = 1) -> Dict:
        """执行周度策略复盘

        Args:
            weeks: 分析最近几周（默认 1 周）

        Returns:
            {"report": str, "path": str, "analyzed_at": str, "weeks": int}
        """
        if not self._configured:
            return self._error("Claude CLI 未配置，请在中控台设置 LLM CLI 命令")

        # 1) 加载 skills 上下文
        skills_context = self._load_skills()

        # 2) 采集信号和复盘数据
        data_context = self._collect_data(weeks)
        if not data_context:
            return self._error(f"近 {weeks} 周无信号/复盘数据")

        # 3) 加载当前策略配置
        config_context = self._load_config_summary()

        # 4) 组装完整 prompt
        today_str = datetime.now().strftime("%Y%m%d")
        task_prompt = f"""请对 EUR/USD 交易系统进行周度策略复盘。

当前日期: {datetime.now().strftime('%Y-%m-%d')} (GMT+8)
分析范围: 最近 {weeks} 周

以下是需要分析的数据:

{data_context}

---

以下是当前策略配置:

{config_context}

---

请按照分析框架，给出完整的策略周复盘报告。在报告标题中使用日期 {today_str}。
用中文输出。"""

        full_prompt = (
            skills_context + "\n\n"
            + STRATEGY_REVIEW_SYSTEM_PROMPT + "\n\n"
            + "=" * 60 + "\n\n"
            + task_prompt
        )

        # 5) 调用 Claude CLI
        try:
            cmd = [self.cli_command] + self.cli_args

            logger.info(
                f"🤖 调用 Claude CLI: {self.cli_command} "
                f"(prompt={len(full_prompt)}chars, timeout={self.timeout}s)"
            )

            result = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(self.proj),
            )

        except subprocess.TimeoutExpired:
            return self._error(f"Claude CLI 超时 ({self.timeout}s)")
        except FileNotFoundError:
            return self._error(f"命令未找到: {self.cli_command}")
        except Exception as e:
            return self._error(f"CLI 调用失败: {e}")

        # 6) 解析响应
        response_text = result.stdout.strip()
        if not response_text:
            stderr_tail = result.stderr.strip()[-500:] if result.stderr else "(空)"
            return self._error(f"Claude CLI 返回空结果\nstderr: {stderr_tail}")

        # 尝试从 JSON 包装中提取（CLI --output-format json 会包装结果）
        report_text = self._extract_report(response_text)

        # 7) 保存
        report_path = self._save_report(report_text)
        logger.info(f"✅ 周复盘完成: {report_path}")

        return {
            "report": report_text,
            "path": str(report_path),
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "weeks": weeks,
        }

    # ── 内部方法 ──

    def _load_skills(self) -> str:
        """加载本地 skills 作为上下文"""
        parts = []
        # 按顺序加载（策略 → 复盘工作流）
        skill_files = ["forex-strategy.md", "forex-review-workflow.md"]
        for name in skill_files:
            path = SKILLS_DIR / name
            if path.exists():
                content = path.read_text(encoding="utf-8")
                # 剥离 frontmatter 注释（<!-- --> 包裹的部分）
                # 只保留正文内容
                body = self._strip_frontmatter(content)
                parts.append(body)
        if parts:
            return "# 交易系统参考文档\n\n" + "\n\n---\n\n".join(parts)
        return ""

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        """去除 HTML 注释式的 frontmatter"""
        import re
        # 去除开头的 <!-- ... --> 注释块
        text = re.sub(r'^<!--.*?-->\s*', '', text, flags=re.DOTALL)
        return text.strip()

    def _collect_data(self, weeks: int) -> str:
        """采集最近 N 周的信号和复盘数据"""
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)
        lines = []

        # ── 信号统计 ──
        signal_files = sorted(self.signals_dir.rglob("*.json"), reverse=True)
        recent_signals = []
        for f in signal_files:
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    recent_signals.append(data)
            except Exception:
                pass

        if recent_signals:
            total = len(recent_signals)
            can_trade = sum(1 for s in recent_signals if s.get("can_trade"))
            observe = total - can_trade
            directions = {}
            for s in recent_signals:
                d = s.get("direction", "观望")
                directions[d] = directions.get(d, 0) + 1

            skip_reasons = {}
            for s in recent_signals:
                r = s.get("skip_reason", "")
                if r:
                    skip_reasons[r[:80]] = skip_reasons.get(r[:80], 0) + 1

            lines.append(f"## 信号数据 (近 {weeks} 周)\n")
            lines.append(f"- 总信号数: {total}")
            lines.append(f"- 可入场: {can_trade} ({can_trade/max(total,1)*100:.0f}%)")
            lines.append(f"- 观望: {observe} ({observe/max(total,1)*100:.0f}%)")
            lines.append(f"- 方向分布: {json.dumps(directions, ensure_ascii=False)}")

            if skip_reasons:
                lines.append(f"\n### 观望原因 Top 10:")
                for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1])[:10]:
                    lines.append(f"  - [{count}次] {reason}")

            # 最近 10 条信号明细
            lines.append(f"\n### 最近 10 条信号明细:")
            for s in recent_signals[:10]:
                lines.append(
                    f"  - {s.get('timestamp', '?')} | "
                    f"方向={s.get('direction', '?')} | "
                    f"可交易={s.get('can_trade', False)} | "
                    f"趋势={s.get('trend', '?')} | "
                    f"D1={s.get('d1', '?')}/H4={s.get('h4', '?')} | "
                    f"置信度={s.get('confidence', 0)}"
                )
                if not s.get("can_trade"):
                    lines.append(f"    跳过原因: {s.get('skip_reason', '?')[:120]}")

        # ── 复盘报告摘要 ──
        review_files = sorted(self.reviews_dir.rglob("*.md"), reverse=True)
        recent_reviews = []
        for f in review_files:
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
                recent_reviews.append(f)
            except Exception:
                pass

        if recent_reviews:
            lines.append(f"\n## 复盘报告 (近 {weeks} 周, {len(recent_reviews)} 份)\n")
            for f in recent_reviews[:5]:
                content = f.read_text(encoding="utf-8")
                # 只取前 500 字符作为摘要
                lines.append(f"### {f.name}")
                lines.append(f"```\n{content[:800]}\n```\n")
        else:
            lines.append(f"\n## 复盘报告\n(无)\n")

        return "\n".join(lines) if lines else ""

    def _load_config_summary(self) -> str:
        """加载当前策略配置摘要"""
        try:
            from src.utils.config_loader import config
            cfg = config.load()
            strategy = cfg.get("strategy", {})

            lines = ["```yaml"]
            lines.append("# 趋势")
            trend = strategy.get("trend", {})
            lines.append(f"d1_sma_period: {trend.get('d1_sma_period', '?')}")
            lines.append(f"h4_sma_period: {trend.get('h4_sma_period', '?')}")
            lines.append(f"slope_bars: {trend.get('slope_bars', '?')}")

            lines.append("\n# 入场")
            entry = strategy.get("entry", {})
            lines.append(f"fast_ema: {entry.get('fast_ema', '?')}")
            lines.append(f"slow_ema: {entry.get('slow_ema', '?')}")
            lines.append(f"slope_momentum: {entry.get('slope_momentum', '?')}")
            lines.append(f"max_distance_from_h4_sma: {entry.get('max_distance_from_h4_sma', '?')}")
            lines.append(f"min_pattern_score: {entry.get('min_pattern_score', '?')}")
            vol = entry.get("volatility_filter", {})
            lines.append(f"volatility_filter.enabled: {vol.get('enabled', '?')}")
            lines.append(f"volatility_filter.min_atr_ratio: {vol.get('min_atr_ratio', '?')}")

            lines.append("\n# 风控")
            risk = strategy.get("risk", {})
            lines.append(f"rr_min_ratio: {risk.get('rr_min_ratio', '?')}")
            lines.append(f"risk_per_trade: {risk.get('risk_per_trade', '?')}")
            sl = risk.get("stop_loss", {})
            lines.append(f"stop_loss.method: {sl.get('method', '?')}")
            lines.append(f"stop_loss.atr_multiplier: {sl.get('atr_multiplier', '?')}")

            lines.append("\n# 特殊规则")
            cal = strategy.get("calendar", {})
            lines.append(f"friday_no_trade: {cal.get('friday_no_trade', '?')}")
            lines.append(f"major_event_warning: {cal.get('major_event_warning', '?')}")

            lines.append("\n# 账户")
            acct = cfg.get("account", {})
            lines.append(f"balance: ${acct.get('balance', '?')}")
            lines.append(f"leverage: 1:{acct.get('leverage', '?')}")
            lines.append(f"default_lots: {acct.get('default_lots', '?')}")
            lines.append("```")

            return "\n".join(lines)
        except Exception as e:
            return f"(无法加载配置: {e})"

    def _extract_report(self, raw_response: str) -> str:
        """从 Claude CLI 响应中提取报告正文"""
        # 尝试1: 解析 JSON 包装
        try:
            outer = json.loads(raw_response)
            if isinstance(outer, dict) and "result" in outer:
                return str(outer["result"])
        except json.JSONDecodeError:
            pass

        # 尝试2: 纯文本 — 找 === 策略周复盘 === 标记
        if "=== 策略周复盘" in raw_response:
            # 从标题开始截取
            idx = raw_response.index("=== 策略周复盘")
            return raw_response[idx:]

        # 兜底: 返回原文
        return raw_response

    def _save_report(self, text: str) -> Path:
        """保存复盘报告"""
        today = datetime.now().strftime("%Y%m%d")
        path = self.out_dir / f"weekly_review_{today}.md"
        # 清理可能的 markdown 代码块残留
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        path.write_text(clean.strip(), encoding="utf-8")
        logger.info(f"📄 报告已保存: {path}")
        return path

    def _error(self, msg: str) -> Dict:
        logger.error(msg)
        return {"error": msg, "report": "", "path": ""}


# ── CLI 入口 ──

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    weeks = 1
    output_json = False

    for arg in sys.argv[1:]:
        if arg.startswith("--weeks="):
            weeks = int(arg.split("=")[1])
        elif arg == "--json":
            output_json = True

    analyzer = WeeklyReviewAnalyzer()
    result = analyzer.analyze(weeks=weeks)

    if result.get("error"):
        print(f"❌ {result['error']}", file=sys.stderr)
        sys.exit(1)

    if output_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["report"])
