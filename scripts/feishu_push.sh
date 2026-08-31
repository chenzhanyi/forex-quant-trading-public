#!/bin/bash
# ============================================================
# 飞书推送工具 — 发送消息到你的飞书
# ============================================================
# 用法: bash scripts/feishu_push.sh "消息内容"
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

USER_ID="ou_1553da692077e5bb106135e56298028a"

# 支持从参数读取或从 stdin 读取
if [ -n "$1" ]; then
    MESSAGE="$1"
else
    MESSAGE=$(cat)
fi

if [ -z "$MESSAGE" ] || [ "$MESSAGE" = "无内容" ]; then
    MESSAGE="无内容"
fi

lark-cli im +messages-send \
  --user-id "$USER_ID" \
  --text "$MESSAGE" \
  --as bot \
  --format json > /dev/null 2>&1

if [ $? -eq 0 ]; then
    echo "✅ 飞书推送成功"
else
    echo "❌ 飞书推送失败"
fi
