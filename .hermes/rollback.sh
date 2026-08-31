#!/bin/bash
# ============================================================
# 回滚脚本 — 恢复到指定备份版本
# ============================================================
# 用法:
#   bash .hermes/rollback.sh                  # 列出可用备份
#   bash .hermes/rollback.sh <备份目录名>      # 回滚到指定版本
# ============================================================

BACKUP_DIR=".hermes/backups"

if [ -z "$1" ]; then
    echo "📋 可用备份:"
    echo ""
    for d in "$BACKUP_DIR"/*/; do
        name=$(basename "$d")
        files=$(ls "$d" | wc -l)
        echo "  $name  ($files 文件)"
        cat "$d/changelog.txt" 2>/dev/null | head -3
        echo ""
    done
    echo "用法: bash .hermes/rollback.sh <备份名>"
    exit 0
fi

TARGET="$BACKUP_DIR/$1"
if [ ! -d "$TARGET" ]; then
    echo "❌ 备份不存在: $1"
    echo "   可用: $(ls $BACKUP_DIR/ 2>/dev/null)"
    exit 1
fi

# 先备份当前状态
NOW=$(date +%Y%m%d_%H%M%S)
PRE_ROLLBACK="$BACKUP_DIR/pre_rollback_${NOW}"
mkdir -p "$PRE_ROLLBACK"
for f in "$TARGET"/*; do
    fname=$(basename "$f")
    if [ -f "$fname" ]; then
        cp "$fname" "$PRE_ROLLBACK/"
    fi
    # 也检查 src/ 子目录
    for srcfile in $(find src/ -name "$fname" -type f 2>/dev/null); do
        mkdir -p "$PRE_ROLLBACK/$(dirname $srcfile)"
        cp "$srcfile" "$PRE_ROLLBACK/$srcfile"
    done
done

echo "📦 已备份当前状态到 $PRE_ROLLBACK"

# 恢复文件
echo ""
echo "🔄 回滚到 $1..."
for f in "$TARGET"/*; do
    fname=$(basename "$f")
    # 跳过非代码文件
    case "$fname" in
        changelog.txt|*.md) continue ;;
    esac
    # 查找目标文件
    found=$(find . -name "$fname" -type f ! -path "./.hermes/*" ! -path "./.venv/*" 2>/dev/null | head -1)
    if [ -n "$found" ]; then
        cp "$f" "$found"
        echo "  ✅ $found ← $fname"
    else
        echo "  ⚠️ 找不到目标路径: $fname"
    fi
done

echo ""
echo "✅ 回滚完成"
echo "⚠️  请重启守护进程: bash scripts/run_daemon.sh --stop && bash scripts/run_daemon.sh --bg"
