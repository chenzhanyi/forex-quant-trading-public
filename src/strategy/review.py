"""复盘模块 — 信号回看 + 走势对比 + MAE分析

对比之前的交易信号与实际的 EUR/USD 走势，
评估信号质量、入场精度和风控表现。
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.data_collection.oanda import OandaClient
from src.analysis.indicators import Indicators
from src.strategy.trend import TrendAnalyzer
from src.utils.config_loader import config

logger = logging.getLogger(__name__)

# 东八区上海时间
SHANGHAI_TZ = timezone(timedelta(hours=8))


def now_shanghai() -> datetime:
    """获取当前上海时间"""
    return datetime.now(SHANGHAI_TZ)


class ReviewEngine:
    """复盘引擎"""

    def __init__(self):
        self.cfg = config.load()
        self.oanda = OandaClient()
        self.ind = Indicators()
        self.signals_dir = Path(self.cfg["output"]["signals_dir"])
        self.reviews_dir = Path(__file__).resolve().parent.parent.parent / "output" / "reviews"
        self.reviews_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "forex"

    def list_signals(self, days: int = 7) -> List[Dict]:
        """列出最近 N 天的信号文件（EUR/USD md + XAU/USD json）"""
        signals = []
        cutoff = datetime.now() - timedelta(days=days)

        # EUR/USD 信号(含观察): YYYYMMDD_HHMMSS_(signal|observe).md
        for f in sorted(self.signals_dir.glob("*_*.md")):
            if f.stat().st_mtime < cutoff.timestamp():
                continue
            m = re.match(r"(\d{8})_(\d{6})_(signal|observe)\.md", f.name)
            if m:
                signals.append({
                    "path": str(f),
                    "date": m.group(1),
                    "time": m.group(2),
                    "type": m.group(3),
                    "filename": f.name,
                    "timestamp": f.stat().st_mtime,
                    "symbol": "EUR/USD",
                })
        # 黄金信号: YYYYMMDD_HHMMSS_XAUUSD_signal.json
        for f in sorted(self.signals_dir.glob("*_XAUUSD_signal.json")):
            if f.stat().st_mtime < cutoff.timestamp():
                continue
            m = re.match(r"(\d{8})_(\d{6})_XAUUSD_signal\.json", f.name)
            if m:
                signals.append({
                    "path": str(f),
                    "date": m.group(1),
                    "time": m.group(2),
                    "type": "signal",
                    "filename": f.name,
                    "timestamp": f.stat().st_mtime,
                    "symbol": "XAU/USD",
                })
        return sorted(signals, key=lambda x: x["timestamp"], reverse=True)

    def parse_signal_file(self, path: str) -> Dict:
        """解析信号文件，提取关键信息

        优先读同名的 .json 文件（SignalGenerator.save() 同步生成），
        降级到正则解析 .md 文件（兼容旧数据）。
        """
        md_path = Path(path)
        json_path = md_path.with_suffix(".json")

        # 优先读结构化 JSON
        if json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                direction = "NONE"
                has_signal = data.get("can_trade", False)
                symbol = str(data.get("symbol", "EUR/USD"))
                if has_signal:
                    dir_str = data.get("direction", "")
                    if "做多" in dir_str or "BUY" in dir_str.upper():
                        direction = "BUY"
                    elif "做空" in dir_str or "SELL" in dir_str.upper():
                        direction = "SELL"
                return {
                    "path": path,
                    "filename": json_path.name,
                    "content": md_path.read_text(encoding="utf-8") if md_path.exists() else "",
                    "symbol": symbol,
                    "direction": direction,
                    "entry_zone_low": data.get("entry_price"),
                    "entry_zone_high": data.get("entry_price"),
                    "stop_loss": data.get("stop_loss"),
                    "take_profit_1": data.get("take_profit_1"),
                    "take_profit_2": data.get("take_profit_2"),
                    "has_signal": has_signal,
                    "_source": "json",
                }
            except Exception as e:
                logger.debug(f"JSON 解析失败，降级到 Markdown: {e}")

        # 降级：正则解析 Markdown（兼容旧版本信号文件）
        content = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        lines = content.split("\n")

        info = {
            "path": path,
            "content": content,
            "symbol": "EUR/USD",
            "direction": "NONE",  # BUY / SELL / 观望
            "entry_zone_low": None,
            "entry_zone_high": None,
            "stop_loss": None,
            "take_profit_1": None,
            "take_profit_2": None,
            "has_signal": False,
        }

        for line in lines:
            # 先检查观望（避免"谨慎做空"被误判为信号）
            if "建议: 观望" in line or "建议：观望" in line:
                info["direction"] = "NONE"
                info["has_signal"] = False
            elif "💡 建议: 观望" in line:
                info["direction"] = "NONE"
                info["has_signal"] = False
            elif "建仓建议" in line:
                # 明确的开仓信号
                if "做多" in line or "BUY" in line.upper():
                    info["direction"] = "BUY"
                    info["has_signal"] = True
                elif "做空" in line or "SELL" in line.upper():
                    info["direction"] = "SELL"
                    info["has_signal"] = True
            # 方向标记（🟢/🔴更可靠）
            if "🟢 做多" in line:
                info["direction"] = "BUY"
                info["has_signal"] = True
            elif "🔴 做空" in line:
                info["direction"] = "SELL"
                info["has_signal"] = True

            # 提取价格
            m = re.search(r"入场[：:]\s*([\d.]+)", line)
            if m:
                info["entry_zone_low"] = float(m.group(1))
                info["entry_zone_high"] = info["entry_zone_low"]

            m = re.search(r"关注区间[：:]\s*([\d.]+)\s*~\s*([\d.]+)", line)
            if m:
                info["entry_zone_low"] = float(m.group(1))
                info["entry_zone_high"] = float(m.group(2))

            m = re.search(r"止损[：:]\s*([\d.]+)", line)
            if m:
                info["stop_loss"] = float(m.group(1))

            m = re.search(r"TP1[：:]\s*([\d.]+)", line)
            if m:
                info["take_profit_1"] = float(m.group(1))

            m = re.search(r"TP2[：:]\s*([\d.]+)", line)
            if m:
                info["take_profit_2"] = float(m.group(1))

        info["_source"] = "markdown"
        return info

    def get_price_window(
        self, signal_time: str, hours_before: int = 12, hours_after: int = 48
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """获取信号前后的价格数据

        Args:
            signal_time: 信号时间 (YYYYMMDD_HHMMSS)
            hours_before: 信号前多少小时
            hours_after: 信号后多少小时

        Returns:
            (m15_df, h1_df) 包含指定窗口的价格数据
        """
        try:
            # 信号文件名是北京时间(GMT+8)，转 UTC 后与行情索引(UTC)对齐
            sig_dt = datetime.strptime(signal_time, "%Y%m%d_%H%M%S").replace(
                tzinfo=SHANGHAI_TZ
            ).astimezone(timezone.utc)
        except ValueError:
            sig_dt = datetime.now(timezone.utc)

        start = sig_dt - timedelta(hours=hours_before)
        end = sig_dt + timedelta(hours=hours_after)

        # 加载数据
        dfs = {}
        for tf, label in [("M15", "m15"), ("H1", "h1")]:
            filepath = self.data_dir / tf / f"EUR_USD_{tf}.parquet"
            if filepath.exists():
                df = pd.read_parquet(filepath)
                # 过滤时间窗口 — 使用完整 datetime 精度（老文件索引可能无时区）
                idx = pd.to_datetime(df.index)
                if idx.tz is None:
                    idx = idx.tz_localize("UTC")
                mask = (idx >= start) & (idx <= end)
                dfs[label] = df[mask]
            else:
                dfs[label] = pd.DataFrame()

        return dfs.get("m15", pd.DataFrame()), dfs.get("h1", pd.DataFrame())

    def _append_gold_result(self, lines: list, info: Dict, sig: Dict) -> None:
        """黄金信号: 显示入场/SL/TP + 交易日志里的结算结果

        行情评估(evaluate_signal)基于 EUR/USD parquet, 对黄金不适用;
        黄金结算以交易日志(trade_journal.json)为准。
        """
        entry = info.get("entry_zone_low")
        sl = info.get("stop_loss")
        tp1 = info.get("take_profit_1")
        if entry:
            lines.append(f"     💵 入场: {entry:.2f} | SL: {sl:.2f} | TP: {tp1:.2f}")
        try:
            import json
            journal_path = (Path(__file__).resolve().parent.parent.parent
                            / "data" / "trade_journal.json")
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            match = next(
                (j for j in journal if j.get("signal_file") == sig.get("filename")),
                None,
            )
            if match and match.get("exit_result"):
                res = match["exit_result"]
                pnl = match.get("pnl_usd")
                if res in ("tp", "tp1", "tp2"):
                    lines.append(
                        f"     ✅ 结果: 止盈"
                        + (f" (+${pnl:.2f})" if pnl is not None else "")
                    )
                elif res == "sl":
                    lines.append(
                        f"     ❌ 结果: 止损"
                        + (f" (${pnl:.2f})" if pnl is not None else "")
                    )
                else:
                    lines.append(f"     🟡 结果: {res}")
            else:
                lines.append("     🟡 结果: 持仓中/未标注")
        except Exception:
            lines.append("     🟡 结果: 交易日志不可读")

    def evaluate_signal(self, signal_info: Dict) -> Dict:
        """评估单个信号的质量

        Returns:
            {
                "signal_type": str,         # BUILD/SELL/NONE
                "signal_result": str,        # HIT / MISS / UNKNOWN
                "max_price_reached": float,  # 信号后最高价
                "min_price_reached": float,  # 信号后最低价
                "price_now": float,          # 当前价
                "mae_pips": float,           # 最大向不利变动
                "mfe_pips": float,           # 最大向有利变动
                "entry_found": bool,         # 价格是否进入入场区
                "sl_hit": bool,              # 是否打止损
                "tp1_hit": bool,             # 是否到TP1
                "tp2_hit": bool,             # 是否到TP2
                "days_held": float,          # 至今持有天数
            }
        """
        if not signal_info["has_signal"]:
            return {"signal_type": "NONE", "signal_result": "N/A"}

        sig_time = signal_info.get("filename", "").split("_")[0] + "_" + \
                   signal_info.get("filename", "").split("_")[1]
        # 评估窗口延伸到数据末尾(实盘持仓会一直持有直到SL/TP触发,
        # 固定48h窗口会把"48h后才扫损"的订单误判为持仓中)
        m15, h1 = self.get_price_window(sig_time, hours_after=720)

        if h1.empty or len(h1) < 2:
            return {"signal_type": signal_info["direction"], "signal_result": "UNKNOWN",
                    "reason": "数据不足"}

        direction = signal_info["direction"]
        entry_low = signal_info.get("entry_zone_low")
        entry_high = signal_info.get("entry_zone_high")
        sl = signal_info.get("stop_loss")
        tp1 = signal_info.get("take_profit_1")
        tp2 = signal_info.get("take_profit_2")

        # 信号后的 H1 数据：严格以信号时刻(北京时间→UTC)为界，排除信号前行情
        try:
            sig_dt_utc = datetime.strptime(sig_time, "%Y%m%d_%H%M%S").replace(
                tzinfo=SHANGHAI_TZ
            ).astimezone(timezone.utc)
        except ValueError:
            sig_dt_utc = None
        if sig_dt_utc is not None:
            idx = pd.to_datetime(h1.index)
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            after_signal = h1[idx > sig_dt_utc]
        else:
            after_signal = h1

        if after_signal.empty:
            return {"signal_type": direction, "signal_result": "PENDING",
                    "reason": "信号刚发出，尚无后续数据"}

        max_price = float(after_signal["high"].max())
        min_price = float(after_signal["low"].min())
        latest_price = float(after_signal["close"].iloc[-1])

        result = {
            "signal_type": direction,
            "signal_result": "PENDING",
            "max_price": max_price,
            "min_price": min_price,
            "price_now": latest_price,
            "entry_found": False,
            "sl_hit": False,
            "tp1_hit": False,
            "tp2_hit": False,
        }

        if direction == "BUY":
            # 入场区间检查：价格区间必须与入场区有重叠
            if entry_low and entry_high:
                result["entry_found"] = (
                    min_price <= entry_high and max_price >= entry_low
                )

            # 止损检查
            if sl:
                result["sl_hit"] = min_price <= sl
                result["mae_pips"] = round((entry_low - min_price) * 10000, 1) if entry_low else 0

            # 止盈检查
            if tp1:
                result["tp1_hit"] = max_price >= tp1
            if tp2:
                result["tp2_hit"] = max_price >= tp2

            # MAE / MFE
            result["mae_pips"] = round((float(entry_low or after_signal["close"].iloc[0])
                                        - min_price) * 10000, 1)
            result["mfe_pips"] = round((max_price - float(entry_low or after_signal["close"].iloc[0]))
                                       * 10000, 1)

            # 结果判定
            if result["tp1_hit"]:
                result["signal_result"] = "HIT"
                result["reason"] = f"✅ 到 TP1 (最高 {max_price:.5f})"
            elif result["sl_hit"]:
                result["signal_result"] = "MISS"
                result["reason"] = f"❌ 打止损 (最低 {min_price:.5f})"
            elif latest_price > float(after_signal["close"].iloc[0]):
                result["signal_result"] = "HOLDING"
                result["reason"] = f"🟢 浮盈中 (当前 {latest_price:.5f})"
            else:
                result["signal_result"] = "HOLDING"
                result["reason"] = f"🟡 浮亏中 (当前 {latest_price:.5f})"

        else:  # SELL
            if entry_low and entry_high:
                result["entry_found"] = (
                    min_price <= entry_high and max_price >= entry_low
                )

            if sl:
                result["sl_hit"] = max_price >= sl

            if tp1:
                result["tp1_hit"] = min_price <= tp1
            if tp2:
                result["tp2_hit"] = min_price <= tp2

            result["mae_pips"] = round((max_price - float(entry_low or after_signal["close"].iloc[0]))
                                       * 10000, 1)
            result["mfe_pips"] = round((float(entry_low or after_signal["close"].iloc[0])
                                        - min_price) * 10000, 1)

            if result["tp1_hit"]:
                result["signal_result"] = "HIT"
                result["reason"] = f"✅ 到 TP1 (最低 {min_price:.5f})"
            elif result["sl_hit"]:
                result["signal_result"] = "MISS"
                result["reason"] = f"❌ 打止损 (最高 {max_price:.5f})"
            elif latest_price < float(after_signal["close"].iloc[0]):
                result["signal_result"] = "HOLDING"
                result["reason"] = f"🟢 浮盈中 (当前 {latest_price:.5f})"
            else:
                result["signal_result"] = "HOLDING"
                result["reason"] = f"🟡 浮亏中 (当前 {latest_price:.5f})"

        # 持有时间（信号时间为北京时间）
        try:
            sig_dt_sh = datetime.strptime(sig_time, "%Y%m%d_%H%M%S").replace(
                tzinfo=SHANGHAI_TZ
            )
            result["days_held"] = round(
                (datetime.now(SHANGHAI_TZ) - sig_dt_sh).total_seconds() / 86400, 1
            )
        except ValueError:
            result["days_held"] = 0

        return result

    def get_historical_trend(self, days_ago: int = 7) -> Dict:
        """回看历史趋势

        获取指定天数前的 D1+H4 50SMA 状态，与当前对比
        """
        try:
            df_d1 = self.oanda.load_parquet("D1")
            df_h4 = self.oanda.load_parquet("H4")
        except Exception:
            return {"error": "无法加载历史数据"}

        # D1 历史趋势
        df_d1 = self.ind.add_sma(df_d1, 50)
        d1_history = []
        for i in range(min(len(df_d1), days_ago), 0, -1):
            idx = -(i + 1)
            if abs(idx) <= len(df_d1):
                slice_df = df_d1.iloc[:idx + 1] if idx < 0 else df_d1
            else:
                continue
            d1_history.append({
                "date": df_d1.index[idx].strftime("%m-%d") if hasattr(df_d1.index[idx], 'strftime') else str(df_d1.index[idx]),
                "price": round(float(df_d1["close"].iloc[idx]), 5),
                "sma50": round(float(df_d1["SMA50"].iloc[idx]), 5),
                "direction": self.ind.get_ma_direction(
                    df_d1.iloc[:idx + 1] if idx < 0 else df_d1, "SMA50", 3
                ),
            })

        # H4 历史趋势（200 SMA）
        df_h4 = self.ind.add_sma(df_h4, 200)
        h4_history = []
        for i in range(min(len(df_h4), days_ago * 6), 0, -1):
            idx = -(i + 1)
            if abs(idx) <= len(df_h4):
                h4_history.append({
                    "date": df_h4.index[idx].strftime("%m-%d %H:00") if hasattr(df_h4.index[idx], 'strftime') else str(df_h4.index[idx]),
                    "price": round(float(df_h4["close"].iloc[idx]), 5),
                    "sma200": round(float(df_h4["SMA200"].iloc[idx]), 5),
                    "direction": self.ind.get_ma_direction(
                        df_h4.iloc[:idx + 1] if idx < 0 else df_h4, "SMA200", 3
                    ),
                })

        return {
            "d1": d1_history,
            "h4": h4_history,
            "current_d1": d1_history[-1] if d1_history else None,
            "current_h4": h4_history[-1] if h4_history else None,
        }

    def generate_review(self, days: int = 7) -> str:
        """生成完整复盘报告"""
        lines = []
        lines.append("─" * 50)
        lines.append(f"📋 EUR/USD 复盘报告（近 {days} 天）")
        lines.append(f"   生成时间: {now_shanghai().strftime('%Y-%m-%d %H:%M')} GMT+8")
        lines.append("─" * 50)

        # 1. 历史趋势
        lines.append("")
        lines.append("📈 历史趋势回顾")
        lines.append("─" * 40)

        hist = self.get_historical_trend(days)
        if "error" not in hist:
            # D1 趋势变化
            d1_dirs = [h["direction"] for h in hist["d1"]]
            unique_dirs = []
            for d in d1_dirs:
                if not unique_dirs or d != unique_dirs[-1]:
                    unique_dirs.append(d)
            lines.append(f"  D1 50SMA: {' → '.join(unique_dirs)}")
            lines.append(f"  最新: {hist['current_d1']['date']} "
                         f"价={hist['current_d1']['price']:.5f} "
                         f"SMA50={hist['current_d1']['sma50']:.5f} [{hist['current_d1']['direction']}]")

            h4_dirs = [h["direction"] for h in hist["h4"]]
            unique_h4 = []
            for d in h4_dirs:
                if not unique_h4 or d != unique_h4[-1]:
                    unique_h4.append(d)
            lines.append(f"  H4 200SMA: {' → '.join(unique_h4)}")
            lines.append(f"  最新: {hist['current_h4']['date']} "
                         f"价={hist['current_h4']['price']:.5f} "
                         f"SMA200={hist['current_h4']['sma200']:.5f} [{hist['current_h4']['direction']}]")
        else:
            lines.append(f"  {hist['error']}")

        # 2. 信号回顾
        lines.append("")
        lines.append("📊 信号回顾")
        lines.append("─" * 40)

        signals = self.list_signals(days)
        if not signals:
            lines.append("  近 {days} 天无信号记录")
        else:
            hit_count = 0
            miss_count = 0
            pending_count = 0
            total = 0

            for sig in signals:
                info = self.parse_signal_file(sig["path"])
                # 跳过观望，只显示交易信号
                if not info["has_signal"]:
                    continue
                symbol = info.get("symbol", "EUR/USD")
                sym_tag = "🥇 XAU/USD" if symbol == "XAU/USD" else "💶 EUR/USD"

                lines.append("")
                lines.append(f"  ⏰ {sig['date']} {sig['time'][:2]}:{sig['time'][2:4]} GMT+8 {sym_tag} 信号")

                if symbol == "XAU/USD":
                    # 黄金信号: 行情评估基于 EURUSD 数据不适用 — 显示交易日志结算
                    self._append_gold_result(lines, info, sig)
                    continue

                eval_result = self.evaluate_signal(info)
                total += 1

                result = eval_result.get("signal_result", "UNKNOWN")
                if result == "HIT":
                    hit_count += 1
                    lines.append(f"     {'✅' if eval_result.get('tp1_hit') else '🟢'} "
                                 f"结果: {eval_result.get('reason', '命中')}")
                elif result == "MISS":
                    miss_count += 1
                    lines.append(f"     ❌ 结果: {eval_result.get('reason', '未命中')}")
                else:
                    pending_count += 1
                    lines.append(f"     🟡 结果: {eval_result.get('reason', '进行中')}")

                if eval_result.get("mae_pips"):
                    lines.append(f"     📉 MAE: -{eval_result['mae_pips']} pips "
                                 f"| MFE: +{eval_result.get('mfe_pips', 0)} pips")
                if eval_result.get("entry_found"):
                    lines.append(f"     📌 入场区间命中 ✅")
                if eval_result.get("days_held"):
                    lines.append(f"     📆 持有: {eval_result['days_held']} 天")

            # 统计汇总(EUR/USD)
            lines.append("")
            lines.append("  ── EUR/USD 统计 ──")
            if total > 0:
                hit_rate = (hit_count / total * 100) if hit_count > 0 else 0
                lines.append(f"  总信号: {total}")
                if hit_count + miss_count > 0:
                    resolved = hit_count + miss_count
                    lines.append(f"  已结算: {resolved} (命中率 {hit_count}/{resolved} = "
                                 f"{hit_count/resolved*100:.0f}%)")
                lines.append(f"  进行中: {pending_count}")
            else:
                lines.append("  近 N 天无 EUR/USD 交易信号")

        # 3. 价格区间
        lines.append("")
        lines.append("💰 价格区间")
        lines.append("─" * 40)
        try:
            df_d1 = self.oanda.load_parquet("D1")
            recent = df_d1.iloc[-days:]
            lines.append(f"  近 {days} 天 EUR/USD 波动区间:")
            lines.append(f"    最高: {recent['high'].max():.5f}")
            lines.append(f"    最低: {recent['low'].min():.5f}")
            lines.append(f"    开盘: {recent['open'].iloc[0]:.5f}")
            lines.append(f"    收盘: {recent['close'].iloc[-1]:.5f}")
            lines.append(f"    波幅: {(recent['high'].max() - recent['low'].min()) * 10000:.0f} pips")
        except Exception as e:
            lines.append(f"  数据获取失败: {e}")

        lines.append("")
        lines.append("─" * 50)
        lines.append("📌 明日展望")
        lines.append("─" * 50)
        from src.strategy.trend import TrendAnalyzer
        t = TrendAnalyzer()
        r = t.analyze()
        lines.append(f"  {r.get('reason', '分析中')}")
        if r.get("can_trade"):
            lines.append("  ✅ 方向明确，继续关注入场机会")
        else:
            lines.append("  ⏸ 趋势分歧，观望为主")

        lines.append("─" * 50)

        return "\n".join(lines)

    def save_review(self, days: int = 7) -> Path:
        """保存复盘报告"""
        report = self.generate_review(days)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.reviews_dir / f"review_{timestamp}_{days}d.md"
        path.write_text(report, encoding="utf-8")
        logger.info(f"✅ 复盘报告已保存: {path}")
        return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    engine = ReviewEngine()
    print(engine.generate_review(days=7))
    engine.save_review(days=7)
