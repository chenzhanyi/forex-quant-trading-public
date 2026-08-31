# 外汇量化交易 — 本地 Skills 说明

> 这些 skills 文件是 Claude CLI 分析任务时的**上下文参考文档**。
> 在执行周复盘、日度分析等任务时，将对应 skill 内容作为 prompt 前缀传入。

---

## 📁 Skills 清单

| Skill 文件 | 用途 | 适用场景 |
|-----------|------|---------|
| `forex-strategy.md` | 交易策略完整参数与规则 | 策略复盘、参数优化建议 |
| `forex-review-workflow.md` | 复盘工作流与输出模板 | 日度/周度复盘生成 |

---

## 🚀 使用方式

### 方式 A: Shell 脚本（推荐）

```bash
# Claude CLI 周复盘（自动加载 skills）
bash scripts/run_weekly_review.sh

# 指定分析周数
bash scripts/run_weekly_review.sh --weeks 2

# 仅输出 JSON
bash scripts/run_weekly_review.sh --json
```

### 方式 B: Python 模块

```python
from src.analysis.weekly_review import WeeklyReviewAnalyzer

analyzer = WeeklyReviewAnalyzer()
result = analyzer.analyze(weeks=1)
print(result["report"])
```

### 方式 C: 直接调用 Claude CLI（手动拼接 skill）

```bash
cat skills/forex-strategy.md skills/forex-review-workflow.md <(echo "你的任务...") \
  | claude-doubao -p --dangerously-skip-permissions --print --output-format json
```

---

## ⚙️ Claude CLI 配置

中控台设置 → LLM CLI 配置（存储在 `config/dashboard_settings.json`）:

```json
{
  "llm": {
    "cli_command": "claude-doubao",
    "cli_args": ["-p", "--dangerously-skip-permissions", "--print", "--output-format", "json"]
  }
}
```

可通过中控台 http://127.0.0.1:5001 在线修改，或直接编辑 `config/dashboard_settings.json`。

---

## 📝 添加新 Skill

1. 在 `skills/` 下创建 `your-skill-name.md`
2. 文件头部包含 frontmatter 注释:

```markdown
<!--
skill: your-skill-name
usage: |
  作为 Claude CLI prompt 前缀使用。
  适用命令: bash scripts/run_xxx.sh
-->
```

3. 在本 README 的 Skills 清单中注册
