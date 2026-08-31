#!/bin/bash
# ============================================================
# 外汇量化交易 — 守护进程
# ============================================================
# 同时启动:
#   🌐 中控面板 http://127.0.0.1:5001
#   🔄 定时信号评估（每 15 分钟）
#   📋 自动复盘（每 6 小时）
# ============================================================
# 用法:
#   bash scripts/run_daemon.sh          # 前台运行（终端模式）
#   bash scripts/run_daemon.sh --bg     # 后台运行 + 菜单栏图标
#   bash scripts/run_daemon.sh --status # 查看运行状态
#   bash scripts/run_daemon.sh --stop   # 停止 daemon
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

mkdir -p output/logs

case "${1:-}" in
    --bg)
        echo "🚀 守护进程后台启动（含菜单栏图标）..."
        nohup python3 -m src.daemon --menubar \
            > output/logs/daemon.log 2>&1 &
        echo "   PID: $!"
        echo "   面板: http://127.0.0.1:5001"
        echo "   日志: output/logs/daemon.log"
        echo "   停止: 菜单栏图标 → 退出 / pkill -f 'src.daemon'"
        ;;
    --status)
        PID=$(pgrep -f "src.daemon" | head -1)
        if [ -n "$PID" ]; then
            echo "✅ 守护进程运行中 (PID: $PID)"
            echo "   面板: http://127.0.0.1:5001"
            SIGNAL_COUNT=$(curl -s http://127.0.0.1:5001/api/daemon/status 2>/dev/null | grep -o '"eval_count":[0-9]*' | cut -d: -f2)
            echo "   信号评估次数: ${SIGNAL_COUNT:-未知}"
        else
            echo "❌ 守护进程未运行"
            echo "   启动: bash scripts/run_daemon.sh"
        fi
        ;;
    --stop)
        PID=$(pgrep -f "src.daemon" | head -1)
        if [ -n "$PID" ]; then
            kill "$PID"
            echo "✅ 已停止 (PID: $PID)"
        else
            echo "❌ 未运行"
        fi
        ;;
    *)
        echo "========================================"
        echo "🚀 外汇量化交易 · 守护进程"
        echo "========================================"
        echo "   面板: http://127.0.0.1:5001"
        echo "   信号: 每 15 分钟评估"
        echo "   复盘: 每 6 小时"
        echo "   Ctrl+C 停止"
        echo "========================================"
        python3 -m src.daemon
        ;;
esac
