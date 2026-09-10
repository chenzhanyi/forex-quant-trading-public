"""澳元 AUD/USD 交易引擎 — 独立于 EURUSD 与黄金

方案A(170天回测: 84单 58% +533p; 40天: 28单 65% +314p):
  - 15分钟评估一次
  - 结算: SL/TP 优先 + H1反转双重确认平仓(浮盈≥0)
  - 熔断: 同方向连续2次SL → 暂停48h (回测贡献+349p, 关键风控)
  - 横盘40p + 重复价位15p 过滤 (回测验证有效)
  - 同方向限3单
  - 中控台独立开关(仅信号 / MT4自动下单)
  - MT4 下单 symbol=AUDUSD — relay品种路由确保只在澳元图表执行
"""
import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("aud")

PROJ = Path(__file__).resolve().parent.parent.parent
AUD_STATE_FILE = PROJ / "data" / "aud_state.json"
SIGNALS_DIR = PROJ / "output" / "signals"   # 与其他品种共用, 供中控台交易日志
SH_TZ = timezone(timedelta(hours=8))

EVAL_INTERVAL = 15 * 60  # 15分钟


def _log_push(tag: str, msg: str) -> None:
    """澳元推送监控日志(与 engine._push_feishu 共用 feishu_push.log)"""
    try:
        first_line = (msg or "").split("\n")[0][:60]
        with open(PROJ / "data" / "feishu_push.log", "a", encoding="utf-8") as f:
            f.write(
                f"{datetime.now(SH_TZ).strftime('%Y-%m-%d %H:%M:%S')} "
                f"| aud.{tag} | {first_line}\n"
            )
    except Exception:
        pass


class AudEngine:
    """澳元交易引擎"""

    def __init__(self):
        self._running = False
        self._eval_count = 0
        self._open_trades = []  # 在途订单
        self._last_eval_time = None
        self._last_eval = None  # 最近一次评估摘要(供总览卡片)
        # 熔断状态: 同方向连续止损 N 次 → 暂停该方向(回测: 方案A关键风控, 贡献+349p)
        self._fuse = {
            "loss_streak": {"SELL": 0, "BUY": 0},
            "paused_until": {"SELL": None, "BUY": None},
        }
        self._last_entry = {"SELL": None, "BUY": None}  # {direction: [ts, price]} 重复价位过滤
        # MT4同步两轮确认: relay /positions 是EA上报缓存, clear与上报之间存在
        # 空窗口期 — 单轮"查不到ticket"可能是瞬时空白, 不得立即判平仓(误判→超限下单)
        self._mt4_missing = {}  # {ticket: 首次缺失时间}
        self._last_eval_bar = None  # 上次处理信号的M15 bar时间(5分钟评估防重复)
        self._load_state()

    # ── 状态持久化 ──

    def _load_state(self):
        if AUD_STATE_FILE.exists():
            try:
                data = json.loads(AUD_STATE_FILE.read_text(encoding="utf-8"))
                self._open_trades = data.get("open_trades", [])
                saved_fuse = data.get("fuse")
                if saved_fuse:
                    self._fuse = {
                        "loss_streak": {**self._fuse["loss_streak"],
                                        **saved_fuse.get("loss_streak", {})},
                        "paused_until": {**self._fuse["paused_until"],
                                         **saved_fuse.get("paused_until", {})},
                    }
                saved_le = data.get("last_entry")
                if saved_le:
                    self._last_entry = {
                        k: (datetime.fromisoformat(v[0]) if v else None, v[1])
                        for k, v in saved_le.items() if v
                    }
                saved_mm = data.get("mt4_missing")
                if saved_mm:
                    self._mt4_missing = {k: v for k, v in saved_mm.items()}
            except Exception:
                self._open_trades = []

    def _save_state(self):
        AUD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        last_entry = {
            k: ([v[0].isoformat(), v[1]] if v else None)
            for k, v in self._last_entry.items()
        }
        tmp = AUD_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"open_trades": self._open_trades, "fuse": self._fuse,
                        "last_entry": last_entry,
                        "mt4_missing": self._mt4_missing},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(AUD_STATE_FILE)  # 原子替换，防写一半崩溃

    # ── 配置 ──

    @property
    def aud_cfg(self):
        # 中控台设置覆盖 YAML 兜底(合并模式)
        from src.utils.dashboard_settings import load
        from src.utils.config_loader import config
        yaml_cfg = config.load().get("aud", {}) or {}
        ui = load().get("aud", {}) or {}
        return {**yaml_cfg, **ui}

    @property
    def enabled(self) -> bool:
        return bool(self.aud_cfg.get("enabled", False))

    @property
    def auto_trade(self) -> bool:
        return bool(self.aud_cfg.get("auto_trade", False))

    @property
    def max_open(self) -> int:
        return int(self.aud_cfg.get("max_open", 3))

    @property
    def fuse_enabled(self) -> bool:
        return bool(self.aud_cfg.get("fuse_enabled", True))

    @property
    def fuse_max_streak(self) -> int:
        return int(self.aud_cfg.get("fuse_max_loss_streak", 2))

    @property
    def fuse_cooldown_hours(self) -> int:
        return int(self.aud_cfg.get("fuse_cooldown_hours", 48))

    @property
    def flat_range(self) -> float:
        return float(self.aud_cfg.get("flat_range", 40))

    @property
    def dedup_pips(self) -> float:
        return float(self.aud_cfg.get("dedup_pips", 15))

    @property
    def reversal_tf(self) -> str:
        return str(self.aud_cfg.get("reversal_tf", "H1"))

    @property
    def reversal_min_profit_pips(self) -> float:
        return float(self.aud_cfg.get("reversal_min_profit_pips", 0))

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
            logger.info(f"🦘 熔断冷却结束: {direction} 恢复交易")
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
                    f"🦘 熔断触发: {direction} 连续{streak}次止损 → "
                    f"暂停该方向 {self.fuse_cooldown_hours} 小时"
                )
                try:
                    push_script = PROJ / "scripts" / "feishu_push.sh"
                    import subprocess
                    _fuse_msg = (f"🦘 澳元熔断提醒\n{'─'*20}\n"
                                 f"方向 {direction} 连续 {streak} 次止损\n"
                                 f"→ 已暂停该方向 {self.fuse_cooldown_hours} 小时\n"
                                 f"(回测验证: 熔断为方案A贡献+349p)")
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

    def _eval_trend(self, detector) -> str:
        """H4 200SMA 趋势摘要(总览卡片用): 价格在均线上方/下方"""
        try:
            _, sma, _ = detector._get_h4_meta()
            if sma is None:
                return "数据不足"
            m15 = detector._prepare_m15()
            if m15 is None:
                return "数据不足"
            p = float(m15.iloc[-1]['close'])
            side = "上方(偏多)" if p > sma else "下方(偏空)"
            return f"H4 200SMA {side} 价{p:.5f} vs {sma:.5f}"
        except Exception:
            return "?"

    def _set_last_eval(self, summary: str, detail: str = "") -> None:
        self._last_eval = {
            "time": datetime.now(SH_TZ).strftime("%m-%d %H:%M"),
            "summary": summary,
            "detail": detail,
        }

    def evaluate(self, refresh: bool = False) -> dict:
        """评估一次澳元信号

        refresh: True=评估前刷新行情(手动评估用); 统一调度器已刷新, 传 False 避免重复拉取
        """
        from src.strategy.aud_entry import AudEntryDetector
        self._eval_count += 1
        detector = AudEntryDetector()

        # 刷新行情(手动触发时): OANDA 主源 + TwelveData 备用
        if refresh:
            try:
                from src.data_collection.market_data import refresh_symbol_data
                refresh_symbol_data("AUD_USD")
            except Exception as e:
                logger.warning(f"澳元行情刷新失败: {str(e)[:60]}")

        # 结算在途订单（复用同一个 detector 的数据）
        self._settle_open_trades(detector)
        # MT4 实际持仓同步: 手动平仓/EA执行差异 → 修正本地在途订单
        self._sync_mt4_positions(detector)

        # 同bar防重: 5分钟评估间隔下, 同一根M15会被评估2~3次 —
        # 同一bar只生成一次信号(结算/同步不受影响, 照常每轮执行)
        try:
            m15_df = detector._prepare_m15()
            cur_bar = m15_df.index[-1] if m15_df is not None and len(m15_df) else None
        except Exception:
            cur_bar = None
        if cur_bar is not None and cur_bar == self._last_eval_bar:
            return {"enabled": self.enabled, "signal": None, "skipped": "same_bar",
                    "open": len(self._open_trades), "eval": self._eval_count}
        self._last_eval_bar = cur_bar

        if not self.enabled:
            self._set_last_eval("未启用", self._eval_trend(detector))
            return {"enabled": False, "eval": self._eval_count}

        # 重大数据黑洞窗口（非农等）→ 跳过新信号, 但结算照常
        try:
            from src.utils.trade_blackout import is_blacked_out
            blacked, reason = is_blacked_out()
            if blacked:
                logger.info(f"🦘 {reason} → 跳过评估")
                self._set_last_eval(f"黑洞窗口: {reason}", self._eval_trend(detector))
                return {"enabled": True, "signal": None, "blackout": True,
                        "reason": reason, "open": len(self._open_trades),
                        "eval": self._eval_count}
        except Exception as e:
            logger.debug(f"澳元数据黑洞检查失败: {e}")

        sig = detector.detect()
        if sig is None:
            self._set_last_eval("无入场信号", self._eval_trend(detector))
            return {"enabled": True, "signal": None, "open": len(self._open_trades),
                    "eval": self._eval_count}

        # 熔断检查: 同方向连续止损后暂停(方案A关键风控)
        if self._is_fused(sig.direction):
            logger.info(f"🦘 {sig.direction} 处于熔断暂停期，跳过新信号")
            d = "做空" if sig.direction == "SELL" else "做多"
            self._set_last_eval(f"信号出现({d})但熔断暂停中", self._eval_trend(detector))
            return {"enabled": True, "signal": sig.__dict__, "skipped": "fused",
                    "open": len(self._open_trades), "eval": self._eval_count}

        # 横盘+重复价位过滤(方案A回测参数: flat40p/dedup15p)
        skip_reason = self._check_dup_flat(detector, sig)
        if skip_reason:
            logger.info(f"🦘 {skip_reason}，跳过新信号")
            d = "做空" if sig.direction == "SELL" else "做多"
            self._set_last_eval(f"信号出现({d})但横盘/重复过滤", self._eval_trend(detector))
            return {"enabled": True, "signal": sig.__dict__, "skipped": skip_reason,
                    "open": len(self._open_trades), "eval": self._eval_count}

        # 同方向持仓上限(方案A: 3单) — 本地 + MT4 实时持仓双口径核对
        # (防本地计数失真: 如MT4同步误清/下单回滚竞态 → 超限下单)
        same_dir = [t for t in self._open_trades if t["direction"] == sig.direction]
        no_ticket_local = [t for t in same_dir if not t.get("ticket")]
        mt4_same = self._mt4_same_dir_count(sig.direction)
        if mt4_same is None:
            # relay 查询失败(Cloudflare 403等) → 本地在途全数兜底(保守, 防超限)
            total_same = len(self._open_trades)
            logger.debug(f"🦘 MT4持仓查询失败, 限单用本地在途数兜底: {total_same}")
        else:
            total_same = mt4_same + len(no_ticket_local)
        if total_same >= self.max_open:
            logger.info(f"🦘 同方向单量已达上限(本地{len(no_ticket_local)}+MT4{mt4_same}≥{self.max_open})，跳过新信号")
            d = "做空" if sig.direction == "SELL" else "做多"
            self._set_last_eval(f"信号出现({d})但单量已满", self._eval_trend(detector))
            return {"enabled": True, "signal": sig.__dict__, "skipped": "max_open",
                    "open": len(self._open_trades), "eval": self._eval_count}

        # 记录在途
        trade = {
            "direction": sig.direction,
            "entry": sig.entry_price,
            "sl": sig.stop_loss,
            "tp": sig.take_profit,
            "sl_pips": sig.sl_pips,
            "tp_pips": sig.tp_pips,
            "pattern": sig.pattern,
            "time": datetime.now(SH_TZ).isoformat(),
        }
        self._open_trades.append(trade)
        self._last_entry[sig.direction] = [datetime.now(SH_TZ), sig.entry_price]
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
            logger.warning(f"澳元推送失败: {e}")

        # MT4 自动下单
        mt4_result = None
        if self.auto_trade:
            mt4_result = self._mt4_order(sig)
            trade["mt4"] = bool((mt4_result or {}).get("success"))
            ticket = ((mt4_result or {}).get("result") or {}).get("ticket")
            if ticket:
                trade["ticket"] = int(ticket)

        d = "做空" if sig.direction == "SELL" else "做多"
        self._set_last_eval(f"入场信号: {d} @ {sig.entry_price:.5f}", self._eval_trend(detector))
        return {"enabled": True, "signal": trade, "open": len(self._open_trades),
                "eval": self._eval_count, "mt4": mt4_result}

    def _check_dup_flat(self, detector, sig) -> str:
        """横盘+重复价位过滤(方案A: flat40p / dedup15p)

        横盘期(近12根H4区间<40p)禁止同价位扎堆开仓; 趋势期允许顺势加仓。
        """
        flat_now = False
        try:
            rng = detector.h4_flat_range()
            flat_now = rng is not None and rng < self.flat_range
        except Exception:
            pass
        prev = self._last_entry.get(sig.direction)
        if prev and prev[0] is not None and self.dedup_pips > 0:
            dt = datetime.now(SH_TZ)
            hours = (dt - prev[0]).total_seconds() / 3600
            dist = abs(sig.entry_price - prev[1]) * 10000
            if hours <= 48 and dist < self.dedup_pips and flat_now:
                return "dup_flat"
        return ""

    def _settle_open_trades(self, detector=None):
        """用最新价格结算在途订单: SL/TP 优先 + H1反转双重确认平仓

        同一 bar 内 SL/TP 都被触发时，用 K 线方向判断先后。
        """
        if detector is None:
            from src.strategy.aud_entry import AudEntryDetector
            detector = AudEntryDetector()
        m15 = detector._prepare_m15()
        if m15 is None or not self._open_trades:
            return
        bar = m15.iloc[-1]
        hi = float(bar['high']); lo = float(bar['low'])
        op = float(bar['open']); cl = float(bar['close'])
        bearish = cl < op  # 阴线: 先跌后涨, 下方先触发
        bullish = cl > op  # 阳线: 先涨后跌, 上方先触发

        # H1 反转出场检测(浮盈≥阈值 → 平仓) — 与回测 sim_reversal_exit 同款
        # 按持仓方向逐单检测(见循环内), 这里只预取 H1 数据
        h1_df = detector.prepare_h1() if self.reversal_tf == "H1" else None

        remaining = []
        for t in self._open_trades:
            direction = t["direction"]
            sl_hit = (direction == "SELL" and hi >= t["sl"]) or \
                     (direction == "BUY" and lo <= t["sl"])
            tp_hit = (direction == "SELL" and lo <= t["tp"]) or \
                     (direction == "BUY" and hi >= t["tp"])

            # 反转出场: 反转形态+收盘破H1 200SMA+浮盈≥阈值 → 按当前价平仓(REV)
            if not sl_hit and not tp_hit and self.reversal_tf == "H1":
                pat = detector.detect_reversal_h1(direction, h1_df) if h1_df is not None else ""
                if pat:
                    float_pips = ((t["entry"] - cl) if direction == "SELL"
                                  else (cl - t["entry"])) * 10000
                    if float_pips >= self.reversal_min_profit_pips:
                        remaining.append((t, "REV", cl, float_pips, pat))
                        continue

            if not sl_hit and not tp_hit:
                remaining.append(t)
                continue

            # 同一 bar 内 SL/TP 都触发 → 用 K 线方向判断先后
            if sl_hit and tp_hit:
                if direction == "SELL":
                    sl_first = bullish   # 阳线先涨 → 上方先触发 → SL先
                else:
                    sl_first = bearish   # 阴线先跌 → 下方先触发 → SL先
            else:
                sl_first = sl_hit

            remaining.append((t, "SL" if sl_first else "TP", None, None, ""))

        new_open = []
        changed = False
        for item in remaining:
            if isinstance(item, tuple):
                t, oc, exit_p, float_pips, pat = item
                self._finalize_trade(t, oc, exit_p, float_pips, pat)
                changed = True
            else:
                new_open.append(item)

        if changed:
            self._open_trades = new_open
            self._save_state()

    # ── MT4 / 推送 / 日志 ──

    def _mt4_same_dir_count(self, direction: str):
        """relay 实时查询 MT4 上本品种同向持仓数 — 成功返回计数(可为0),
        查询失败返回 None(调用方用本地在途数兜底, 防 Cloudflare 403 时超限下单)"""
        try:
            from src.utils.dashboard_settings import load
            cfg = load().get("mt4_relay", {})
            url = cfg.get("url", "").rstrip("/")
            if not url or not cfg.get("enabled", False):
                return None
            import httpx
            from src.execution.mt4_remote import relay_headers
            resp = httpx.get(f"{url}/positions", timeout=5, headers=relay_headers())
            if resp.status_code != 200:
                return None
            mt4_type = direction  # MT4 type 字段: BUY/SELL
            return sum(
                1 for p in resp.json().get("positions", [])
                if str(p.get("symbol", "")).upper() == "AUDUSD"
                and p.get("type") == mt4_type
            )
        except Exception:
            return None

    def _sync_mt4_positions(self, detector=None) -> None:
        """从 relay 拉 MT4 实际持仓, 修正本地在途订单

        - 本地有 ticket 但 MT4 已无该持仓 → 手动平仓/EA差异 → 本地结算
          (平仓类型按当前价距 SL/TP 远近判定, 供熔断计数)
        - MT4 有但本地无 → 补记(手动单)
        """
        try:
            from src.utils.dashboard_settings import load
            cfg = load().get("mt4_relay", {})
            url = cfg.get("url", "").rstrip("/")
            if not url or not cfg.get("enabled", False):
                return
            import httpx
            resp = httpx.get(f"{url}/positions", timeout=5, headers=relay_headers())
            if resp.status_code != 200:
                return
            mt4 = [p for p in resp.json().get("positions", [])
                   if str(p.get("symbol", "")).upper() == "AUDUSD"]
            mt4_tickets = {int(p["ticket"]) for p in mt4 if p.get("ticket")}

            if self._open_trades:
                if detector is None:
                    from src.strategy.aud_entry import AudEntryDetector
                    detector = AudEntryDetector()
                m15 = detector._prepare_m15()
                cl = float(m15.iloc[-1]['close']) if m15 is not None else None
                remaining, changed, mm_changed = [], False, False
                for t in self._open_trades:
                    tk = t.get("ticket")
                    if not tk:
                        remaining.append(t)
                        continue
                    if tk in mt4_tickets:
                        # 重新出现 → 清首次缺失标记(之前是瞬时空白)
                        if self._mt4_missing.pop(tk, None):
                            mm_changed = True
                        remaining.append(t)
                        continue
                    # 两轮确认: 第一轮缺失只标记, 第二轮仍缺失才判平仓
                    if tk not in self._mt4_missing:
                        self._mt4_missing[tk] = datetime.now(SH_TZ).isoformat()
                        mm_changed = True
                        remaining.append(t)  # 本轮先保留, 下轮再判
                        continue
                    # 连续第二轮缺失 → 确认平仓
                    self._mt4_missing.pop(tk, None)
                    mm_changed = True
                    logger.info(f"🦘 MT4同步: ticket {tk} 连续2轮不在MT4持仓 → 本地结算")
                    is_sl = True
                    if cl is not None:
                        d_sl = abs(cl - t.get("sl", cl))
                        d_tp = abs(cl - t.get("tp", cl))
                        is_sl = d_sl <= d_tp
                    self._finalize_trade(t, "SL" if is_sl else "TP", None, None, "")
                    changed = True
                if changed or mm_changed:
                    self._open_trades = remaining
                    self._save_state()

            # MT4 有但本地无 → 补记(手动单)
            local_tickets = {t.get("ticket") for t in self._open_trades}
            for p in mt4:
                tk = int(p.get("ticket", 0))
                if not tk or tk in local_tickets:
                    continue
                entry = float(p.get("open", 0))
                if entry <= 0:
                    continue
                direction = "SELL" if p.get("type") == "SELL" else "BUY"
                sl = float(p.get("sl", 0))
                tp = float(p.get("tp", 0))
                self._open_trades.append({
                    "direction": direction, "entry": entry,
                    "sl": sl if sl > 0 else entry,
                    "tp": tp if tp > 0 else entry,
                    "sl_pips": round(abs(entry - sl) * 10000, 1) if sl > 0 else 0,
                    "tp_pips": round(abs(tp - entry) * 10000, 1) if tp > 0 else 0,
                    "pattern": "MT4同步补记(手动单)", "ticket": tk,
                    "time": datetime.now(SH_TZ).isoformat(),
                })
                self._save_state()
                logger.info(f"🦘 MT4同步: 补记 ticket {tk} {direction} @{entry}")
        except Exception as e:
            logger.debug(f"澳元MT4同步失败: {e}")

    def _mt4_order(self, sig):
        """通过 MT4 relay 下澳元单 — symbol=AUDUSD, relay按品种路由到澳元图表EA"""
        try:
            from src.utils.dashboard_settings import load
            cfg = load().get("mt4_relay", {})
            url = cfg.get("url", "").rstrip("/")
            if not url or not cfg.get("enabled", False):
                return {"skipped": "mt4_not_enabled"}
            import httpx
            lots = float(self.aud_cfg.get("lots", 0.01))
            resp = httpx.post(f"{url}/order", json={
                "direction": sig.direction,
                "symbol": "AUDUSD",
                "volume": lots,
                "sl": round(sig.stop_loss, 5),
                "tp": round(sig.take_profit, 5),
                # 距离模式: EA 按成交价换算 SL/TP, 消除平台点差差异
                "sl_pips": sig.sl_pips,
                "tp_pips": sig.tp_pips,
                "comment": "aud_auto",
            }, timeout=15, headers=relay_headers())
            result = resp.json()
            if result.get("success"):
                logger.info(f"🦘 MT4澳元下单成功: {result.get('order_id')}")
            else:
                logger.warning(f"🦘 MT4澳元下单失败: {result.get('error')}")
                # 失败回滚
                if self._open_trades:
                    self._open_trades.pop()
                    self._save_state()
            return result
        except Exception as e:
            logger.warning(f"MT4澳元下单异常: {e}")
            if self._open_trades:
                self._open_trades.pop()
                self._save_state()
            return {"error": str(e)}

    def _push_signal(self, sig):
        push_script = PROJ / "scripts" / "feishu_push.sh"
        import subprocess
        d = "做空" if sig.direction == "SELL" else "做多"
        msg = (
            f"🦘 澳元入场信号!\n"
            f"{'─'*20}\n"
            f"⏰ {datetime.now(SH_TZ).strftime('%H:%M')} GMT+8\n\n"
            f"💡 {d} @ {sig.entry_price:.5f}\n"
            f"🛑 止损: {sig.stop_loss:.5f} (-{sig.sl_pips}p)\n"
            f"🎯 止盈: {sig.take_profit:.5f} (+{sig.tp_pips}p)\n"
            f"📐 R:R {sig.rr_ratio:.1f} | 形态: {sig.pattern}\n"
            f"📊 在途: {len(self._open_trades)}/{self.max_open}"
        )
        _log_push("signal", msg)
        subprocess.run(["bash", str(push_script), msg],
                       capture_output=True, text=True, timeout=30)

    def _write_signal_file(self, sig, trade):
        """出信号时写一个 output/signals/*_AUDUSD_signal.json"""
        try:
            SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
            d = "做空" if sig.direction == "SELL" else "做多"
            ts = datetime.now(SH_TZ).strftime("%Y%m%d_%H%M%S")
            name = f"{ts}_AUDUSD_signal.json"
            data = {
                "timestamp": datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M GMT+8"),
                "symbol": "AUD/USD",
                "can_trade": True,
                "direction": d,
                "entry_price": sig.entry_price,
                "stop_loss": sig.stop_loss,
                "take_profit_1": sig.take_profit,
                "sl_pips": sig.sl_pips,
                "tp_pips": sig.tp_pips,
                "rr_ratio": sig.rr_ratio,
                "pattern": sig.pattern,
                "sl_method": "ATR2.0x",
                "holding_advice": (f"🦘 澳元{d} @ {sig.entry_price:.5f}\n"
                                   f"🛑 止损: {sig.stop_loss:.5f} (-{sig.sl_pips}p)\n"
                                   f"🎯 止盈: {sig.take_profit:.5f} (+{sig.tp_pips}p)\n"
                                   f"📐 R:R {sig.rr_ratio:.1f} | 形态: {sig.pattern}"),
            }
            (SIGNALS_DIR / name).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return name
        except Exception as e:
            logger.warning(f"澳元信号文件写入失败: {e}")
            return None

    def _finalize_trade(self, t, oc, exit_p, float_pips, pat):
        """订单结算后, 把结果写回信号文件 + trade_journal, 供面板复盘"""
        direction = t.get("direction", "")
        if oc == "REV":
            t["exit_result"] = "rev"
            t["exit_price"] = round(exit_p, 5)
            t["pnl_pips"] = round(float_pips, 1)
            logger.info(f"🦘 反转出场: {direction} @{t['entry']:.5f} → {exit_p:.5f} (+{float_pips:.0f}p, {pat})")
        else:
            t["exit_result"] = "sl" if oc == "SL" else "tp"
            t["exit_price"] = t.get("sl") if oc == "SL" else t.get("tp")
            t["pnl_pips"] = round(-t.get("sl_pips", 0), 1) if oc == "SL" \
                else round(t.get("tp_pips", 0), 1)
            logger.info(f"🦘 {'止损' if oc == 'SL' else '止盈'}: {direction} @{t['entry']:.5f} → {t['pnl_pips']:+.0f}p")
        t["exit_time"] = datetime.now(SH_TZ).isoformat()

        # 熔断计数: 仅 SL 计损(REV/TP 清零)
        self._record_fuse_result(direction, oc == "SL")

        sig_file = t.get("signal_file")
        if not sig_file:
            return
        self._update_signal_file(sig_file, t)
        self._update_journal(sig_file, t)

    def _update_signal_file(self, sig_file, t):
        """把结算结果写回澳元信号文件"""
        try:
            p = SIGNALS_DIR / sig_file
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            data["exit_result"] = t["exit_result"]
            data["exit_price"] = t["exit_price"]
            data["exit_time"] = t["exit_time"]
            data["pnl_pips"] = t["pnl_pips"]
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        except Exception as e:
            logger.warning(f"澳元信号文件更新失败: {e}")

    def _update_journal(self, sig_file, t):
        """在 trade_journal.json 写入/补一条澳元结算记录"""
        try:
            from src.web.trade_journal import load_journal, save_journal
            journal = load_journal()
            entry = next((j for j in journal if j.get("signal_file") == sig_file), None)
            if entry is None:
                entry = {"signal_file": sig_file}
                journal.append(entry)
            entry.update({
                "symbol": "AUD/USD",
                "entered": True if t.get("mt4") else None,
                "exit_result": t["exit_result"],
                "exit_price": t["exit_price"],
                "exit_time": t["exit_time"],
                "pnl_pips": t["pnl_pips"],
                "annotated_at": t["exit_time"],
                "notes": "🦘 澳元引擎自动结算",
            })
            save_journal(journal)
        except Exception as e:
            logger.warning(f"澳元日志更新失败: {e}")

    # ── 循环 ──

    def start(self):
        """澳元引擎启动 — 评估由统一调度器驱动(每15分钟统一拉取三品种数据后依次评估)"""
        self._running = True
        logger.info(f"🦘 澳元引擎启动 (上限{self.max_open}单, 自动交易:{self.auto_trade}, 统一调度评估)")
        return None

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
                logger.error(f"澳元评估失败: {e}")

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
            "last_eval": self._last_eval,
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
