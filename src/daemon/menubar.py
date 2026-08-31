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


def fetch_status() -> dict:
    """从 daemon API 获取运行状态"""
    try:
        import urllib.request
        req = urllib.request.Request(f"{API_BASE}/api/status", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("data", {})
    except Exception:
        return {}


def fetch_daemon_status() -> dict:
    """获取守护进程详细状态"""
    try:
        import urllib.request
        req = urllib.request.Request(f"{API_BASE}/api/daemon/status", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("data", {})
    except Exception:
        return {}


def fetch_gold_status() -> dict:
    """获取黄金交易模块状态"""
    try:
        import urllib.request
        req = urllib.request.Request(f"{API_BASE}/api/gold/status", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("data", {})
    except Exception:
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
        self._start_polling()

    def _start_polling(self):
        """每 10 秒轮询一次状态"""
        self._refresh_timer = rumps.Timer(self._refresh_menu, 10)
        self._refresh_timer.start()

    def _refresh_menu(self, _=None):
        """刷新菜单内容"""
        self.menu.clear()

        # ── 状态区 ──
        daemon_status = fetch_daemon_status()
        running = daemon_status.get("running", False)
        eval_count = daemon_status.get("eval_count", 0)
        threads = daemon_status.get("threads", {})

        if running:
            self.menu.add(rumps.MenuItem(f"🟢 Daemon 运行中 | 信号 #{eval_count}"))
        else:
            self.menu.add(rumps.MenuItem("🔴 Daemon 未运行"))

        self.menu.add(rumps.separator)

        # ── 线程健康度 ──
        thread_labels = {
            "signal": "📊 信号评估",
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

        self.menu.add(rumps.separator)

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
        gold = fetch_gold_status()
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
                f"   在途 {g_open}/{g_max}单 | 评估 {g_eval}次 | {'循环🟢' if g_run else '停止🔴'}"
            ))
        else:
            self.menu.add(rumps.MenuItem("🥇 黄金 ⚫ 状态不可用"))

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
    """启动菜单栏应用"""
    logging.basicConfig(level=logging.WARNING)
    app = ForexTradingApp()
    app.run()


if __name__ == "__main__":
    main()
