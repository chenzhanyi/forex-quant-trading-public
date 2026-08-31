# 外汇量化交易系统 — 项目即开即用指南

> EUR/USD 中长线量化交易 · D1+H4 趋势 + M15 四步入场

---

## 快速启动（新环境 3 步上手）

```bash
# 1. 初始化环境
bash scripts/setup.sh

# 2. 编辑本地配置（填入 OANDA API Key）
vim config/config.local.yaml

# 3. 启动
bash scripts/run_daemon.sh --bg     # 后台 + macOS 菜单栏
# 或
bash scripts/run_dashboard.sh        # 仅 Web 面板
```

面板地址: **http://127.0.0.1:5001**

---

## 目录结构

```
外汇量化交易/
├── PROJECT.md               ← 本文件（即开即用指南）
├── 项目说明.md               ← 完整策略说明
├── requirements.txt          ← Python 依赖
│
├── config/
│   ├── config.yaml           ← 策略参数（版本控制）
│   ├── config.local.yaml     ← 敏感信息（OANDA Key, 代理, 不提交 Git）
│   └── dashboard_settings.json ← 中控台在线配置（Claude CLI 命令等）
│
├── src/
│   ├── strategy/             ← 核心策略
│   │   ├── trend.py          ← D1 50SMA + H4 200SMA 大势判断
│   │   ├── entry.py          ← M15 四步入场法（波段→EMA→回弹→反转）
│   │   ├── signal.py         ← 信号生成流水线
│   │   ├── risk.py           ← 风控（M15波段止损 + H4 止盈）
│   │   └── review.py         ← 复盘引擎
│   ├── analysis/
│   │   ├── indicators.py     ← 技术指标（SMA/EMA/ATR/RSI/形态/Fib）
│   │   ├── llm_sentiment.py  ← Claude CLI 新闻情绪分析
│   │   ├── weekly_review.py  ← Claude CLI 周度策略复盘
│   │   ├── fundamentals.py   ← 基本面关键词分析
│   │   ├── opportunity_tracker.py ← 错失机会追踪
│   │   └── visualization.py  ← 图表输出
│   ├── daemon/               ← 守护进程（定时信号+复盘+推送）
│   ├── data_collection/      ← OANDA/日历/新闻 数据采集
│   └── web/                  ← Flask 中控面板
│
├── skills/                   ← Claude CLI 上下文文档（本地化）
│   ├── README.md             ← Skills 使用说明
│   ├── forex-strategy.md     ← 策略参数与规则参考
│   └── forex-review-workflow.md ← 复盘工作流与模板
│
├── scripts/
│   ├── setup.sh              ← 一键环境初始化
│   ├── run_daemon.sh         ← 守护进程（面板+定时信号+菜单栏）
│   ├── run_dashboard.sh      ← 仅 Web 面板
│   ├── run_weekly_review.sh  ← Claude CLI 周复盘
│   ├── backtest_four_step.py ← 四步入场法回测
│   └── demo_data.py          ← 生成测试数据
│
├── data/                     ← 行情/新闻/日历 本地缓存
├── output/                   ← 信号/复盘/分析 输出
├── .hermes/                  ← 历史遗留（Hermes Agent），不再使用
└── .venv/                    ← Python 虚拟环境
```

---

## 外部工具依赖

| 工具 | 用途 | 必需？ | 安装方式 |
|------|------|--------|---------|
| Python 3.9+ | 运行环境 | ✅ 必需 | 系统自带或 `brew install python` |
| Claude CLI (`claude-doubao`) | LLM 情绪分析+周复盘 | 🟡 可选 | 安装后在 http://127.0.0.1:5001 配置 |
| lark-cli | 飞书推送 | 🟡 可选 | `pip install lark-cli` |
| OANDA API Key | 实时行情 | 🟡 可选 | 免费注册 practice 账号 |

> 没有 Claude CLI 时，中控台仍可运行，只是 `/api/llm-sentiment` 和周复盘功能不可用。基本面会降级到关键词打分。

---

## Claude CLI 配置

中控台 → LLM 设置，或直接编辑 `config/dashboard_settings.json`:

```json
{
  "llm": {
    "cli_command": "claude-doubao",
    "cli_args": ["-p", "--dangerously-skip-permissions", "--print", "--output-format", "json"]
  }
}
```

支持的 CLI 命令: `claude-doubao` / `claude` / `claude-code` / 任意自定义命令（只要接受 stdin prompt + 输出 JSON）。

---

## 策略速览

### 趋势判断 (D1+H4)
- D1 50SMA 定大势方向
- H4 200SMA 确认中期趋势 + 斜率动量过滤

### 入场 (M15 四步法)
- **做空**: 反弹高点 → EMA 死叉 → 回弹不破前高 → 反转形态 → 入场
- **做多**: 回调低点 → EMA 金叉 → 回踩不破前低 → 反转形态 → 入场

### 止损止盈
- **止损**: M15 波段极值点（< 20pips 时升级到 H4 级别宽止损）
- **止盈 TP1**: H4 前低/前高
- **止盈 TP2**: 斐波那契 161.8% 延伸

### 仓位
- $100 账户固定 0.01 手，$500 以上按风险比例缩放

---

## 常用命令

```bash
# 启动
bash scripts/run_daemon.sh --bg          # 后台守护进程 + 菜单栏
bash scripts/run_dashboard.sh             # 仅 Web 面板

# 停止
bash scripts/run_daemon.sh --stop

# 分析
bash scripts/run_weekly_review.sh         # Claude CLI 周复盘
python scripts/backtest_four_step.py      # 四步入场法回测

# 数据
python -c "
from src.data_collection.oanda import OandaClient
o = OandaClient()
for tf in ['M5','M15','H1','H4','D1']:
    d = o.fetch_candles(tf, 500)
    o.save_candles(d, tf)
"

# 状态
bash scripts/run_daemon.sh --status       # 守护进程状态
curl http://127.0.0.1:5001/api/signal     # 当前信号 (JSON)
```

---

## 换 Agent 说明

本项目设计为 **Agent 无关**。Skills 文档 (`skills/`) 是纯 Markdown，任何 LLM Agent 都可以读取作为上下文。

**使用 Claude Code (claude-doubao)**:
```bash
bash scripts/run_weekly_review.sh
```

**使用其他 Agent（如 Hermes、Cursor、自定义 Agent）**:
1. 让 Agent 读取 `skills/forex-strategy.md` + `skills/forex-review-workflow.md`
2. 告诉 Agent 运行 `python -m src.analysis.weekly_review` 或自己构造 prompt
3. 调用 Agent 的 CLI 传入拼接后的 prompt

**完全不用 LLM Agent**:
- 中控台 http://127.0.0.1:5001 提供完整 Web 界面
- `python -m src.daemon` 纯本地运行
- 信号自动生成，不需要 LLM 参与
