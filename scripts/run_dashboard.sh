#!/bin/bash
# ============================================================
# 外汇量化交易 — 中控面板启动
# ============================================================
# 启动后会监听 http://127.0.0.1:5001
# 用法:
#   bash scripts/run_dashboard.sh          # 前台运行（Ctrl+C 停止）
#   bash scripts/run_dashboard.sh --bg     # 后台运行
#   bash scripts/run_dashboard.sh --stop   # 停止后台进程
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

# 激活虚拟环境（如果存在）
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

PID_FILE="$PROJECT_DIR/.dashboard.pid"

case "${1:-}" in
    --bg)
        echo "🌐 中控面板后台启动..."
        nohup python3 -m src.web.dashboard \
            > output/logs/dashboard.log 2>&1 &
        echo $! > "$PID_FILE"
        echo "   PID: $!"
        echo "   面板: http://127.0.0.1:5001"
        echo "   日志: output/logs/dashboard.log"
        echo "   停止: bash scripts/run_dashboard.sh --stop"
        ;;
    --stop)
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            if kill "$PID" 2>/dev/null; then
                echo "✅ 已停止 (PID: $PID)"
                rm -f "$PID_FILE"
            else
                echo "⚠️ PID $PID 已不存在，清理 pid 文件"
                rm -f "$PID_FILE"
            fi
        else
            # 降级：尝试通过端口找到进程
            PORT_PID=$(lsof -ti:5001 2>/dev/null | head -1)
            if [ -n "$PORT_PID" ]; then
                kill "$PORT_PID"
                echo "✅ 已停止端口 5001 进程 (PID: $PORT_PID)"
            else
                echo "❌ 未找到运行中的面板进程"
            fi
        fi
        ;;
    *)
        echo "🌐 外汇量化交易 · 中控面板"
        echo "   地址: http://127.0.0.1:5001"
        echo "   Ctrl+C 停止"
        mkdir -p output/logs
        python3 -m src.web.dashboard
        ;;
esac
