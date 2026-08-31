"""黄金 XAU/USD 交易引擎 — 独立于 EURUSD

- 15分钟评估一次
- 在途单量上限控制
- 中控台开关控制是否接入 MT4 自动下单
"""
import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("gold")

PROJ = Path(__file__).resolve().parent.parent.parent
GOLD_STATE_FILE = PROJ / "data" / "gold_state.json"
SIGNALS_DIR = PROJ / "output" / "signals"   # 与 EURUSD 共用, 供中控台交易日志
SH_TZ = timezone(timedelta(hours=8))

EVAL_INTERVAL = 15 * 60  # 15分钟


def _log_push(tag: str, msg: str) -> None:
    """黄金推送监控日志(与 engine._push_feishu 共用 feishu_push.log)"""
    try:
        first_line = (msg or "").split("\n")[0][:60]
        with open(PROJ / "data" / "feishu_push.log", "a", encoding="utf-8") as f:
            f.write(
                f"{datetime.now(SH_TZ).strftime('%Y-%m-%d %H:%M:%S')} "
                f"| gold.{tag} | {first_line}\n"
            )
    except Exception:
        pass


class GoldEngine:
    """黄金交易引擎"""

    def __init__(self):
        self._running = False
        self._eval_count = 0
        self._open_trades = []  # 在途订单
        self._last_eval_time = None
        # 熔断状态: 同方向连续止损 N 次 → 暂停该方向(回测75天: 熔断2连SL 收益翻倍 49%→71%)
        self._fuse = {
            "loss_streak": {"SELL": 0, "BUY": 0},
            "paused_until": {"SELL": None, "BUY": None},
        }
        self._load_state()

    # ── 状态持久化 ──

    def _load_state(self):
        if GOLD_STATE_FILE.exists():
            try:
                data = json.loads(GOLD_STATE_FILE.read_text(encoding="utf-8"))
                self._open_trades = data.get("open_trades", [])
                saved_fuse = data.get("fuse")
                if saved_fuse:
                    self._fuse = {
                        "loss_streak": {**self._fuse["loss_streak"],
                                        **saved_fuse.get("loss_streak", {})},
                        "paused_until": {**self._fuse["paused_until"],
                                         **saved_fuse.get("paused_until", {})},
                    }
            except Exception:
                self._open_trades = []

    def _save_state(self):
        GOLD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = GOLD_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"open_trades": self._open_trades, "fuse": self._fuse},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(GOLD_STATE_FILE)  # 原子替换，防写一半崩溃

    # ── 配置 ──

    @property
    def gold_cfg(self):
        # 中控台设置覆盖 YAML 兜底(合并模式: 面板只设置改过的键, YAML 提供默认值)
        from src.utils.dashboard_settings import load
        from src.utils.config_loader import config
        yaml_cfg = config.load().get("gold", {}) or {}
        ui = load().get("gold", {}) or {}
        return {**yaml_cfg, **ui}

    @property
    def enabled(self) -> bool:
        return bool(self.gold_cfg.get("enabled", False))

    @property
    def auto_trade(self) -> bool:
        return bool(self.gold_cfg.get("auto_trade", False))

    @property
    def max_open(self) -> int:
        return int(self.gold_cfg.get("max_open", 2))

    @property
    def fuse_enabled(self) -> bool:
        return bool(self.gold_cfg.get("fuse_enabled", True))

    @property
    def fuse_max_streak(self) -> int:
        return int(self.gold_cfg.get("fuse_max_loss_streak", 2))

    @property
    def fuse_cooldown_hours(self) -> int:
        return int(self.gold_cfg.get("fuse_cooldown_hours", 48))

    # ── 熔断 ──

    def _is_fused(self, direction: str) -> bool:
        """该方向是否处于熔断暂停期(冷却到期自动解除)"""
        if not self.fuse_enabled:
            return False
        pu = self._fuse["paused_until"].get(direction)
        if not pu:
            return False
        try:
            until = datetime.fromisoformat(pu)
        except Exception:
            return False
        if datetime.now(SH_TZ) >= until:
            self._fuse["paused_until"][direction] = None
            self._fuse["loss_streak"][direction] = 0
            self._save_state()
            logger.info(f"🥇 熔断冷却结束: {direction} 恢复交易")
            return False
        return True

    def _record_fuse_result(self, direction: str, is_loss: bool) -> None:
        """结算后更新熔断计数: 连续止损达到阈值 → 暂停该方向"""
        if not self.fuse_enabled:
            return
        if is_loss:
            streak = self._fuse["loss_streak"].get(direction, 0) + 1
            self._fuse["loss_streak"][direction] = streak
            if streak >= self.fuse_max_streak:
                self._fuse["paused_until"][direction] = (
                    datetime.now(SH_TZ) + timedelta(hours=self.fuse_cooldown_hours)
                ).isoformat()
                self._fuse["loss_streak"][direction] = 0
                logger.info(
                    f"🥇 熔断触发: {direction} 连续{streak}次止损 → "
                    f"暂停该方向 {self.fuse_cooldown_hours} 小时"
                )
                try:
                    push_script = PROJ / "scripts" / "feishu_push.sh"
                    import subprocess
                    _fuse_msg = (f"🥇 黄金熔断提醒\n{'─'*20}\n"
                                 f"方向 {direction} 连续 {streak} 次止损\n"
                                 f"→ 已暂停该方向 {self.fuse_cooldown_hours} 小时\n"
                                 f"(防趋势反转期连续亏损, 回测验证收益翻倍)")
                    _log_push("fuse", _fuse_msg)
                    subprocess.run(
                        ["bash", str(push_script), _fuse_msg],
                        capture_output=True, text=True, timeout=30)
                except Exception:
                    pass
        else:
            self._fuse["loss_streak"][direction] = 0
        self._save_state()

    # ── 评估循环 ──

    def evaluate(self) -> dict:
        """评估一次黄金信号"""
        from src.strategy.gold_entry import GoldEntryDetector
        self._eval_count += 1
        detector = GoldEntryDetector()

        # 结算在途订单（复用同一个 detector 的 M15 数据）
        self._settle_open_trades(detector)

        if not self.enabled:
            return {"enabled": False, "eval": self._eval_count}

        # 重大数据黑洞窗口（非农等）→ 跳过新信号, 但结算照常
        try:
            from src.utils.trade_blackout import is_blacked_out
            blacked, reason = is_blacked_out()
            if blacked:
                logger.info(f"🥇 {reason} → 跳过评估")
                return {"enabled": True, "signal": None, "blackout": True,
                        "reason": reason, "open": len(self._open_trades),
                        "eval": self._eval_count}
        except Exception as e:
            logger.debug(f"黄金数据黑洞检查失败: {e}")

        sig = detector.detect()
        if sig is None:
            return {"enabled": True, "signal": None, "open": len(self._open_trades),
                    "eval": self._eval_count}

        # 熔断检查: 同方向连续止损后暂停(防趋势反转期连亏)
        if self._is_fused(sig.direction):
            logger.info(f"🥇 {sig.direction} 处于熔断暂停期，跳过新信号")
            return {"enabled": True, "signal": sig.__dict__, "skipped": "fused",
                    "open": len(self._open_trades), "eval": self._eval_count}

        # 单量限制
        if len(self._open_trades) >= self.max_open:
            logger.info(f"🥇 单量已达上限({self.max_open})，跳过新信号")
            return {"enabled": True, "signal": sig.__dict__, "skipped": "max_open",
                    "open": len(self._open_trades), "eval": self._eval_count}

        # 记录在途
        trade = {
            "direction": sig.direction,
            "entry": sig.entry_price,
            "sl": sig.stop_loss,
            "tp": sig.take_profit,
            "sl_usd": sig.sl_usd,
            "tp_usd": sig.tp_usd,
            "pattern": sig.pattern,
            "time": datetime.now(SH_TZ).isoformat(),
        }
        self._open_trades.append(trade)
        self._save_state()

        # 落盘信号文件(供中控台交易日志 + 手动标注)
        sig_file = self._write_signal_file(sig, trade)
        if sig_file:
            trade["signal_file"] = sig_file
            self._save_state()

        # 飞书推送
        try:
            self._push_signal(sig)
        except Exception as e:
            logger.warning(f"黄金推送失败: {e}")

        # MT4 自动下单
        mt4_result = None
        if self.auto_trade:
            mt4_result = self._mt4_order(sig)
            trade["mt4"] = bool((mt4_result or {}).get("success"))
            # 存 MT4 ticket — 保本锁定时用于同步修改 SL
            ticket = ((mt4_result or {}).get("result") or {}).get("ticket")
            if ticket:
                trade["ticket"] = int(ticket)

        return {"enabled": True, "signal": trade, "open": len(self._open_trades),
                "eval": self._eval_count, "mt4": mt4_result}

    def _settle_open_trades(self, detector=None):
        """用最新价格结算在途订单

        同一 bar 内 SL/TP 都被触发时，用 K 线方向判断先后：
          - 阴线(close<open): 价格先跌后涨 → 下方先触发
          - 阳线(close>open): 价格先涨后跌 → 上方先触发
        """
        if detector is None:
            from src.strategy.gold_entry import GoldEntryDetector
            detector = GoldEntryDetector()
        m15 = detector._prepare_m15()
        if m15 is None or not self._open_trades:
            return
        bar = m15.iloc[-1]
        hi = float(bar['high']); lo = float(bar['low'])
        op = float(bar['open']); cl = float(bar['close'])
        bearish = cl < op  # 阴线: 先跌后涨, 下方先触发
        bullish = cl > op  # 阳线: 先涨后跌, 上方先触发

        # 保本止损: 浮盈≥阈值 → SL 移到入场价(回测75天: 熔断2+保本$50 = 75%胜率)
        be_trigger = float(self.gold_cfg.get("be_trigger_usd", 0))
        if be_trigger > 0:
            self._apply_breakeven(cl, be_trigger)

        remaining = []
        for t in self._open_trades:
            sl_hit = (t["direction"] == "SELL" and hi >= t["sl"]) or \
                     (t["direction"] == "BUY" and lo <= t["sl"])
            tp_hit = (t["direction"] == "SELL" and lo <= t["tp"]) or \
                     (t["direction"] == "BUY" and hi >= t["tp"])

            if not sl_hit and not tp_hit:
                remaining.append(t)
                continue

            # 同一 bar 内 SL/TP 都触发 → 用 K 线方向判断先后
            if sl_hit and tp_hit:
                if t["direction"] == "SELL":
                    # SELL: SL在上方, TP在下方
                    sl_first = bullish   # 阳线先涨 → 上方先触发 → SL先
                else:
                    # BUY: SL在下方, TP在上方
                    sl_first = bearish   # 阴线先跌 → 下方先触发 → SL先
            else:
                sl_first = sl_hit

            if sl_first:
                if t.get("be_locked"):
                    logger.info(f"🥇 保本出场: {t['direction']} @{t['entry']} 打平(±$0)")
                else:
                    logger.info(f"🥇 止损: {t['direction']} @{t['entry']} → -${t['sl_usd']}")
            else:
                profit = t.get('tp_usd', t['sl_usd'] * 2.5)
                logger.info(f"🥇 止盈: {t['direction']} @{t['entry']} → +${profit}")

            # 把进出场结果写回交易日志(signal 文件 + journal)
            self._finalize_trade(t, sl_first)

        if len(remaining) != len(self._open_trades):
            self._open_trades = remaining
            self._save_state()

    def _apply_breakeven(self, current_price: float, trigger_usd: float) -> None:
        """保本止损: 浮盈≥trigger_usd 的在途单 → SL 移到入场价±容差(一次性)

        容差(be_lock_buffer_points, 黄金0.1美元=1点): 锁定在盈利侧,
        覆盖不同平台滑点/点差导致的报价差异 — 打平结算时保留微利。
        回测75天: 保本$50单独+$186; 与熔断组合后 75%胜率 +$2425
        """
        buf_points = float(self.gold_cfg.get("be_lock_buffer_points", 0))
        buf_usd = round(buf_points * 0.1, 2)  # 黄金 0.1 美元 = 1 点
        changed = False
        for t in self._open_trades:
            if t.get("be_locked"):
                continue
            direction = t["direction"]
            entry = t["entry"]
            profit = (entry - current_price) if direction == "SELL" else (current_price - entry)
            if profit < trigger_usd:
                continue
            # 做多: SL 锁在入场价上方(盈利侧); 做空: SL 锁在入场价下方
            lock_sl = entry + buf_usd if direction == "BUY" else entry - buf_usd
            t["sl"] = round(lock_sl, 2)
            t["be_locked"] = True
            t["be_buffer_usd"] = buf_usd
            changed = True
            logger.info(
                f"🥇 保本锁定: {direction} @{entry:.2f} "
                f"浮盈${profit:.0f}≥${trigger_usd:.0f} → SL移到{lock_sl:.2f}"
                + (f" (入场价+{buf_points:.0f}点容差)" if buf_usd > 0 else "")
            )
            try:
                push_script = PROJ / "scripts" / "feishu_push.sh"
                import subprocess
                _be_msg = (f"🥇 黄金保本锁定\n{'─'*20}\n"
                           f"方向 {direction} @{entry:.2f}\n"
                           f"浮盈 ${profit:.0f} → SL 已移到 {lock_sl:.2f}"
                           + (f" (入场价+{buf_points:.0f}点容差)\n" if buf_usd > 0 else "\n")
                           + f"后续回撤最多打平, 不亏本")
                _log_push("breakeven", _be_msg)
                subprocess.run(
                    ["bash", str(push_script), _be_msg],
                    capture_output=True, text=True, timeout=30)
            except Exception:
                pass
            # MT4 同步修改 SL(自动交易且已知 ticket 时)
            if self.auto_trade and t.get("ticket"):
                self._mt4_modify_sl(t)
        if changed:
            self._save_state()

    def _mt4_modify_sl(self, t) -> None:
        """MT4 修改止损(保本锁定同步)"""
        try:
            from src.utils.dashboard_settings import load
            cfg = load().get("mt4_relay", {})
            url = cfg.get("url", "").rstrip("/")
            if not url or not cfg.get("enabled", False):
                return
            import httpx
            resp = httpx.post(f"{url}/modify", json={
                "ticket": int(t["ticket"]),
                "sl": round(t["sl"], 2),
                "tp": round(t["tp"], 2),
            }, timeout=15)
            logger.info(f"🥇 MT4保本修改: {resp.json().get('success')}")
        except Exception as e:
            logger.warning(f"MT4保本修改失败(可手动改SL): {e}")

    def _push_signal(self, sig):
        proj_dir = PROJ
        push_script = proj_dir / "scripts" / "feishu_push.sh"
        import subprocess
        d = "做空" if sig.direction == "SELL" else "做多"
        msg = (
            f"🥇 黄金入场信号!\n"
            f"{'─'*20}\n"
            f"⏰ {datetime.now(SH_TZ).strftime('%H:%M')} GMT+8\n\n"
            f"💡 {d} @ {sig.entry_price:.2f}\n"
            f"🛑 止损: {sig.stop_loss:.2f} (-${sig.sl_usd})\n"
            f"🎯 止盈: {sig.take_profit:.2f} (+${sig.tp_usd})\n"
            f"📐 R:R {sig.rr_ratio:.1f} | 形态: {sig.pattern}\n"
            f"📊 在途: {len(self._open_trades)}/{self.max_open}"
        )
        _log_push("signal", msg)
        subprocess.run(["bash", str(push_script), msg],
                       capture_output=True, text=True, timeout=30)

    def _mt4_order(self, sig):
        """通过 MT4 relay 下黄金单"""
        try:
            from src.utils.dashboard_settings import load
            cfg = load().get("mt4_relay", {})
            url = cfg.get("url", "").rstrip("/")
            if not url or not cfg.get("enabled", False):
                return {"skipped": "mt4_not_enabled"}
            import httpx
            lots = float(self.gold_cfg.get("lots", 0.01))
            resp = httpx.post(f"{url}/order", json={
                "direction": sig.direction,
                "symbol": "XAUUSD",
                "volume": lots,
                "sl": round(sig.stop_loss, 2),
                "tp": round(sig.take_profit, 2),
                "comment": "gold_auto",
            }, timeout=15)
            result = resp.json()
            if result.get("success"):
                logger.info(f"🥇 MT4黄金下单成功: {result.get('order_id')}")
            else:
                logger.warning(f"🥇 MT4黄金下单失败: {result.get('error')}")
                # 失败回滚
                if self._open_trades:
                    self._open_trades.pop()
                    self._save_state()
            return result
        except Exception as e:
            logger.warning(f"MT4黄金下单异常: {e}")
            if self._open_trades:
                self._open_trades.pop()
                self._save_state()
            return {"error": str(e)}

    # ── 交易日志落盘(供中控台)──

    def _write_signal_file(self, sig, trade):
        """出信号时写一个 output/signals/*_XAUUSD_signal.json"""
        try:
            SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
            d = "做空" if sig.direction == "SELL" else "做多"
            ts = datetime.now(SH_TZ).strftime("%Y%m%d_%H%M%S")
            name = f"{ts}_XAUUSD_signal.json"
            data = {
                "timestamp": datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M GMT+8"),
                "symbol": "XAU/USD",
                "can_trade": True,
                "direction": d,
                "entry_price": sig.entry_price,
                "stop_loss": sig.stop_loss,
                "take_profit_1": sig.take_profit,
                "sl_usd": sig.sl_usd,
                "tp_usd": sig.tp_usd,
                "rr_ratio": sig.rr_ratio,
                "pattern": sig.pattern,
                "rsi": getattr(sig, "rsi", None),
                "sl_method": "ATR2.5x",
                "holding_advice": (f"🥇 黄金{d} @ {sig.entry_price:.2f}\n"
                                   f"🛑 止损: {sig.stop_loss:.2f} (-${sig.sl_usd})\n"
                                   f"🎯 止盈: {sig.take_profit:.2f} (+${sig.tp_usd})\n"
                                   f"📐 R:R {sig.rr_ratio:.1f} | 形态: {sig.pattern}"),
            }
            (SIGNALS_DIR / name).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return name
        except Exception as e:
            logger.warning(f"黄金信号文件写入失败: {e}")
            return None

    def _finalize_trade(self, t, sl_first):
        """订单结算后, 把结果写回信号文件 + trade_journal, 供面板复盘"""
        if t.get("be_locked") and sl_first:
            # 保本锁定后打平: 结算保留锁定微利(容差部分)
            t["exit_result"] = "be"
            t["exit_price"] = t.get("sl", t["entry"])
            t["pnl_usd"] = round(t.get("be_buffer_usd", 0), 2)
        else:
            t["exit_result"] = "sl" if sl_first else "tp"
            t["exit_price"] = t.get("sl") if sl_first else t.get("tp")
            t["pnl_usd"] = round(-t.get("sl_usd", 0), 2) if sl_first \
                else round(t.get("tp_usd", t.get("sl_usd", 0) * 2.5), 2)
        t["exit_time"] = datetime.now(SH_TZ).isoformat()

        # 熔断计数(结算即更新, 与日志落盘解耦)
        self._record_fuse_result(t.get("direction", ""), sl_first)

        sig_file = t.get("signal_file")
        if not sig_file:
            return
        self._update_signal_file(sig_file, t)
        self._update_journal(sig_file, t)

    def _update_signal_file(self, sig_file, t):
        """把结算结果写回黄金信号文件"""
        try:
            p = SIGNALS_DIR / sig_file
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            data["exit_result"] = t["exit_result"]
            data["exit_price"] = t["exit_price"]
            data["exit_time"] = t["exit_time"]
            data["pnl_usd"] = t["pnl_usd"]
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        except Exception as e:
            logger.warning(f"黄金信号文件更新失败: {e}")

    def _update_journal(self, sig_file, t):
        """在 trade_journal.json 写入/补一条黄金结算记录(不动 EURUSD 持仓)"""
        try:
            from src.web.trade_journal import load_journal, save_journal
            journal = load_journal()
            entry = next((j for j in journal if j.get("signal_file") == sig_file), None)
            if entry is None:
                entry = {"signal_file": sig_file}
                journal.append(entry)
            entry.update({
                "symbol": "XAU/USD",
                "entered": True if t.get("mt4") else None,
                "exit_result": t["exit_result"],
                "exit_price": t["exit_price"],
                "exit_time": t["exit_time"],
                "pnl_usd": t["pnl_usd"],
                "annotated_at": t["exit_time"],
                "notes": "🥇 黄金引擎自动结算",
            })
            save_journal(journal)
        except Exception as e:
            logger.warning(f"黄金日志更新失败: {e}")

    # ── 循环 ──

    def start(self):
        self._running = True
        logger.info(f"🥇 黄金引擎启动 (上限{self.max_open}单, 自动交易:{self.auto_trade})")
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()
        return thread

    def _loop(self):
        # 立即跑一次
        self.evaluate()
        while self._running:
            for _ in range(EVAL_INTERVAL // 10):
                if not self._running:
                    break
                time.sleep(10)
            if not self._running:
                break
            try:
                self.evaluate()
            except Exception as e:
                logger.error(f"黄金评估失败: {e}")

    def stop(self):
        self._running = False

    # ── 状态 ──

    @property
    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "auto_trade": self.auto_trade,
            "max_open": self.max_open,
            "open_trades": self._open_trades,
            "open_count": len(self._open_trades),
            "eval_count": self._eval_count,
            "running": self._running,
            "fuse": {
                "enabled": self.fuse_enabled,
                "max_streak": self.fuse_max_streak,
                "cooldown_hours": self.fuse_cooldown_hours,
                "loss_streak": dict(self._fuse["loss_streak"]),
                "paused_until": dict(self._fuse["paused_until"]),
            },
        }
