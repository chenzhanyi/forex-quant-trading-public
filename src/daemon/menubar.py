"""macOS 菜单栏状态图标 — 显示 daemon 运行状态 + 快捷操作"""
import json
import logging
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import rumps

logger = logging.getLogger(__name__)

PROJ = Path(__file__).resolve().parent.parent.parent
API_BASE = "http://127.0.0.1:5001"


def fetch_menu_status() -> dict:
    """聚合状态 — 一次请求拿全(daemon/gold/aud/market/proxy)

    菜单栏渲染在 AppKit 主线程, 多请求串行会冻结界面 —
    聚合接口单请求几十毫秒返回, 避免卡顿。
    """
    t0 = time.time()
    try:
        import urllib.request
        # 本地回环请求强制直连: macOS 系统代理开启时 urllib 会把 127.0.0.1
        # 也发给代理 → 502 Bad Gateway(菜单栏灯灭根因)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"{API_BASE}/api/menu/status", method="GET")
        with opener.open(req, timeout=10) as resp:
            data = json.loads(resp.read())
            logger.debug(f"menu/status 请求成功 {int((time.time()-t0)*1000)}ms")
            return data.get("data", {})
    except Exception as e:
        logger.error(f"menu/status 请求失败 ({int((time.time()-t0)*1000)}ms): {type(e).__name__}: {e}")
        return {}


class ForexTradingApp(rumps.App):
    """外汇量化交易 — 菜单栏应用"""

    def __init__(self):
        super().__init__(
            name="外汇量化",
            title="📊",
            quit_button=None,  # 自定义退出逻辑
        )
        self._refresh_timer = None
        self._poll_count = 0
        logger.info("📊 菜单栏应用初始化")
        self._start_polling()

    def _start_polling(self):
        """每 10 秒轮询一次状态"""
        self._refresh_timer = rumps.Timer(self._refresh_menu, 10)
        self._refresh_timer.start()
        logger.info("⏱ 菜单栏轮询定时器已启动 (每10秒)")

    def _refresh_menu(self, _=None):
        """刷新菜单内容"""
        self._poll_count += 1
        t0 = time.time()
        # 调试日志: 前30次全打 + 之后每60次打一次(排查菜单栏挂起用)
        if self._poll_count <= 30 or self._poll_count % 60 == 0:
            alive = self._refresh_timer.is_alive() if self._refresh_timer else "?"
            logger.info(f"🔄 菜单栏轮询 #{self._poll_count} (Timer alive={alive})")
        try:
            self._render()
        except Exception:
            import traceback
            logger.error(f"菜单栏渲染异常 (轮询#{self._poll_count}):\n{traceback.format_exc()}")
        if self._poll_count <= 30 or self._poll_count % 60 == 0:
            logger.info(f"✅ 轮询 #{self._poll_count} 完成 ({int((time.time()-t0)*1000)}ms)")

    def _render(self):
        """渲染菜单内容(实际逻辑)"""
        self.menu.clear()

        # ── 聚合状态(单请求) ──
        all_status = fetch_menu_status()
        daemon_status = all_status.get("daemon", {})
        running = daemon_status.get("running", False)
        eval_count = daemon_status.get("eval_count", 0)
        threads = daemon_status.get("threads", {})

        if running:
            self.menu.add(rumps.MenuItem(f"🟢 Daemon 运行中 | 信号 #{eval_count}"))
        else:
            self.menu.add(rumps.MenuItem("🔴 Daemon 未运行"))

        self.menu.add(rumps.separator)

        # ── 统一调度(每15分钟拉三品种数据→依次评估) ──
        market = all_status.get("market", {})
        tick_str = "?"
        if market.get("last_tick"):
            try:
                from datetime import datetime, timezone, timedelta
                t = datetime.fromisoformat(market["last_tick"])
                tick_str = t.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M")
            except Exception:
                tick_str = "?"
        self.menu.add(rumps.MenuItem(f"🔄 统一调度: {tick_str} 三品种拉取+评估"))

        # ── 三品种数据新鲜度 ──
        symbols = market.get("symbols", {})
        name_map = {"EUR_USD": "欧美", "XAU_USD": "黄金", "AUD_USD": "澳元"}
        parts = []
        for sym in ["EUR_USD", "XAU_USD", "AUD_USD"]:
            s = symbols.get(sym, {})
            if s.get("fresh"):
                parts.append(f"{name_map[sym]}🟢")
            elif s.get("age_min") is not None:
                parts.append(f"{name_map[sym]}🔴{s['age_min']}min")
            else:
                parts.append(f"{name_map[sym]}⚫")
        self.menu.add(rumps.MenuItem("📊 数据: " + " | ".join(parts)))

        # ── 专用代理(系统级基础设施, 三品种数据/日历/RSS 共用) ──
        proxy = all_status.get("proxy", {})
        proc = proxy.get("process", {})
        pmode = proxy.get("mode", "off")
        if proc.get("running"):
            self.menu.add(rumps.MenuItem(f"🌐 专用代理 🟢 端口{proc.get('port')} (mode={pmode})"))
        elif pmode and pmode != "off":
            self.menu.add(rumps.MenuItem(f"🌐 专用代理 🔴 未运行 (mode={pmode})"))
        else:
            self.menu.add(rumps.MenuItem("🌐 专用代理 ⚫ 关闭"))

        self.menu.add(rumps.separator)

        # ── 熔断状态汇总(三引擎) ──
        fuse_lines = []
        eur_fuse = daemon_status.get("fuse", {}) or {}
        for d in ["SELL", "BUY"]:
            pu = (eur_fuse.get("paused_until") or {}).get(d)
            if pu:
                fuse_lines.append(f"欧美{d}⛔")
        for name, key in [("🥇黄金", "gold"), ("🦘澳元", "aud")]:
            st = all_status.get(key, {}) or {}
            f = st.get("fuse", {}) or {}
            for d in ["SELL", "BUY"]:
                if (f.get("paused_until") or {}).get(d):
                    fuse_lines.append(f"{name}{d}⛔")
        if fuse_lines:
            self.menu.add(rumps.MenuItem("⛔ 熔断暂停: " + " ".join(fuse_lines)))

        # ── OANDA API 调用状态 ──
        api = daemon_status.get("api", {})
        if api:
            last_call = api.get("last_call", "?")
            latency = api.get("latency_ms", 0)
            quote = api.get("quote", 0)
            status = api.get("status", "?")

            if status == "OK":
                self.menu.add(rumps.MenuItem(
                    f"📡 API {last_call} | {latency:.0f}ms | EUR/USD {quote:.5f}"
                ))
            else:
                self.menu.add(rumps.MenuItem(
                    f"📡 API {last_call} | ⚠️ {status}"
                ))
        else:
            self.menu.add(rumps.MenuItem("📡 API 未调用"))

        # ── VPS 中继状态 ──
        vps = daemon_status.get("vps", {})
        if vps and vps.get("last_check"):
            http_ok = vps.get("http", False)
            ea_ok = vps.get("ea", False)
            ms = vps.get("latency_ms", 0)
            last = vps.get("last_check", "?")
            icon = "🟢" if http_ok else "🔴"
            ea_str = "EA✅" if ea_ok else "EA⚫"
            self.menu.add(rumps.MenuItem(
                f"🖥 VPS {icon} {ea_str} | {ms}ms | {last}"
            ))
        else:
            self.menu.add(rumps.MenuItem("🖥 VPS ⚫ 未配置"))

        self.menu.add(rumps.separator)

        # ── 黄金交易状态 ──
        gold = all_status.get("gold", {})
        if gold:
            g_en = gold.get("enabled", False)
            g_at = gold.get("auto_trade", False)
            g_open = gold.get("open_count", 0)
            g_max = gold.get("max_open", 2)
            g_eval = gold.get("eval_count", 0)
            g_run = gold.get("running", False)
            if g_en:
                mode = "自动下单✅" if g_at else "仅信号🔸"
                self.menu.add(rumps.MenuItem(f"🥇 黄金 🟢 交易中 | {mode}"))
            else:
                self.menu.add(rumps.MenuItem("🥇 黄金 🔴 未开启"))
            self.menu.add(rumps.MenuItem(
                f"   在途 {g_open}/{g_max}单 | 评估 {g_eval}次 | {'运行🟢' if g_run else '停止🔴'}"
            ))
        else:
            self.menu.add(rumps.MenuItem("🥇 黄金 ⚫ 状态不可用"))

        # ── 澳元交易状态 ──
        aud = all_status.get("aud", {})
        if aud:
            a_en = aud.get("enabled", False)
            a_at = aud.get("auto_trade", False)
            a_open = aud.get("open_count", 0)
            a_max = aud.get("max_open", 3)
            a_eval = aud.get("eval_count", 0)
            a_run = aud.get("running", False)
            if a_en:
                mode = "自动下单✅" if a_at else "仅信号🔸"
                self.menu.add(rumps.MenuItem(f"🦘 澳元 🟢 交易中 | {mode}"))
            else:
                self.menu.add(rumps.MenuItem("🦘 澳元 🔴 未开启"))
            self.menu.add(rumps.MenuItem(
                f"   在途 {a_open}/{a_max}单 | 评估 {a_eval}次 | {'运行🟢' if a_run else '停止🔴'}"
            ))
        else:
            self.menu.add(rumps.MenuItem("🦘 澳元 ⚫ 状态不可用"))

        self.menu.add(rumps.separator)

        # ── 线程健康度 ──
        thread_labels = {
            "signal": "🔄 统一调度心跳",
            "review": "📋 自动复盘",
            "news": "📰 新闻采集",
            "push": "📱 飞书推送",
            "opportunity": "🔍 错失扫描",
            "weekly": "📊 周分析",
        }

        all_healthy = True
        for key, label in thread_labels.items():
            t = threads.get(key, {})
            if t:
                healthy = t.get("healthy", False)
                ago_min = round(t.get("last_beat_seconds_ago", 0) / 60)
                if healthy:
                    icon = "🟢"
                else:
                    icon = "🔴"
                    all_healthy = False
                self.menu.add(rumps.MenuItem(f"  {icon} {label} ({ago_min}min前)"))
            else:
                # 线程未注册心跳
                self.menu.add(rumps.MenuItem(f"  ⚫ {label} (无数据)"))
                all_healthy = False

        # 更新标题图标
        self.title = "🟢📊" if all_healthy else "🔴📊"

        self.menu.add(rumps.separator)

        # ── 快捷操作 ──
        self.menu.add(rumps.MenuItem("🌐 打开中控台", callback=self._open_dashboard))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("🔄 重启 Daemon", callback=self._restart_daemon))
        self.menu.add(rumps.MenuItem("⏹ 退出 Daemon", callback=self._stop_daemon))

    def _open_dashboard(self, _):
        """打开中控台网页"""
        webbrowser.open(API_BASE)

    def _restart_daemon(self, _):
        """重启 daemon — 用 run_daemon.sh --stop 精确优雅停止, 再启动"""
        try:
            subprocess.run(
                ["bash", "scripts/run_daemon.sh", "--stop"],
                cwd=str(PROJ), capture_output=True, text=True, timeout=15,
            )
            time.sleep(1)
            subprocess.Popen(
                [sys.executable, "-m", "src.daemon", "--no-open"],
                cwd=str(PROJ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            rumps.notification("外汇量化交易", "Daemon 已重启", "")
        except Exception as e:
            rumps.notification("外汇量化交易", f"重启失败: {e}", "")

    def _stop_daemon(self, _):
        """停止 daemon — 复用 run_daemon.sh 停止逻辑"""
        try:
            subprocess.run(
                ["bash", "scripts/run_daemon.sh", "--stop"],
                cwd=str(PROJ), capture_output=True, text=True, timeout=15,
            )
            rumps.notification("外汇量化交易", "Daemon 已停止", "")
        except Exception as e:
            rumps.notification("外汇量化交易", f"停止失败: {e}", "")


def main():
    """启动菜单栏应用 — 带重试(重启daemon时AppKit/NSStatusItem初始化可能瞬时冲突)"""
    logging.basicConfig(level=logging.INFO)
    for attempt in range(3):
        try:
            logger.info(f"📊 菜单栏启动 (第{attempt + 1}次)")
            app = ForexTradingApp()
            app.run()
            logger.warning("⚠️ app.run() 已返回 — 菜单栏事件循环退出(图标消失/挂起的根因线索)")
            break
        except Exception as e:
            import traceback
            logger.error(f"菜单栏启动失败(第{attempt + 1}次): {e}\n{traceback.format_exc()}")
            time.sleep(3)
    else:
        logger.error("菜单栏启动失败3次, 放弃 (面板不受影响)")


if __name__ == "__main__":
    main()
