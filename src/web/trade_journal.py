"""交易日志 — 手动标注已推送信号的执行结果

用于记录: 是否手动跟单 / 最终扫止损还是止盈
数据存储在 data/trade_journal.json，独立于自动信号记录。
"""
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJ = Path(__file__).resolve().parent.parent.parent
JOURNAL_FILE = PROJ / "data" / "trade_journal.json"
SIGNALS_DIR = PROJ / "output" / "signals"
SH_TZ = timezone(timedelta(hours=8))

# ── 数据模型 ──

# 每条日志对应一个已推送的交易信号
# {
#   "signal_file": "20260730_151855_signal.json",
#   "signal_time": "2026-07-30 15:18 GMT+8",
#   "direction": "SELL",
#   "entry_price": 1.14428,
#   "stop_loss": 1.14728,
#   "take_profit_1": 1.13638,
#   "take_profit_2": 1.10004,
#   "confidence": 4,
#   "pattern": "阴吞阳",
#   # ── 手动标注 ──
#   "entered": null,           # true=已跟单 / false=未跟单
#   "entry_price_actual": null, # 实际成交价
#   "exit_result": null,       # "tp1" / "tp2" / "sl" / "manual" / "open"
#   "exit_price": null,
#   "exit_time": null,
#   "notes": "",
#   "annotated_at": null,      # 标注时间
# }


def load_signal_files(days: int = 30) -> List[Dict]:
    """加载最近 N 天所有 _signal.json 文件（仅交易信号，不含观望）"""
    signals = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    for f in sorted(SIGNALS_DIR.glob("*_signal.json"), reverse=True):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
            signals.append({
                "signal_file": f.name,
                "signal_time": data.get("timestamp", ""),
                "symbol": data.get("symbol", "EUR/USD"),
                "direction": _parse_direction(data.get("direction", "")),
                "entry_price": data.get("entry_price"),
                "stop_loss": data.get("stop_loss"),
                "take_profit_1": data.get("take_profit_1"),
                "take_profit_2": data.get("take_profit_2"),
                "confidence": data.get("confidence", 0),
                "rr_ratio": data.get("rr_ratio"),
                "entry_reason": data.get("entry_reason", ""),
                "sentiment_label": data.get("sentiment_label", ""),
                "file_mtime": mtime.isoformat(),
            })
        except Exception as e:
            logger.debug(f"读取信号文件失败 {f}: {e}")

    return signals


def _parse_direction(raw) -> str:
    """兼容方向表示: 做空/做多(中文) 或 SELL/BUY(英文)"""
    d = str(raw or "")
    if "做空" in d or d.strip().upper() == "SELL":
        return "SELL"
    return "BUY"


def load_journal() -> List[Dict]:
    """加载已有的交易日志"""
    if not JOURNAL_FILE.exists():
        return []
    try:
        return json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_journal(entries: List[Dict]):
    """保存交易日志"""
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def merge_signals_with_journal(signals: List[Dict]) -> List[Dict]:
    """把信号和已有日志合并，标注过的保留标注"""
    journal = load_journal()
    journal_map = {j.get("signal_file"): j for j in journal}

    merged = []
    for s in signals:
        key = s["signal_file"]
        if key in journal_map:
            j = journal_map[key]
            # 合并: 日志标注覆盖信号默认值（标注字段优先）
            defaults = {
                "entered": None, "entry_price_actual": None,
                "exit_result": None, "exit_price": None,
                "exit_time": None, "notes": "", "reflection": "",
                "annotated_at": None,
            }
            merged.append({**defaults, **s, **j})
        else:
            merged.append({
                **s,
                "entered": None, "entry_price_actual": None,
                "exit_result": None, "exit_price": None,
                "exit_time": None, "notes": "", "reflection": "",
                "annotated_at": None,
            })
    return merged


def update_annotation(signal_file: str, fields: Dict) -> Optional[Dict]:
    """更新或创建一条日志标注"""
    journal = load_journal()
    journal_map = {j.get("signal_file"): j for j in journal}

    if signal_file in journal_map:
        entry = journal_map[signal_file]
        entry.update(fields)
        entry["annotated_at"] = datetime.now(SH_TZ).isoformat()
    else:
        # 新建
        entry = {"signal_file": signal_file}
        entry.update(fields)
        entry["annotated_at"] = datetime.now(SH_TZ).isoformat()
        journal.append(entry)

    save_journal(journal)

    # ── 同步: 已平仓 → 关闭活跃持仓（停止飞书提醒）──
    # 仅 EUR/USD 走 PositionManager; 黄金由引擎自己结算, 不触碰 EUR 持仓
    exit_result = entry.get("exit_result")
    if exit_result and exit_result not in ("open", None):
        try:
            from src.strategy.position_manager import PositionManager
            pm = PositionManager()
            sig_path = SIGNALS_DIR / signal_file
            if sig_path.exists():
                sig_data = json.loads(sig_path.read_text(encoding="utf-8"))
                symbol = sig_data.get("symbol", "EUR/USD")
                if symbol != "EUR/USD":
                    logger.info(f"📝 {symbol} 标注{exit_result} → 不走 EUR 持仓同步")
                else:
                    direction = "SELL" if "做空" in str(sig_data.get("direction", "")) else "BUY"
                    pm.close_position(direction)
                    logger.info(f"📝 标注{exit_result} → 已同步关闭{direction}持仓")
        except Exception as e:
            logger.warning(f"同步持仓失败: {e}")

    return entry


def auto_settle(days: int = 30) -> int:
    """对未结算的 EUR/USD 信号用行情评估自动判定 SL/TP 并写回日志

    实盘持仓一直持有到 SL/TP 触发, 而日志标注经常滞后 —
    导致面板显示"持仓中/待标注"与实际不符。
    评估窗口延伸到数据末尾(与 review.py 修复一致)。

    Returns:
        本次自动结算的条目数
    """
    from src.strategy.review import ReviewEngine
    journal = load_journal()
    journal_map = {j.get("signal_file"): j for j in journal}
    rev = ReviewEngine()
    settled = 0

    for s in load_signal_files(days):
        j = journal_map.get(s["signal_file"])
        if j and j.get("exit_result"):
            continue  # 已结算(手动标注或此前自动结算)
        # 构造评估输入
        info = {
            "filename": s["signal_file"],
            "has_signal": True,
            "direction": s["direction"],
            "entry_zone_low": s["entry_price"],
            "entry_zone_high": s["entry_price"],
            "stop_loss": s["stop_loss"],
            "take_profit_1": s["take_profit_1"],
            "take_profit_2": s["take_profit_2"],
        }
        try:
            r = rev.evaluate_signal(info)
        except Exception as e:
            logger.debug(f"自动结算评估失败 {s['signal_file']}: {e}")
            continue
        if r.get("sl_hit") and s["stop_loss"]:
            result, price = "sl", s["stop_loss"]
        elif r.get("tp1_hit") and s["take_profit_1"]:
            result, price = "tp1", s["take_profit_1"]
        else:
            continue  # 确实未结算(还在持有) → 保持原样

        if j is None:
            j = {"signal_file": s["signal_file"]}
            journal.append(j)
        # 实际已平仓(行情评估判定了SL/TP) → entered 视为已跟单(可手动改)
        if j.get("entered") is None:
            j["entered"] = True
        j["exit_result"] = result
        j["exit_price"] = price
        j["exit_time"] = datetime.now(SH_TZ).isoformat()
        j["annotated_at"] = datetime.now(SH_TZ).isoformat()
        j["notes"] = "🤖 行情评估自动结算"
        logger.info(f"🤖 自动结算: {s['signal_file']} → {result}")
        settled += 1

    if settled:
        save_journal(journal)
    return settled


def get_summary() -> Dict:
    """获取交易日志统计摘要"""
    # 用合并数据(信号文件+日志标注): journal 条目本身不含 entry_price,
    # pips 统计需要信号文件的入场价
    merged = merge_signals_with_journal(load_signal_files(days=30))
    journal = merged
    entered = [j for j in journal if j.get("entered")]
    tp_hits = [j for j in entered if j.get("exit_result") in ("tp1", "tp2")]
    sl_hits = [j for j in entered if j.get("exit_result") == "sl"]
    open_trades = [j for j in entered if j.get("exit_result") == "open" or j.get("exit_result") is None]
    not_entered = [j for j in journal if j.get("entered") == False]

    # 计算盈亏 (EUR/USD 用 pips, 黄金 XAU/USD 用美元)
    total_pips = 0
    total_usd = 0
    win_count = 0
    loss_count = 0
    for j in entered:
        res = j.get("exit_result")
        if j.get("symbol") == "XAU/USD":
            if res in ("tp1", "tp2", "tp"):
                win_count += 1
                total_usd += j.get("pnl_usd") or 0
            elif res == "sl":
                loss_count += 1
                total_usd += j.get("pnl_usd") or 0
            continue
        if res in ("tp1", "tp2") and j.get("entry_price") and j.get("exit_price"):
            total_pips += abs(j["entry_price"] - j["exit_price"]) * 10000
            win_count += 1
        elif res == "sl" and j.get("entry_price") and j.get("exit_price"):
            total_pips -= abs(j["entry_price"] - j["exit_price"]) * 10000
            loss_count += 1

    return {
        "total_signals": len(journal),
        "entered": len(entered),
        "not_entered": len(not_entered),
        "tp_hits": len(tp_hits),
        "sl_hits": len(sl_hits),
        "open": len(open_trades),
        "total_pips": round(total_pips, 1),
        "total_usd": round(total_usd, 2),
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": round(win_count / max(win_count + loss_count, 1) * 100),
    }
