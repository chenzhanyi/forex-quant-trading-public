#!/bin/bash
# ============================================================
# Hermes 基本面分析 — 由 LLM 驱动的新闻/数据解读
# ============================================================
# 由 crontab 定时触发，每次全新会话，无上下文污染
# 
# 行为约束:
#   ✅ 只读分析，不修改项目文件
#   ✅ 理性客观，不编造数据
#   ✅ 输出结构化结论到 output/analysis/
#   ❌ 不修改 config/ src/ data/ 下的任何文件
#   ❌ 不依赖任何历史对话上下文
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

mkdir -p output/analysis output/logs

LOG="output/logs/hermes_analysis_$(date +%Y%m%d).log"

# ── 检查是否有新数据需要分析 ──
# 只在有新的新闻/日历数据时才跑，避免空转
python3 -c "
from pathlib import Path
from datetime import datetime, timedelta
news_dir = Path('data/news')
cal_dir = Path('data/calendar')
today = datetime.now().strftime('%Y%m%d')
news_file = news_dir / f'news_{today}.csv'
cal_file = cal_dir / f'calendar_{today}.json'
if news_file.exists() or cal_file.exists():
    print('有最新数据，启动分析')
else:
    print('今日无新数据，跳过分析')
" 2>&1 | tee "$LOG"

grep -q "跳过" "$LOG" && exit 0

echo "" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"
echo "🧠 Hermes 基本面分析" | tee -a "$LOG"
echo "    $(date '+%Y-%m-%d %H:%M') GMT+8" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"

# ── 用 Hermes CLI 做 LLM 分析 ──
# 每次全新会话，--skills 加载项目 skill，--worktree 隔离
# prompt 严格约束行为

hermes chat -q "
你是一个外汇基本面分析师，正在分析 EUR/USD 的最新资讯。

## 你的身份
- 你是理性的量化交易辅助系统
- 你的分析基于数据和事实，不猜测、不编造
- 你的输出必须可验证、可追溯

## 你的任务
1. 读取项目目录 data/news/ 和 data/calendar/ 下的最新数据文件
2. 分析这些资讯对 EUR/USD 的中长期影响
3. 输出结构化的分析结论

## 行为约束
- ❌ 不要修改项目中的任何文件（config/ src/ data/ 等）
- ❌ 不要安装或删除任何软件包
- ❌ 不要执行 git 操作
- ❌ 不要编造不存在的数据
- ✅ 只做只读分析
- ✅ 分析结论输出到 output/analysis/ 目录

## 输出格式
请输出以下 JSON（不要添加额外说明），同时将评估结论追加入 output/analysis/ 目录下一个名为 sentiment_YYYYMMDD.json 的文件中：

=== 基本面分析 $(date +%Y%m%d) ===

📊 市场情绪: [看多/偏多/中性/偏空/看空]
📈 关键因素: [列出 2-3 个最重要的影响因素]
📰 相关新闻: [列出相关新闻标题及影响方向]
⚠️ 风险提示: [列出需要注意的风险点]
💡 操作参考: [结合当前 D1 50SMA + H4 200SMA 趋势，给出操作建议]

同时输出 JSON 格式的评分用于面板展示（追加到 output/analysis/sentiment_$(date +%Y%m%d).json）:
{\"label\": \"看多/偏多/中性/偏空/看空\", \"score\": 0.XX, \"reason\": \"一句话说明理由\"}"
" \
  --skills "外汇量化交易/skills/forex-daily-signal" \
  --worktree \
  -v 2>&1 | tee -a "$LOG"

echo "==========================================" | tee -a "$LOG"
echo "✅ 分析完成" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"
