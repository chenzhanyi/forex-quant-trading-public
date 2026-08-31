"""守护进程 — 面板服务 + 定时信号评估 + 自动复盘 + 飞书推送"""
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.daemon.history import SignalHistory
from src.strategy.signal import SignalGenerator
from src.strategy.review import ReviewEngine
from src.utils.config_loader import setup_logging

setup_logging("daemon")
logger = logging.getLogger("daemon")

# 信号评估间隔（实盘：15分钟刷新数据 + 评估信号）
SIGNAL_INTERVAL = 15 * 60  # 15 分钟
# 复盘间隔
REVIEW_INTERVAL = 6 * 3600  # 6 小时
# RSS 新闻 + 基本面采集间隔
NEWS_INTERVAL = 2 * 3600  # 2 小时
# 上海时区 (UTC+8)
SH_TZ = timezone(timedelta(hours=8))
# 推送时间点（上海时间）— 全自动交易后定时简报已禁用
# 如需恢复: 取消注释 PUSH_TIMES
PUSH_TIMES = []  # [(7, 30), (15, 0), (22, 0)]  # 早间 / 亚盘总结 / 晚间
# 错失机会扫描（每天一次，欧美盘重叠时段数据充足）
OPPORTUNITY_SCAN_HOUR = 16  # 上海时间 16:00
# 每周错失机会闭环分析（周六上午，方便周末复盘）
WEEKLY_REVIEW_DAY = 5  # 0=Mon, 5=Sat
WEEKLY_REVIEW_HOUR = 10  # 上海时间 10:00


class DaemonEngine:
    """守护进程引擎

    在后台运行:
    1. 定时信号评估 → 记录历史
    2. 自动复盘（对比过去信号与实际走势）
    3. RSS 新闻采集 + 基本面自动分类
    4. 早间/亚盘/晚间 飞书推送
    5. 每日错失机会扫描（回看观望信号 >45pips）
    """

    def __init__(self):
        self.history = SignalHistory()
        self.generator = SignalGenerator()
        self.review = ReviewEngine()
        self._running = False
        self._eval_count = 0
        self._last_news_time: datetime | None = None
        self._last_push_time: datetime | None = None
        self._last_review_time: datetime | None = None
        self._last_scan_time: datetime | None = None
        self._last_weekly_time: datetime | None = None
        # 实时信号推送冷却：避免同一信号反复推送
        self._last_signal_alert_time: datetime | None = None
        self._last_alert_direction: str = ""  # 上次推送的方向，方向变了就重新推送
        self._signal_alert_cooldown = 30 * 60  # 30 分钟冷却
        # 同向叠加信号栈(限制同一波段同时持仓数, 防止一个波段堆4-5单)
        self._signal_stack: list = []           # [{ts, direction}]
        self._signal_life_span = 48 * 3600      # 信号最长生命周期 48h(与回测窗口一致)
        self._last_position_reminder_time: datetime | None = None  # 持仓提醒冷却
        self._position_reminder_interval = 4 * 3600  # 每 4 小时
        # API 调用计时
        self._last_api_call: str = ""          # 上次 API 调用时间
        self._last_api_latency_ms: float = 0   # 上次 API 延迟(毫秒)
        self._last_api_quote: float = 0        # 最新报价
        self._last_api_status: str = "未调用"  # 状态
        # VPS 连接状态
        self._vps_status: dict = {"http": False, "tcp": False, "ea": False, "latency_ms": 0, "last_check": "", "checks": 0, "fails": 0}
        # 线程心跳: {thread_name: (last_beat, max_interval_seconds)}
        self._heartbeats: dict[str, tuple[datetime, float]] = {}
        # 熔断状态(EURUSD): 同方向连续止损N次 → 暂停该方向
        # 回测170天: 实盘配置+熔断2连SL = 82%胜率 +650p(基线75% +588p)
        self._fuse = {
            "loss_streak": {"SELL": 0, "BUY": 0},
            "paused_until": {"SELL": None, "BUY": None},
        }
        self._load_fuse()

    def evaluate_signal(self) -> dict:
        """生成一次信号并记录"""
        try:
            sig = self.generator.generate()

            # 熔断检查(在记录/保存之前): 同方向连续止损暂停期 → 转观察
            if sig.can_trade:
                direction = "SELL" if "做空" in sig.direction else "BUY"
                if self._eurusd_fused(direction):
                    sig.can_trade = False
                    sig.direction = "观望"
                    sig.skip_reason = (
                        f"⛔ 熔断: {direction} 连续止损后暂停交易中，信号转观察"
                    )
                    sig.confidence = 0
                    logger.info(f"⛔ 熔断拦截: {direction} 信号转观察")
                    self._push_feishu(
                        f"⛔ EURUSD 熔断拦截\n{'─' * 20}\n"
                        f"{direction} 信号被熔断机制拦截(连续止损暂停期)\n"
                        f"→ 不推送入场、不下单，等待冷却结束"
                    )

            signal_data = {
                "timestamp": sig.timestamp,
                "direction": sig.direction,
                "can_trade": sig.can_trade,
                "entry_price": sig.entry_price if sig.can_trade else None,
                "stop_loss": sig.stop_loss if sig.can_trade else None,
                "tp1": sig.take_profit_1 if sig.can_trade else None,
                "tp2": sig.take_profit_2 if sig.can_trade else None,
                "rr_ratio": sig.rr_ratio if sig.can_trade else None,
                "lots": sig.position_lots if sig.can_trade else None,
                "max_loss": sig.max_loss_amount if sig.can_trade else None,
                "confidence": sig.confidence,
                "trend": sig.trend_overall,
                "d1": sig.trend_d1,
                "h4": sig.trend_h4,
                "skip_reason": sig.skip_reason,
                "entry_reason": sig.entry_reason if sig.can_trade else "",
            }

            # 记录到历史
            self.history.record(signal_data)

            # 保存信号文件
            self.generator.save(sig)

            self._eval_count += 1
            logger.info(f"📊 信号 #{self._eval_count}: {sig.direction}")

            # 🚀 可交易信号 → 立即飞书推送 + 记录持仓 + MT4自动下单
            if sig.can_trade:
                direction = "SELL" if "做空" in sig.direction else "BUY"

                # 同向叠加上限检查(同一波段最多 max_same_dir 单, 超限转延续提示)
                # 槽位只在"成功推送"后占用 — 冷却期内/推送失败的信号不算单
                if self._push_signal_if_slot(sig):
                    pushed = self._maybe_push_signal_alert(sig)
                    self._handle_trade_execution(sig, direction)
                    if pushed:
                        self._signal_stack.append({"ts": time.time(), "direction": direction})

            # 🔍 检测活跃持仓是否接近 TP → 推送调整建议
            self._maybe_check_positions()
            # 📬 定期推送持仓状态卡片（每4h）
            self._maybe_push_position_reminder()
            # 📡 检测 VPS 连通性
            self._check_vps_health()
            # 🔄 MT4 持仓同步
            self._sync_mt4_positions()

            return signal_data

        except Exception as e:
            logger.error(f"信号评估失败: {e}")
            return {"error": str(e)}

    def run_review(self) -> dict:
        """运行一次复盘"""
        try:
            # 先自动结算未标注的历史信号(行情评估判定SL/TP写回日志)
            try:
                from src.web.trade_journal import auto_settle
                settled = auto_settle(days=30)
                if settled:
                    logger.info(f"🤖 自动结算: {settled} 条历史信号已判定SL/TP")
            except Exception as e:
                logger.debug(f"自动结算跳过: {e}")
            report = self.review.generate_review(days=7)
            path = self.review.save_review(days=7)
            logger.info(f"📋 复盘完成: {path}")
            return {"report_length": len(report), "path": str(path)}
        except Exception as e:
            logger.error(f"复盘失败: {e}")
            return {"error": str(e)}

    # 各周期数据新鲜度阈值(秒): 超过则判定 OANDA 数据停更, 触发 Twelve Data 备用源
    FRESHNESS_MAX_AGE = {"M15": 90 * 60, "H1": 6 * 3600, "H4": 12 * 3600, "D1": 3 * 86400}

    @staticmethod
    def _market_closed() -> bool:
        """外汇市场是否休市(UTC): 周五21:00 收市, 周日21:00 开市

        休市期间数据必然"过期" — 不触发备用源补充, 避免空转浪费额度。
        """
        now = datetime.now(timezone.utc)
        wd, h = now.weekday(), now.hour
        if wd == 5:
            return True                    # 周六全天
        if wd == 6 and h < 21:
            return True                    # 周日21:00 UTC 前
        if wd == 4 and h >= 21:
            return True                    # 周五21:00 UTC 后
        return False

    def _refresh_market_data(self) -> None:
        """刷新行情数据: OANDA 主源 + Twelve Data 备用(自动重试)

        OANDA 请求失败, 或返回的数据本身停更(如 2026-08-29 全天缺口),
        都触发 Twelve Data 补齐 — 保证本地 parquet 始终有新数据。
        """
        max_retries = 3
        retry_delay = 10  # 秒

        for attempt in range(1, max_retries + 1):
            t0 = time.time()
            try:
                from src.data_collection.oanda import OandaClient
                o = OandaClient()
                latest_quote = 0
                for tf in ["M15", "H1", "H4", "D1"]:
                    d = o.fetch_candles(tf, 200)
                    o.save_candles(d, tf)
                    if d and tf == "M15":
                        latest_quote = float(d[-1]["close"])

                # 新鲜度检查: OANDA 正常返回但数据停更 → 备用源补齐
                # (休市期间跳过 — 市场收市数据必然"过期", 不是数据源故障)
                if not self._market_closed():
                    for tf in ["M15", "H1", "H4", "D1"]:
                        if not self._is_data_fresh(tf):
                            self._fill_from_backup(tf, o)

                elapsed_ms = (time.time() - t0) * 1000
                self._last_api_call = datetime.now(SH_TZ).strftime("%H:%M:%S")
                self._last_api_latency_ms = round(elapsed_ms, 1)
                self._last_api_quote = latest_quote
                self._last_api_status = "OK"
                if attempt > 1:
                    logger.info(f"📡 OANDA 重试第{attempt}次成功 ({elapsed_ms:.0f}ms)")
                return  # 成功，退出

            except Exception as e:
                elapsed_ms = (time.time() - t0) * 1000
                if attempt < max_retries:
                    logger.warning(
                        f"⚠️ OANDA 刷新失败(第{attempt}次): {str(e)[:50]}, "
                        f"{retry_delay}s后重试..."
                    )
                    time.sleep(retry_delay)
                else:
                    # OANDA 彻底失败 → 备用源接管全部周期
                    try:
                        self._fill_from_backup("M15", None)
                        self._fill_from_backup("H1", None)
                        self._fill_from_backup("H4", None)
                        self._fill_from_backup("D1", None)
                    except Exception:
                        pass
                    self._last_api_call = datetime.now(SH_TZ).strftime("%H:%M:%S")
                    self._last_api_latency_ms = round(elapsed_ms, 1)
                    self._last_api_status = f"ERR×{max_retries}: {str(e)[:30]}"
                    logger.warning(
                        f"❌ OANDA 刷新失败(已重试{max_retries}次): {e}"
                    )

    def _is_data_fresh(self, tf: str) -> bool:
        """本地 parquet 最后K线是否在新鲜度阈值内"""
        try:
            from src.data_collection.oanda import OandaClient
            df = OandaClient().load_parquet(tf)
            if df.empty:
                return False
            last_ts = df.index.max()
            if last_ts.tzinfo is None:
                last_ts = last_ts.tz_localize("UTC")
            age = (datetime.now(timezone.utc) - last_ts).total_seconds()
            return age <= self.FRESHNESS_MAX_AGE.get(tf, 4 * 3600)
        except Exception:
            return False

    def _fill_from_backup(self, tf: str, oanda_client=None) -> None:
        """用 Twelve Data 补齐缺失数据(只补缺口, 不覆盖 OANDA 已有K线)

        两个数据源报价存在差异, 若直接合并会以 Twelve Data 报价覆盖
        OANDA 同时间戳K线, 造成数据混杂。这里只保留本地没有的时间戳。
        """
        try:
            import pandas as pd
            from src.data_collection.twelvedata import TwelveDataClient
            from src.data_collection.oanda import OandaClient
            td = TwelveDataClient()
            if not td.is_configured():
                return
            d = td.fetch_candles(tf, 200)
            if not d:
                logger.debug(f"📡 TwelveData {tf}: 无数据")
                return
            o = oanda_client or OandaClient()
            # 只保留本地缺失的时间戳(缺口)
            # 本地数据读不到时放弃补充 — 否则会误判"全是缺口",
            # 用 Twelve Data 报价覆盖 OANDA 已有数据(两源报价存在差异)
            try:
                existing = o.load_parquet(tf)
                existing_idx = set(pd.to_datetime(existing.index).tz_localize(None))
            except Exception:
                logger.debug(f"📡 TwelveData {tf}: 本地数据不可读, 放弃补充")
                return
            new_only = [
                c for c in d
                if pd.Timestamp(c["time"]).tz_localize(None) not in existing_idx
            ]
            if not new_only:
                logger.debug(f"📡 TwelveData {tf}: 无缺口需补(与本地数据一致)")
                return
            o.save_candles(new_only, tf)
            logger.info(
                f"📡 TwelveData 备用源补充 {tf}: 缺口 {len(new_only)} 根 "
                f"(最新 {new_only[-1]['time'][:16]} UTC)"
            )
        except Exception as e:
            logger.warning(f"📡 TwelveData 备用源 {tf} 补充失败: {str(e)[:60]}")

    def _signal_loop(self):
        """每 15 分钟刷新数据 + 评估信号（实盘模式）"""
        logger.info(f"🔄 信号评估: 每 {SIGNAL_INTERVAL // 60} 分钟（含 OANDA 实时数据刷新）")
        # 启动时立刻跑一次
        self._refresh_market_data()
        self.evaluate_signal()
        self._last_eval_time = datetime.now(timezone.utc)
        while self._running:
            for _ in range(SIGNAL_INTERVAL // 10):
                if not self._running:
                    break
                time.sleep(10)
            if not self._running:
                break

            # 评估信号（即使刷新失败也用旧数据）
            try:
                self._refresh_market_data()
            except Exception:
                pass  # 刷新失败不阻塞信号评估
            self.evaluate_signal()
            self._last_eval_time = datetime.now(timezone.utc)
            self._beat("signal", SIGNAL_INTERVAL * 2)

    def _review_loop(self):
        """定时复盘循环"""
        logger.info(f"🔄 自动复盘启动 (间隔={REVIEW_INTERVAL}s)")
        while self._running:
            time.sleep(REVIEW_INTERVAL)
            if not self._running:
                break
            self.run_review()
            self._beat("review", 12 * 3600)  # 每 6h, 12h 未跳=异常

    def _collect_news(self) -> None:
        """执行一次 RSS 新闻采集 + 基本面分类"""
        try:
            from src.analysis.fundamentals import FundamentalsAnalyzer

            fa = FundamentalsAnalyzer()
            # collect_and_classify() 内部完成: RSS fetch → CSV save → 分类
            classified = fa.collect_and_classify()
            fa.save_fundamentals(classified)

            self._last_news_time = datetime.now(timezone.utc)
            long_cnt = len(classified.get("long", []))
            med_cnt = len(classified.get("medium", []))
            logger.info(f"📰 新闻+基本面采集完成 (长期:{long_cnt} 中期:{med_cnt})")
        except Exception as e:
            logger.warning(f"📰 新闻/基本面采集失败: {e}")

    def _news_loop(self):
        """RSS 新闻 + 基本面自动分类循环"""
        logger.info(f"📰 新闻+基本面采集启动 (间隔={NEWS_INTERVAL//3600}h)")
        # 首次启动立即跑一次
        self._collect_news()
        while self._running:
            # 分片 sleep，便于快速响应 shutdown
            for _ in range(NEWS_INTERVAL // 60):
                if not self._running:
                    break
                time.sleep(60)
            if not self._running:
                break
            self._collect_news()
            self._beat("news", 4 * 3600)  # 每 2h, 4h 未跳=异常

    # ── 错失机会扫描 ──

    def _run_opportunity_scan(self) -> None:
        """扫描过去 24h 的观望信号，记录 >60pips 波动的错失机会"""
        try:
            from src.analysis.opportunity_tracker import OpportunityTracker
            tracker = OpportunityTracker()
            result = tracker.scan(days=1, min_pips=45)
            logger.info(
                f"🔍 错失机会扫描: {result['total_observe']} 个观望信号, "
                f"错失 {result['missed']} 个 (>45pips)"
            )
            if result["missed"] > 0:
                logger.info(f"📊 特征摘要:\n{result['summary']}")
        except Exception as e:
            logger.warning(f"🔍 错失机会扫描失败: {e}")

    def _opportunity_loop(self):
        """每日错失机会扫描（OPPORTUNITY_SCAN_HOUR CST）"""
        logger.info(f"🔍 错失机会扫描启动 (每日 {OPPORTUNITY_SCAN_HOUR:02d}:00 CST)")
        while self._running:
            wait = self._seconds_until_cst(OPPORTUNITY_SCAN_HOUR, 0)
            logger.debug(f"  距下次机会扫描 {wait/3600:.1f}h")
            for _ in range(int(wait // 60)):
                if not self._running:
                    break
                time.sleep(60)
            if not self._running:
                break
            rem = wait % 60
            if rem > 0:
                time.sleep(rem)
            if not self._running:
                break
            self._run_opportunity_scan()
            self._beat("opportunity", 30 * 3600)  # 每天, 30h 未跳=异常

    # ── 每周闭环分析 ──

    def _run_weekly_review(self) -> None:
        """运行每周错失机会闭环分析，结果推送飞书"""
        try:
            import subprocess
            script = Path(__file__).resolve().parent.parent.parent / "scripts" / "weekly_opportunity_review.py"
            result = subprocess.run(
                [".venv/bin/python3", str(script), "--weeks=1"],
                capture_output=True, text=True, timeout=60,
                cwd=str(Path(__file__).resolve().parent.parent.parent),
            )
            report = result.stdout.strip()
            if report:
                logger.info(f"📊 每周闭环分析完成\n{report[:500]}")
                # 推送飞书
                push_script = Path(__file__).resolve().parent.parent.parent / "scripts" / "feishu_push.sh"
                msg = f"📊 每周错失机会闭环分析\n{'─'*20}\n{report[:1500]}"
                subprocess.run(
                    ["bash", str(push_script), msg],
                    capture_output=True, text=True, timeout=30,
                )
                logger.info("✅ 每周闭环分析已推送")
        except Exception as e:
            logger.warning(f"📊 每周闭环分析失败: {e}")

    def _weekly_review_loop(self):
        """每周六上午运行闭环分析"""
        day_names = ["一", "二", "三", "四", "五", "六", "日"]
        logger.info(
            f"📊 每周闭环分析启动 (周{day_names[WEEKLY_REVIEW_DAY]} {WEEKLY_REVIEW_HOUR:02d}:00 CST)"
        )
        while self._running:
            now_cst = datetime.now(SH_TZ)
            # 计算到下一个周六 10:00 的秒数
            days_until = (WEEKLY_REVIEW_DAY - now_cst.weekday()) % 7
            if days_until == 0 and now_cst.hour >= WEEKLY_REVIEW_HOUR:
                days_until = 7  # 今天已经是周六且过了10点，等下周
            target = now_cst.replace(hour=WEEKLY_REVIEW_HOUR, minute=0, second=0, microsecond=0) + timedelta(days=days_until)
            wait = (target - now_cst).total_seconds()
            logger.debug(f"  距下次闭环分析 {wait/3600:.1f}h ({target.strftime('%m-%d %H:%M')} CST)")
            # 分片 sleep
            for _ in range(int(wait // 60)):
                if not self._running:
                    break
                time.sleep(60)
            if not self._running:
                break
            rem = wait % 60
            if rem > 0:
                time.sleep(rem)
            if not self._running:
                break
            self._run_weekly_review()
            self._beat("weekly", 9 * 86400)  # 每 7 天, 9 天未跳=异常

    # ── 飞书推送 ──

    @staticmethod
    def _seconds_until_cst(hour: int, minute: int) -> float:
        """计算到下一个上海时间 hour:minute 的秒数"""
        now_cst = datetime.now(SH_TZ)
        target = now_cst.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now_cst >= target:
            target += timedelta(days=1)
        return (target - now_cst).total_seconds()

    def _next_push_delay(self) -> float:
        """计算到下一个推送时间点的秒数（取最近的一个）"""
        delays = [self._seconds_until_cst(h, m) for h, m in PUSH_TIMES]
        return min(delays)

    def _handle_trade_execution(self, sig, direction: str) -> None:
        """MT4 自动下单 + 本地持仓记录（先下单成功再动本地状态，失败零副作用）

        顺序保证状态一致:
          1. 同向已有持仓 → 跳过（不重复下单）
          2. 先向 MT4 下新单；失败 → 本地记录完全不动，实盘/本地保持一致
          3. 下单成功 → 写本地新持仓记录
          4. 若有反方向持仓 → MT4 平仓；平仓失败 → 本地保留反方向记录并告警
             （由 _sync_mt4_positions 周期纠正）
        """
        try:
            from src.strategy.position_manager import PositionManager
            pm = PositionManager()
            existing = pm.get_active(direction)
            if existing:
                logger.info(
                    f"🔒 已有{direction}持仓(入场{existing.entry_price:.5f}), "
                    f"跳过重复下单"
                )
                return

            opposite = "BUY" if direction == "SELL" else "SELL"

            # 2) 先 MT4 下单（本地不动，失败零副作用）
            # 距离模式: 传 SL/TP 的 pips 距离, EA 按券商实际成交价换算 —
            # 消除 OANDA 行情与券商之间的点差/基差(数据校准)
            from src.execution.mt4_remote import MT4Remote
            mt4 = MT4Remote()
            direction_func = mt4.sell if direction == "SELL" else mt4.buy
            sl_pips = float(getattr(sig, "max_loss_pips", 0) or 0)
            tp_pips = abs(sig.take_profit_1 - sig.entry_price) * 10000
            try:
                result = direction_func(
                    symbol="EURUSD",
                    volume=sig.position_lots,
                    sl=sig.stop_loss,
                    tp=sig.take_profit_1,
                    sl_pips=sl_pips if sl_pips > 0 else None,
                    tp_pips=tp_pips if tp_pips > 0 else None,
                )
            except Exception as e:
                logger.warning(f"MT4下单异常(本地状态未变更): {e}")
                return
            if not result.get("success"):
                logger.warning(f"⚠️ MT4自动下单失败(本地状态未变更): {result.get('error')}")
                return

            logger.info(f"✅ MT4自动下单成功: {result.get('order_id')}")

            # 3) 下单成功 → 记录本地持仓
            pm.open_position(sig)

            # 4) 平反方向: 先 MT4 后本地；MT4 失败则本地保留记录等待同步纠正
            if pm.get_active(opposite):
                close_result = mt4.close(opposite, symbol="EURUSD")
                if close_result.get("success"):
                    pm.close_position(opposite)
                    logger.info(f"🔁 反向{opposite}持仓已在 MT4 平仓并同步本地")
                else:
                    logger.warning(
                        f"⚠️ MT4 平反向{opposite}失败: {close_result.get('error')} "
                        f"— 本地保留记录，请手动平仓"
                    )
        except Exception as e:
            logger.debug(f"持仓处理失败: {e}")

    # ── 熔断(EURUSD): 同方向连续止损N次 → 暂停该方向 ──

    def _fuse_path(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent / "data" / "eurusd_fuse.json"

    def _load_fuse(self) -> None:
        try:
            p = self._fuse_path()
            if p.exists():
                import json as _json
                saved = _json.loads(p.read_text(encoding="utf-8"))
                self._fuse = {
                    "loss_streak": {**self._fuse["loss_streak"],
                                    **saved.get("loss_streak", {})},
                    "paused_until": {**self._fuse["paused_until"],
                                     **saved.get("paused_until", {})},
                }
        except Exception:
            pass

    def _save_fuse(self) -> None:
        try:
            import json as _json
            p = self._fuse_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps(self._fuse, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        except Exception:
            pass

    def _eurusd_fuse_cfg(self) -> dict:
        """熔断配置: 中控台(exit段) > config.yaml(strategy.exit)"""
        try:
            from src.utils.config_loader import config
            from src.utils.dashboard_settings import load as ui_load
            yaml_cfg = config.load().get("strategy", {}).get("exit", {}) or {}
            ui = ui_load().get("exit", {}) or {}
            return {**yaml_cfg, **ui}
        except Exception:
            return {}

    def _eurusd_fused(self, direction: str) -> bool:
        """该方向是否处于熔断暂停期(冷却到期自动解除)"""
        cfg = self._eurusd_fuse_cfg()
        if not cfg.get("fuse_enabled", True):
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
            self._save_fuse()
            logger.info(f"⛔ 熔断冷却结束: {direction} 恢复交易")
            return False
        return True

    def _record_eurusd_fuse(self, direction: str, is_loss: bool) -> None:
        """平仓结算后更新熔断计数: 连续止损达到阈值 → 暂停该方向"""
        cfg = self._eurusd_fuse_cfg()
        if not cfg.get("fuse_enabled", True):
            return
        max_streak = int(cfg.get("fuse_max_loss_streak", 2))
        cooldown = int(cfg.get("fuse_cooldown_hours", 48))
        if is_loss:
            streak = self._fuse["loss_streak"].get(direction, 0) + 1
            self._fuse["loss_streak"][direction] = streak
            if streak >= max_streak:
                self._fuse["paused_until"][direction] = (
                    datetime.now(SH_TZ) + timedelta(hours=cooldown)
                ).isoformat()
                self._fuse["loss_streak"][direction] = 0
                logger.info(
                    f"⛔ 熔断触发: {direction} 连续{streak}次止损 → "
                    f"暂停该方向 {cooldown} 小时"
                )
                self._push_feishu(
                    f"⛔ EURUSD 熔断提醒\n{'─' * 20}\n"
                    f"方向 {direction} 连续 {streak} 次止损\n"
                    f"→ 已暂停该方向 {cooldown} 小时\n"
                    f"(防趋势反转期连续亏损)"
                )
        else:
            self._fuse["loss_streak"][direction] = 0
        self._save_fuse()

    @staticmethod
    def _judge_exit_type(pos) -> bool:
        """平仓类型判定: 当前价离SL近→止损(True), 离TP近→止盈(False)

        固定TP/SL系统平仓时价格必在触发位置附近, 距离判定准确率足够。
        判定失败(数据缺失)返回 False — 保守不误伤熔断计数。
        """
        try:
            from src.data_collection.oanda import OandaClient
            m15 = OandaClient().load_parquet("M15")
            close = float(m15.iloc[-1]["close"])
        except Exception:
            return False
        if not pos.stop_loss or not pos.take_profit_1:
            return False
        d_sl = abs(close - pos.stop_loss)
        d_tp = abs(close - pos.take_profit_1)
        return d_sl <= d_tp

    def _max_same_dir(self) -> int:
        """同方向最大同时持仓数: 中控台 > config > 默认3"""
        try:
            from src.utils.dashboard_settings import load as ui_load
            v = ui_load().get("trade", {}).get("max_same_dir")
            if v is not None:
                return int(v)
        except Exception:
            pass
        try:
            from src.utils.config_loader import config
            return int(config.load().get("strategy", {}).get("max_same_dir", 3))
        except Exception:
            return 3

    def _prune_signal_stack(self, now_ts: float) -> None:
        """移除超过生命周期的信号(视为已退出叠加)"""
        self._signal_stack = [e for e in self._signal_stack
                              if now_ts - e.get("ts", 0) < self._signal_life_span]

    def _push_signal_if_slot(self, sig) -> bool:
        """同向叠加上限检查:
        有空位 → 返回 True(正常推送/下单; 槽位由调用方在推送成功后占用)
        已达上限 → 只发"趋势延续"提示, 不当作新开仓, 返回 False
        """
        direction = "SELL" if "做空" in sig.direction else "BUY"
        now_ts = time.time()
        self._prune_signal_stack(now_ts)
        active = sum(1 for e in self._signal_stack if e["direction"] == direction)
        maxd = self._max_same_dir()
        if active >= maxd:
            try:
                self._push_feishu(
                    f"🌊 趋势延续提示\n"
                    f"方向 {direction} 近48h 已有 {active} 单(上限{maxd}单)\n"
                    f"→ 不再新增开仓，可考虑锁利/减仓"
                )
            except Exception as e:
                logger.warning(f"延续提示推送失败: {e}")
            logger.info(f"🌊 同方向已达上限({maxd}单, 现{active}单), 转为延续提示不新增")
            return False
        return True

    def _maybe_push_signal_alert(self, sig) -> bool:
        """可交易信号实时飞书推送（30分钟冷却 + 方向变化立即推）

        Returns:
            True=本次成功推送(占用一个同向持仓槽位), False=未推送(冷却/失败)
        """
        now = datetime.now(timezone.utc)

        # 冷却检查：30分钟内同方向不重复推
        # 注意: sig.direction 是中文标签("🟢 做多"/"🔴 做空")，须按中文判定
        if self._last_signal_alert_time is not None:
            elapsed = (now - self._last_signal_alert_time).total_seconds()
            same_direction = (
                ("做多" in sig.direction and "做多" in self._last_alert_direction) or
                ("做空" in sig.direction and "做空" in self._last_alert_direction)
            )
            if elapsed < self._signal_alert_cooldown and same_direction:
                logger.info(
                    f"🔇 信号推送冷却中 "
                    f"(上次={self._last_alert_direction} {elapsed:.0f}s前, "
                    f"方向相同，跳过)"
                )
                return False

        try:
            now_str = datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M GMT+8")
            signal_text = self.generator.format_markdown(sig)

            # 紧凑版信号消息
            msg = (
                f"🚨 入场信号!\n"
                f"{'─' * 20}\n"
                f"⏰ {now_str}\n\n"
                f"{signal_text}"
            )

            if self._push_feishu(msg):
                self._last_signal_alert_time = now
                self._last_alert_direction = sig.direction
                logger.info(f"🚀 实时信号已推送飞书: {sig.direction}")
                return True
            logger.warning("❌ 实时信号推送失败(飞书脚本返回非0)")
            return False
        except Exception as e:
            logger.error(f"❌ 实时信号推送异常: {e}")
            return False

    @staticmethod
    def _push_feishu(msg: str) -> bool:
        """飞书推送（含重试 + 推送日志监控）"""
        proj_dir = Path(__file__).resolve().parent.parent.parent
        push_script = proj_dir / "scripts" / "feishu_push.sh"
        # 推送监控日志: 记录每次推送的内容摘要, 便于回溯"为什么推了这条消息"
        try:
            import inspect as _inspect
            caller = ""
            try:
                caller = _inspect.currentframe().f_back.f_code.co_name
            except Exception:
                pass
            first_line = (msg or "").split("\n")[0][:60]
            log_dir = proj_dir / "data"
            with open(log_dir / "feishu_push.log", "a", encoding="utf-8") as f:
                f.write(
                    f"{datetime.now(SH_TZ).strftime('%Y-%m-%d %H:%M:%S')} "
                    f"| {caller or '?'} | {first_line}\n"
                )
        except Exception:
            pass
        for attempt in range(3):
            try:
                result = subprocess.run(
                    ["bash", str(push_script), msg],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    return True
                if attempt < 2:
                    time.sleep(5)
            except Exception:
                if attempt < 2:
                    time.sleep(5)
        return False

    # ── 持仓 TP 接近 + H4 反转 检查 ──

    def _maybe_check_positions(self) -> None:
        """检查活跃持仓: TP接近→再评估, 反转+浮盈→自动平仓, H4反转→出场信号"""
        try:
            from src.strategy.position_manager import PositionManager
            pm = PositionManager()

            # 1) TP 接近再评估
            suggestion = pm.check_tp_proximity()
            if suggestion:
                logger.info(
                    f"🔍 TP接近: {suggestion['position'].direction} "
                    f"进度={suggestion['progress_pct']}% → {suggestion['action']}"
                )
                self._push_feishu(suggestion["suggestion"])

            # 2) 反转信号 + 浮盈 → 自动平仓（保本出场）
            # 配置优先级: 中控台(dashboard_settings) > config.yaml — 面板改动无需重启
            try:
                from src.utils.config_loader import config
                from src.utils.dashboard_settings import load as ui_load
                exit_yaml = config.load().get("strategy", {}).get("exit", {})
                exit_cfg = {**exit_yaml, **(ui_load().get("exit", {}) or {})}
                if exit_cfg.get("reversal_auto_close", True):
                    min_profit = float(exit_cfg.get("min_profit_pips", 0))
                    auto = pm.check_reversal_auto_close(min_profit_pips=min_profit)
                    if auto:
                        self._execute_auto_close(pm, auto)
                        return  # 已平仓, 后续检查无持仓
            except Exception as e:
                logger.debug(f"反转自动平仓检查跳过: {e}")

            # 3) H4 反转形态 → 出场信号（覆盖浮亏时只推建议的场景）
            reversal = pm.check_h4_reversal()
            if reversal:
                logger.info(
                    f"🔄 H4反转: {reversal['position'].direction} "
                    f"形态={reversal['pattern']} → exit_reversal"
                )
                self._push_feishu(reversal["suggestion"])
                logger.info(f"📤 H4 反转出场信号已推送")

        except Exception as e:
            logger.debug(f"持仓检查跳过: {e}")

    def _execute_auto_close(self, pm, auto: dict) -> None:
        """执行反转自动平仓: MT4 平仓指令 → 本地记录 → 飞书通知

        平仓指令被 relay 接受即删本地记录(防下一周期重复平仓);
        若 EA 实际执行失败, _sync_mt4_positions 的补记逻辑会把持仓补回。
        """
        pos = auto["position"]
        direction = pos.direction
        direction_cn = "做空" if direction == "SELL" else "做多"
        try:
            from src.execution.mt4_remote import MT4Remote
            mt4 = MT4Remote()
            result = mt4.close(direction, symbol="EURUSD")
            if result.get("success"):
                pm.close_position(direction)
                logger.info(
                    f"🚪 反转自动平仓: {direction} @ {auto['current_price']:.5f} "
                    f"浮盈 +{auto['float_pips']}pips ({auto['pattern']})"
                )
                self._push_feishu(
                    f"🚪 反转信号自动平仓(保本出场)\n"
                    f"{'─' * 20}\n"
                    f"方向: {direction_cn}\n"
                    f"形态: {auto['pattern']}\n"
                    f"原因: {auto['reason'][:60]}\n"
                    f"\n"
                    f"💰 平仓价: {auto['current_price']:.5f}\n"
                    f"💵 落袋浮盈: +{auto['float_pips']:.1f} pips\n"
                    f"\n"
                    f"⚡ 已自动平仓, 等待下一入场信号"
                )
            else:
                logger.warning(
                    f"⚠️ 反转自动平仓指令失败: {result.get('error')} — 本地保留持仓"
                )
                self._push_feishu(
                    f"⚠️ 反转信号出现但自动平仓失败\n"
                    f"{'─' * 20}\n"
                    f"方向: {direction_cn} | 形态: {auto['pattern']}\n"
                    f"浮盈 +{auto['float_pips']}pips\n"
                    f"失败原因: {result.get('error', '未知')}\n"
                    f"→ 请手动检查 MT4 持仓"
                )
        except Exception as e:
            logger.warning(f"⚠️ 反转自动平仓异常: {e}")

    # ── 双向信号诊断 ──

    def _signal_diagnostic(self) -> str:
        """生成双向信号检测摘要（附在定时简报中）"""
        try:
            from src.strategy.entry import BUY, SELL
            import io, logging

            # 临时捕获 entry.py 的日志输出
            entry_log = io.StringIO()
            handler = logging.StreamHandler(entry_log)
            handler.setLevel(logging.INFO)
            entry_logger = logging.getLogger("src.strategy.entry")
            entry_logger.addHandler(handler)

            try:
                sell_sig = self.generator.entry._detect_sell()
                buy_sig = self.generator.entry._detect_buy()
            finally:
                entry_logger.removeHandler(handler)

            log_output = entry_log.getvalue()
            lines = []

            # 解析日志提取每步状态
            sell_steps = {"①": "?", "②": "?", "③": "?", "④": "?", "⛔": ""}
            buy_steps = {"①": "?", "②": "?", "③": "?", "④": "?", "⛔": ""}
            current_dir = None

            for line in log_output.split("\n"):
                if "swing high" in line and "M15" in line:
                    current_dir = "SELL"
                    sell_steps["①"] = "✅"
                elif "swing low" in line and "M15" in line:
                    current_dir = "BUY"
                    buy_steps["①"] = "✅"
                elif "未找到有效波段点" in line:
                    if current_dir == "SELL":
                        sell_steps["①"] = "❌"
                    elif current_dir == "BUY":
                        buy_steps["①"] = "❌"
                elif "未形成" in line or "太旧" in line:
                    # Step ② fail
                    pass  # handled below
                elif "EMA 空头" in line and "✅" in line:
                    sell_steps["②"] = "✅"
                elif "EMA 多头" in line and "✅" in line:
                    buy_steps["②"] = "✅"
                elif "回弹高点" in line and "✅" in line:
                    sell_steps["③"] = "✅"
                elif "回踩低点" in line and "✅" in line:
                    buy_steps["③"] = "✅"
                elif "反转形态" in line and "✅" in line:
                    if current_dir == "SELL" or "空头" in line or "大阴线" in line:
                        sell_steps["④"] = "✅"
                    elif current_dir == "BUY" or "多头" in line or "大阳线" in line:
                        buy_steps["④"] = "✅"
                elif "坚决大阴线" in line and "✅" in line:
                    sell_steps["④"] = "✅"
                elif "坚决大阳线" in line and "✅" in line:
                    buy_steps["④"] = "✅"
                elif "ATR" in line or "距 H4" in line or "RSI" in line or "止损" in line or "R:R" in line:
                    if "⛔" in line or "跳过" in line:
                        tag = "⛔" + line.strip()[:30]
                        if current_dir == "SELL":
                            sell_steps["⛔"] = tag
                        elif current_dir == "BUY":
                            buy_steps["⛔"] = tag

            # 从日志提取未满足的原因
            for line in log_output.split("\n"):
                if "未形成" in line and "aligned=" in line:
                    if current_dir == "SELL" or "空头" in line:
                        sell_steps["②"] = "❌"
                    elif current_dir == "BUY" or "多头" in line:
                        buy_steps["②"] = "❌"
                elif "回弹" in line and ("无有效" in line or "无力度" in line):
                    sell_steps["③"] = "❌"
                elif "回踩" in line and ("无有效" in line or "无力度" in line):
                    buy_steps["③"] = "❌"
                elif "未出现" in line and ("反转" in line or "坚决" in line):
                    if current_dir == "SELL":
                        sell_steps["④"] = "❌"
                    elif current_dir == "BUY":
                        buy_steps["④"] = "❌"
                elif "未找到" in line and "波段点" in line:
                    pass  # already handled

            sell_status = "".join(sell_steps[k] for k in ["①","②","③","④"])
            buy_status = "".join(buy_steps[k] for k in ["①","②","③","④"])

            sell_block = sell_steps.get("⛔", "")
            buy_block = buy_steps.get("⛔", "")

            if sell_sig:
                sell_line = f"  🔴 做空: ✅ 已触发! {sell_sig.entry_price:.5f}"
            elif sell_status == "✅✅✅✅":
                sell_line = f"  🔴 做空: ①②③④全过但被拦截 {sell_block}"
            else:
                sell_line = f"  🔴 做空: {sell_status}"

            if buy_sig:
                buy_line = f"  🟢 做多: ✅ 已触发! {buy_sig.entry_price:.5f}"
            elif buy_status == "✅✅✅✅":
                buy_line = f"  🟢 做多: ①②③④全过但被拦截 {buy_block}"
            else:
                buy_line = f"  🟢 做多: {buy_status}"

            return f"\n🔍 信号诊断\n{sell_line}\n{buy_line}\n"
        except Exception as e:
            return f"\n🔍 信号诊断: 获取失败 ({e})\n"

    # ── VPS 连接监测 ──

    def _check_vps_health(self) -> None:
        """检测 MT4 中继 VPS 连通性"""
        try:
            from src.utils.dashboard_settings import load
            cfg = load().get("mt4_relay", {})
            url = cfg.get("url", "").rstrip("/")
            if not url:
                self._vps_status = {"http": False, "tcp": False, "ea": False, "latency_ms": 0, "last_check": "", "checks": 0, "fails": 0}
                return

            import socket, time as _time
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host = parsed.hostname

            t0 = _time.time()
            ok = False
            ea = False

            # HTTP check
            try:
                import httpx
                resp = httpx.get(f"{url}/health", timeout=5)
                if resp.status_code == 200:
                    ok = True
                    ea = resp.json().get("ea_connected", False)
            except Exception:
                pass

            # TCP check (端口取 relay URL 实际端口, 默认 8080)
            tcp_ok = False
            try:
                port = parsed.port or 8080
                s = socket.socket(); s.settimeout(3)
                s.connect((host, port)); s.close()
                tcp_ok = True
            except Exception:
                pass

            ms = round((_time.time() - t0) * 1000)
            self._vps_status = {
                "http": ok, "tcp": tcp_ok, "ea": ea,
                "latency_ms": ms,
                "last_check": datetime.now(SH_TZ).strftime("%H:%M:%S"),
                "checks": self._vps_status.get("checks", 0) + 1,
                "fails": self._vps_status.get("fails", 0) + (0 if ok else 1),
            }
        except Exception:
            pass

    # ── MT4 持仓同步 ──

    def _sync_mt4_positions(self) -> None:
        """从 VPS relay 拉取 MT4 实际持仓，同步本地 active_positions"""
        try:
            from src.utils.dashboard_settings import load
            cfg = load().get("mt4_relay", {})
            url = cfg.get("url", "").rstrip("/")
            if not url or not cfg.get("enabled", False):
                return

            import httpx
            resp = httpx.get(f"{url}/positions", timeout=5)
            if resp.status_code != 200:
                return
            mt4_positions = resp.json().get("positions", [])
            # 只关心 EURUSD — 黄金等其它品种持仓不得影响本模块的状态判断
            mt4_positions = [
                p for p in mt4_positions
                if str(p.get("symbol", "EURUSD")).upper() in ("EURUSD", "EUR/USD")
            ]

            from src.strategy.position_manager import PositionManager
            pm = PositionManager()
            local = pm.load_all()

            # 本地持仓的方向集合
            local_directions = {p.direction for p in local}

            mt4_directions = {("SELL" if p.get("type") == "SELL" else "BUY") for p in mt4_positions}

            # 本地有但 MT4 没有的方向 → 已平仓，关闭本地记录
            for direction in local_directions - mt4_directions:
                pos = pm.get_active(direction)
                if pos is not None:
                    # 熔断计数: 判定平仓类型(止损/止盈)
                    is_sl = self._judge_exit_type(pos)
                    self._record_eurusd_fuse(direction, is_sl)
                pm.close_position(direction)
                logger.info(f"🔄 MT4同步: 本地{direction}持仓已不存在, 已关闭本地记录")

            # MT4 有但本地没有的方向 → 补记本地持仓
            # (EA延迟成交/手动单 — 补记后锁利建议/持仓卡片/反向平仓才能工作)
            for direction in mt4_directions - local_directions:
                actual = next(
                    (m for m in mt4_positions
                     if m.get("type") == direction and m.get("open")),
                    None,
                )
                if not actual:
                    continue
                try:
                    from src.strategy.position_manager import ActivePosition
                    open_px = float(actual["open"])
                    sl = float(actual.get("sl") or 0)
                    tp = float(actual.get("tp") or 0)
                    pm._save(pm.load_all() + [ActivePosition(
                        signal_time=datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M GMT+8"),
                        direction=direction,
                        entry_price=open_px,
                        stop_loss=sl if sl > 0 else open_px,
                        take_profit_1=tp if tp > 0 else open_px,
                        take_profit_2=tp if tp > 0 else open_px,
                        sl_pips=abs(open_px - sl) * 10000 if sl > 0 else 0,
                        tp1_pips=abs(tp - open_px) * 10000 if tp > 0 else 0,
                        entry_reason="MT4同步补记(手动单或延迟成交)",
                        trend_at_entry="",
                        created_at=datetime.now(SH_TZ).isoformat(),
                    )])
                    logger.info(f"🔄 MT4同步: 补记本地{direction}持仓 @{open_px:.5f}")
                except Exception as e:
                    logger.warning(f"🔄 MT4同步: 补记{direction}失败: {e}")

            # 数据校准: 用 MT4 实际成交价/SL/TP 回写本地记录 —
            # 后台显示与平台一致(行情源与券商之间存在点差)
            updated_dirs = pm.sync_actual_prices(mt4_positions)
            if updated_dirs:
                logger.info(f"📊 数据校准: 已回写 {updated_dirs} 持仓的MT4实际成交价/SL/TP")
        except Exception as e:
            logger.debug(f"MT4持仓同步跳过: {e}")

    # ── 定期持仓提醒 ──

    def _maybe_push_position_reminder(self) -> None:
        """每 4 小时推送一次持仓状态卡片（仅在有活跃持仓时）"""
        now = datetime.now(timezone.utc)

        if self._last_position_reminder_time is not None:
            elapsed = (now - self._last_position_reminder_time).total_seconds()
            if elapsed < self._position_reminder_interval:
                return  # 还没到时间

        try:
            from src.strategy.position_manager import PositionManager
            pm = PositionManager()
            card = pm.get_status_card()
            if card is None:
                return  # 无活跃持仓

            proj_dir = Path(__file__).resolve().parent.parent.parent
            push_script = proj_dir / "scripts" / "feishu_push.sh"
            result = subprocess.run(
                ["bash", str(push_script), card],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                self._last_position_reminder_time = now
                logger.info("📤 持仓状态卡片已推送")
            else:
                logger.warning(f"持仓提醒推送失败: {result.stderr.strip()}")
        except Exception as e:
            logger.debug(f"持仓提醒跳过: {e}")

    # ── 定时简报 ──

    def _run_briefing(self, label: str) -> None:
        """执行一次简报生成 + 飞书推送（有重大事件时刷新 LLM 情绪）"""
        try:
            now_str = datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M GMT+8")

            # 今日有重大事件 → 刷新 LLM 情绪分析（避免用过时情绪数据）
            try:
                from src.data_collection.calendar import EconomicCalendar
                cal = EconomicCalendar()
                ev = cal.fetch()
                eur = cal.get_eur_usd_high_impact(ev)
                high_today = [e for e in eur if e.get("impact", "").lower() == "high"]
                if high_today:
                    from src.analysis.llm_sentiment import LLMSentimentAnalyzer
                    analyzer = LLMSentimentAnalyzer()
                    existing = analyzer.get_today_result()
                    # 如果今天还没分析过，或者上次分析超过 6 小时 → 刷新
                    if not existing:
                        logger.info("📰 今日有重大事件，首次 LLM 分析...")
                        analyzer.analyze()
                    elif existing.get("analyzed_at"):
                        try:
                            last = datetime.fromisoformat(existing["analyzed_at"])
                            if (datetime.now(timezone.utc) - last).total_seconds() > 6 * 3600:
                                logger.info("📰 LLM 情绪超过 6h，有重大事件，刷新...")
                                analyzer.analyze()
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"LLM 刷新检查跳过: {e}")

            # 生成信号
            sig = self.generator.generate()
            signal_text = self.generator.format_markdown(sig)

            # 当前价格
            price_line = ""
            try:
                from src.data_collection.oanda import OandaClient
                c = OandaClient()
                d = c.fetch_candles("H1", 1)
                if d:
                    price_line = f"\n💰 EUR/USD 当前: {d[-1]['close']:.5f}\n"
            except Exception:
                pass

            # 日历事件
            cal_lines = ""
            try:
                from src.data_collection.calendar import EconomicCalendar
                cal = EconomicCalendar()
                ev = cal.fetch()
                eur = cal.get_eur_usd_high_impact(ev)
                high = [e for e in eur if e.get("impact", "").lower() == "high"]
                if high:
                    cal_lines = f"\n📅 高影响事件 ({len(high)}):\n"
                    for e in high[:3]:
                        cal_lines += f"  ⚠️ {e['date']} {e['currency']} {e['event'][:40]}\n"
            except Exception:
                pass

            # 大势
            trend_line = ""
            try:
                from src.strategy.trend import TrendAnalyzer
                t = TrendAnalyzer()
                trend_line = f"\n📊 {t.format_one_liner()}\n"
            except Exception:
                pass

            # 🔍 双向信号诊断
            diag_line = ""
            try:
                diag_line = self._signal_diagnostic()
            except Exception:
                pass

            # 组装消息（短分隔线适配手机屏幕）
            if "早" in label:
                header = "🌅 早间简报"
            elif "亚" in label:
                header = "☀️ 亚盘总结"
            else:
                header = "🌙 晚间简报"
            msg = (
                f"{header}\n"
                f"{'─' * 20}\n"
                f"⏰ {now_str}\n\n"
                f"📊 今日信号\n{signal_text}\n"
                f"{price_line}"
                f"{cal_lines}"
                f"{trend_line}"
                f"{diag_line}"
            )

            # 推送飞书
            if self._push_feishu(msg):
                self._last_push_time = datetime.now(timezone.utc)
                logger.info(f"✅ {header}推送成功")
            else:
                logger.warning(f"❌ {header}推送失败(飞书脚本返回非0)")
        except Exception as e:
            logger.error(f"❌ {label}简报失败: {e}")

    def _push_loop(self):
        """定时飞书推送循环"""
        if not PUSH_TIMES:
            logger.info("📱 定时简报已禁用（全自动交易模式），仅保留信号推送")
            return
        times_str = " + ".join(f"{h:02d}:{m:02d}" for h, m in PUSH_TIMES)
        logger.info(f"📱 飞书推送启动 ({times_str} CST)")
        while self._running:
            wait = self._next_push_delay()
            logger.info(f"📱 距下次推送 {wait/3600:.1f}h")
            # 分片 sleep，便于快速 shutdown
            for _ in range(int(wait // 60)):
                if not self._running:
                    break
                time.sleep(60)
            if not self._running:
                break
            # sleep 剩余秒数
            remainder = wait % 60
            if remainder > 0:
                time.sleep(remainder)
            if not self._running:
                break
            # 判断时段
            now_cst = datetime.now(SH_TZ)
            if now_cst.hour < 10:
                label = "早间"
            elif now_cst.hour < 18:
                label = "亚盘总结"
            else:
                label = "晚间"
            self._run_briefing(label)
            self._beat("push", 12 * 3600)  # 每 ~8h, 12h 未跳=异常

    def start(self):
        """启动守护进程"""
        self._running = True
        logger.info("=" * 50)
        logger.info("🚀 外汇量化交易守护进程启动")
        logger.info(f"   信号评估: 每 {SIGNAL_INTERVAL // 60} 分钟（实盘 OANDA 实时刷新）")
        logger.info(f"   自动复盘: 每 {REVIEW_INTERVAL//3600} 小时")
        logger.info(f"   新闻+基本面: 每 {NEWS_INTERVAL//3600} 小时")
        times_str = " / ".join(f"{h:02d}:{m:02d}" for h, m in PUSH_TIMES)
        logger.info(f"   飞书推送: {times_str} CST (早间/亚盘总结/晚间)")
        logger.info(f"   错失机会扫描: 每日 {OPPORTUNITY_SCAN_HOUR:02d}:00 CST")
        logger.info(f"   每周闭环分析: 周{WEEKLY_REVIEW_DAY+1} {WEEKLY_REVIEW_HOUR:02d}:00 CST")
        logger.info("=" * 50)

        # 初始化心跳（启动时全部标记为 alive）
        now = datetime.now(timezone.utc)
        self._heartbeats = {
            "signal": (now, 90 * 60),
            "review": (now, 12 * 3600),
            "news": (now, 4 * 3600),
            "push": (now, 12 * 3600),
            "opportunity": (now, 30 * 3600),
            "weekly": (now, 9 * 86400),
        }

        # 启动推送线程
        push_thread = threading.Thread(target=self._push_loop, daemon=True)
        push_thread.start()

        # 启动机会扫描线程
        opp_thread = threading.Thread(target=self._opportunity_loop, daemon=True)
        opp_thread.start()

        # 启动每周闭环分析线程
        weekly_thread = threading.Thread(target=self._weekly_review_loop, daemon=True)
        weekly_thread.start()

        # 启动新闻采集线程
        news_thread = threading.Thread(target=self._news_loop, daemon=True)
        news_thread.start()

        # 启动信号线程
        signal_thread = threading.Thread(target=self._signal_loop, daemon=True)
        signal_thread.start()

        # 启动复盘线程
        review_thread = threading.Thread(target=self._review_loop, daemon=True)
        review_thread.start()

        return signal_thread, review_thread, news_thread, push_thread

    def stop(self):
        """停止守护进程"""
        self._running = False
        logger.info("🛑 守护进程停止")

    def _beat(self, name: str, max_interval: float) -> None:
        """记录线程心跳"""
        self._heartbeats[name] = (datetime.now(timezone.utc), max_interval)

    def _check_thread_health(self) -> dict:
        """检查所有线程健康状态"""
        now = datetime.now(timezone.utc)
        threads = {}
        for name, (last_beat, max_sec) in self._heartbeats.items():
            elapsed = (now - last_beat).total_seconds()
            healthy = elapsed < max_sec
            threads[name] = {
                "healthy": healthy,
                "last_beat_seconds_ago": round(elapsed),
                "max_interval_seconds": max_sec,
            }
        return threads

    @property
    def status(self) -> dict:
        """获取运行状态"""
        summary = self.history.summary(7)
        cfg = self._eurusd_fuse_cfg()
        return {
            "running": self._running,
            "eval_count": self._eval_count,
            "history": summary,
            "last_news_collection": self._last_news_time.isoformat() if self._last_news_time else None,
            "last_push": self._last_push_time.isoformat() if self._last_push_time else None,
            "threads": self._check_thread_health(),
            "api": {
                "last_call": self._last_api_call,
                "latency_ms": self._last_api_latency_ms,
                "quote": self._last_api_quote,
                "status": self._last_api_status,
            },
            "vps": self._vps_status,
            "fuse": {
                "enabled": cfg.get("fuse_enabled", True),
                "max_streak": int(cfg.get("fuse_max_loss_streak", 2)),
                "cooldown_hours": int(cfg.get("fuse_cooldown_hours", 48)),
                "loss_streak": dict(self._fuse["loss_streak"]),
                "paused_until": dict(self._fuse["paused_until"]),
            },
        }
