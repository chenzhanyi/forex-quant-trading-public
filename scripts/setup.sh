#!/bin/bash
# ============================================================
# 外汇量化交易 — 环境初始化脚本
# ============================================================
# 用法: bash scripts/setup.sh
# ============================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "========================================"
echo "📊 外汇量化交易系统 — 环境初始化"
echo "========================================"

# 1. 创建目录
echo ""
echo "📁 创建目录结构..."
mkdir -p data/forex/{M5,M15,H1,H4,D1}
mkdir -p data/news data/calendar data/signals data/opportunities data/fundamentals
mkdir -p output/signals output/charts output/logs output/analysis output/reviews
mkdir -p skills
# .hermes/ 为历史遗留（Hermes Agent 使用），不再主动创建
echo "   ✅ 目录已创建"

# 2. Python 虚拟环境
echo ""
echo "🐍 创建 Python 虚拟环境..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "   ✅ 虚拟环境已创建"
else
    echo "   ⏩ 虚拟环境已存在"
fi
source .venv/bin/activate

# 3. 安装依赖
echo ""
echo "📦 安装 Python 依赖..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "   ✅ 依赖安装完成"

# 4. 检查外部工具
echo ""
echo "🔧 检查外部工具..."
LARK_CLI=$(which lark-cli 2>/dev/null || echo "")
CLAUDE_CLI=$(which claude-doubao 2>/dev/null || which claude 2>/dev/null || echo "")
if [ -n "$LARK_CLI" ]; then
    echo "   ✅ lark-cli: $LARK_CLI"
    lark-cli auth status 2>/dev/null | grep -q "ready" && echo "   ✅ 飞书已认证" || echo "   ⚠️  飞书未认证，运行: lark-cli config init --new"
else
    echo "   ⚠️  未安装 lark-cli（飞书推送需要）"
fi
if [ -n "$CLAUDE_CLI" ]; then
    echo "   ✅ Claude CLI: $CLAUDE_CLI"
else
    echo "   ⚠️  未安装 Claude CLI（LLM 分析需要，安装后在 http://127.0.0.1:5001 中控台配置命令）"
fi

# 5. 本地配置模板
echo ""
echo "⚙️  检查本地配置..."
if [ ! -f "config/config.local.yaml" ]; then
    cat > config/config.local.yaml << 'LOCALEOF'
# ============================================================
# 本地配置覆盖 — 填入你的 API Key
# ============================================================

oanda:
  api_key: "YOUR_OANDA_API_KEY"
  account_id: "YOUR_ACCOUNT_ID"
  environment: "practice"

network:
  enabled: false
  http: "http://127.0.0.1:7890"
  https: "http://127.0.0.1:7890"

account:
  balance: 100
LOCALEOF
    echo "   ⚠️  请编辑 config/config.local.yaml 填入你的 OANDA API Key"
else
    echo "   ✅ 本地配置已存在"
fi

# 6. 脚本权限
chmod +x scripts/*.sh 2>/dev/null || true

# 7. 生成测试数据
echo ""
echo "🧪 生成测试数据..."
python3 scripts/demo_data.py 2>/dev/null || echo "   ⏩ 跳过（demo_data 需要 OANDA 连接）"

echo ""
echo "========================================"
echo "✅ 初始化完成！"
echo "========================================"
echo ""
echo "📋 下一步："
echo "   python -m src.daemon              # 前台启动（终端模式）"
echo "   bash scripts/run_daemon.sh --bg   # 后台 + 菜单栏图标"
echo "   编辑 config/config.local.yaml     # 填入 OANDA API Key"
echo "========================================"
