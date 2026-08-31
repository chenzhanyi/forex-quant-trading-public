# .hermes 目录 — 历史遗留

> ⚠️ 此目录为 Hermes Agent 使用时期的历史遗留，**不再使用**。

当前项目的 LLM 分析已迁移到 **Claude CLI**（通过中控台统一配置）。

## 内容说明

| 路径 | 说明 | 状态 |
|------|------|------|
| `backups/` | Hermes 自动备份（项目文件历史快照） | 仅保留备查 |
| `plans/` | Hermes 生成的执行计划 | 历史参考 |
| `changelogs/` | Hermes 记录的操作变更日志 | 历史参考 |
| `crontab.conf` | Hermes 定时任务配置 | 已弃用 |
| `rollback.sh` | Hermes 回滚脚本 | 已弃用 |

## 当前方案

- **LLM 分析**: `claude-doubao` CLI → 配置在 `config/dashboard_settings.json`（中控台可在线修改）
- **周复盘**: `bash scripts/run_weekly_review.sh` → 使用 `src/analysis/weekly_review.py`
- **情绪分析**: `src/analysis/llm_sentiment.py` → 调用 Claude CLI
- **Skills**: `skills/` 目录 → 本地 Markdown，作为 Claude CLI prompt 上下文
