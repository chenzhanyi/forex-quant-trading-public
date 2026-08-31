#!/bin/bash
# ============================================================
# 外汇量化交易 — Claude CLI 周度策略复盘
# ============================================================
# 使用中控台配置的 Claude CLI（claude-doubao）执行周复盘。
# 自动加载 skills/ 下的策略和复盘工作流文档作为上下文。
#
# 用法:
#   bash scripts/run_weekly_review.sh            # 复盘最近 1 周
#   bash scripts/run_weekly_review.sh --weeks 2  # 复盘最近 2 周
#   bash scripts/run_weekly_review.sh --json     # 输出 JSON 格式
#   bash scripts/run_weekly_review.sh --help     # 帮助
#
# 关联:
#   - skills/forex-strategy.md        策略参数与规则参考
#   - skills/forex-review-workflow.md 复盘工作流与模板
#   - src/analysis/weekly_review.py   Python 分析模块
#   - config/dashboard_settings.json  Claude CLI 配置
#
# 替代:
#   原 Hermes 版本: scripts/cron_hermes_review.sh (已弃用)
# ============================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

# 激活虚拟环境
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

mkdir -p output/analysis output/logs

# 解析参数
WEEKS=1
OUTPUT_JSON=false
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --weeks=*)
            WEEKS="${1#*=}"
            shift
            ;;
        --weeks)
            WEEKS="$2"
            shift 2
            ;;
        --json)
            OUTPUT_JSON=true
            shift
            ;;
        --help|-h)
            echo "用法: bash scripts/run_weekly_review.sh [选项]"
            echo ""
            echo "选项:"
            echo "  --weeks=N    分析最近 N 周 (默认 1)"
            echo "  --json       输出 JSON 格式"
            echo "  --help       帮助"
            echo ""
            echo "示例:"
            echo "  bash scripts/run_weekly_review.sh"
            echo "  bash scripts/run_weekly_review.sh --weeks 2"
            echo "  bash scripts/run_weekly_review.sh --weeks 4 --json"
            exit 0
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

LOG="output/logs/weekly_review_$(date +%Y%m%d).log"

echo "==========================================" | tee "$LOG"
echo "📊 Claude CLI 周度策略复盘" | tee -a "$LOG"
echo "   日期: $(date '+%Y-%m-%d %H:%M') GMT+8" | tee -a "$LOG"
echo "   范围: 最近 ${WEEKS} 周" | tee -a "$LOG"
echo "   Skills: skills/forex-strategy.md + skills/forex-review-workflow.md" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"
echo "" | tee -a "$LOG"

# 构建 Python 参数
PY_ARGS=("--weeks=$WEEKS")
if [ "$OUTPUT_JSON" = true ]; then
    PY_ARGS+=("--json")
fi

# 执行分析
python3 -m src.analysis.weekly_review "${PY_ARGS[@]}" 2>&1 | tee -a "$LOG"
EXIT_CODE=${PIPESTATUS[0]}

echo "" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ 周复盘完成" | tee -a "$LOG"
    echo "   报告: output/analysis/weekly_review_$(date +%Y%m%d).md" | tee -a "$LOG"
else
    echo "❌ 周复盘失败 (退出码: $EXIT_CODE)" | tee -a "$LOG"
fi
echo "   日志: $LOG" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"

exit $EXIT_CODE
