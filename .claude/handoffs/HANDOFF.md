# 项目交接 & 会话记录

> 本文件用于在异常退出后快速接上上下文。新会话先读本文件 + `CHATLOG.md`。
> 最后更新: 2026-08-21

---

## 最近关键结论（2026-08-21 会话）

### 黄金 (XAU/USD) 策略大改 ✅
**背景**: 用户黄金策略原本 = EUR/USD 六条件法去掉 RSI。回测发现**RSI 是关键过滤器**,去掉就亏。

| 版本 | 胜率 | 净盈亏/90天 |
|------|:---:|:---:|
| 无RSI + 固定$15 SL + RR3 | 20% | -$165 |
| **有RSI + ATR止损×2.5 + 固定TP$100** | **61%** | **+$560** |

**已提交改动** (`5c8a311`):
- `gold_entry.py`: 加 RSI 30-60,止损改 H4 ATR×2.5,止盈改固定 $100
- `config.yaml`: gold 段改为 `sl_atr_mult: 2.5`, `tp_usd: 100`, `rsi: {enabled: true, lo:30, hi:60}`
- `gold_engine.py`: 在途单结算用 `tp_usd`
- `dashboard.py`: 更新黄金配置 key
- `scripts/backtest_gold.py`: 黄金回测脚本(带RSI)

### GBP/USD 放弃 ❌
- 原意 = EUR/USD 去 RSI。回测全方向皆亏(固定SL/ATR止损都试过)。
- 已删除脚本和数据文件。**不要给 GBP/USD 做无RSI回测**——和黄金一样结论:去RSI没戏。

### 参数扫描发现(重要)
- 黄金 ATR ~$40,比 EUR/USD 大。用户原本固定$15止损太紧。
- 只有 ATR止损 + 固定TP 机制能转正,固定$15机制即使加RSI也亏(-$390)。

---

## 当前活跃配置

- **EUR/USD**: 主策略,6条件法(含RSI),回测83%胜率,月均+55pips。核心文件 `src/strategy/entry.py`, `signal.py`, `trend.py`。
- **黄金**: `gold_entry.py` + `gold_engine.py`,15分钟评估一次。
- **中控台**: http://127.0.0.1:5001

---

## 项目技术要点

- **配置**: `config/config.yaml` + `config.local.yaml`(敏感,不提交)。`dashboard_settings.json`(中控台在线配置,不提交)
- **OANDA**: 需走代理 `http://127.0.0.1:7897` + `follow_redirects=True`,否则 SSL 报错
- **数据**: `data/forex/{TF}/SYMBOL_TF.parquet` (gitignored)
- **指标库**: `src/analysis/indicators.py` (纯 pandas,无 ta-lib)
- **回测脚本**: `scripts/backtest_four_step.py`(EUR/USD), `backtest_gold.py`(黄金)
- **数据校验**: OANDA 数据校验会警告黄金价格"范围外"(因为黄金~$4500,校验区间按EUR/USD设),**属正常警告,可忽略**

---

## 常用命令

```bash
bash scripts/run_daemon.sh --bg     # 后台守护
bash scripts/run_daemon.sh --status
python scripts/backtest_four_step.py --days 90  # EUR/USD回测
python scripts/backtest_gold.py --days 90       # 黄金回测
```

---

## 待办/待决定
- [ ] 黄金实盘是否启用 RSI 新策略(用户需确认开启/enable)
- [ ] 用户在途单 `data/gold_state.json` 已 gitignore
- [ ] 项目 MEMORY 增强(见 .claude/ 后续)