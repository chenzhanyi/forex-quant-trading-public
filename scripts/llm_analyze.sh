#!/bin/bash
# ============================================================
# Claude CLI 新闻分析脚本
# 由 llm_sentiment.py 通过 subprocess 调用
# 你可以修改此脚本以适配你的 Claude CLI 环境
# ============================================================
#
# 输入: $1 = 包含 prompt 的文本文件路径
# 输出: Claude 的 JSON 分析结果（stdout）
#
# Claude CLI 用法示例:
#   claude -p "$(cat prompt.txt)" --print --output-format json
#   或
#   claude --print < prompt.txt
#
# 如果你用的是 Hermes 或其他封装，修改下面这行即可
# ============================================================

PROMPT_FILE="$1"

if [ ! -f "$PROMPT_FILE" ]; then
    echo '{"label": "中性", "score": 0, "reason": "Prompt 文件不存在", "key_events": [], "source": "error"}'
    exit 1
fi

# ── 这里改成你的 Claude CLI 调用方式 ──
# 方式A: 直接传 prompt 文本
claude -p "$(cat "$PROMPT_FILE")" --print --output-format json 2>/dev/null

# 方式B: 如果你用的是 Hermes 或其他封装:
# hermes ask --file "$PROMPT_FILE" --json

# 方式C: 如果你的 claude 不支持 --output-format json:
# claude -p "$(cat "$PROMPT_FILE")" --print 2>/dev/null
