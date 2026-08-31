#!/bin/bash
# ============================================================
# 外汇量化交易 — macOS .app 启动器
# ============================================================
# 创建一个可双击的 .app 文件，方便启动
#
# 注意: 由于 macOS 对 ~/Documents 的沙箱隔离，
# LaunchAgent 无法后台启动，建议用此 .app 或 run_daemon.sh --bg。
#
# 用法:
#   bash scripts/install_mac.sh --app      # 创建 .app 启动器
#   bash scripts/install_mac.sh --status   # 查看运行状态
# ============================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$PROJECT_DIR/ForexTrading.app"

GREEN='\033[0;32m'
NC='\033[0m'

show_status() {
    echo "📊 外汇量化交易 — 运行状态"
    echo "============================"

    PID=$(pgrep -f "src.daemon" | head -1)
    if [ -n "$PID" ]; then
        echo "Daemon PID: $PID 🟢"
    else
        echo "Daemon: 未运行 🔴"
    fi

    curl -s http://127.0.0.1:5001/api/status &>/dev/null && echo "面板 5001: 在线 🟢" || echo "面板 5001: 离线 🔴"

    echo ""
    echo "启动: bash scripts/run_daemon.sh --bg"
    echo "停止: bash scripts/run_daemon.sh --stop"
    echo "============================"
}

create_app() {
    echo "📱 创建 .app 启动器..."
    local MACOS_DIR="$APP_DIR/Contents/MacOS"
    local RES_DIR="$APP_DIR/Contents/Resources"

    rm -rf "$APP_DIR"
    mkdir -p "$MACOS_DIR" "$RES_DIR"

    # 可执行脚本 — 调用项目的 run_daemon.sh
    cat > "$MACOS_DIR/ForexTrading" << APPEOF
#!/bin/bash
PROJECT_DIR="$PROJECT_DIR"
cd "\$PROJECT_DIR"
bash scripts/run_daemon.sh --bg
# 等面板启动后打开
sleep 3
open http://127.0.0.1:5001
APPEOF
    chmod +x "$MACOS_DIR/ForexTrading"

    cat > "$APP_DIR/Contents/Info.plist" << 'PLISTEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>ForexTrading</string>
    <key>CFBundleIdentifier</key>
    <string>com.forex.trading</string>
    <key>CFBundleName</key>
    <string>外汇量化交易</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>LSBackgroundOnly</key>
    <false/>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
PLISTEOF

    echo ""
    echo -e "${GREEN}✅ .app 已创建: $APP_DIR${NC}"
    echo "   双击 ForexTrading.app 即可启动"
    echo "   首次运行: 右键 → 打开（绕过 Gatekeeper）"
}

case "${1:-}" in
    --status)
        show_status
        ;;
    --app)
        create_app
        ;;
    *)
        echo "用法: bash scripts/install_mac.sh --app | --status"
        exit 1
        ;;
esac
