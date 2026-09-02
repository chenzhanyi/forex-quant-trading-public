"""专用代理管理 — VLESS 链接 → sing-box 本地代理进程

中控台保存 VLESS 链接后, 本模块负责:
  1. 解析 vless:// 链接
  2. 生成 sing-box 配置(本地 mixed 入站 127.0.0.1:PORT)
  3. 启动/停止 sing-box 子进程
  4. 代理连通性测试

端口可在中控台设置(默认 7898), 只监听 127.0.0.1 且只被本系统的
OANDA 请求显式使用 — 与 Clash 及其他应用完全隔离, 互不影响。

daemon(engine) 只读 dashboard_settings["proxy"] 配置,
按 mode 决定 OANDA 请求是否走本代理。
"""
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx

logger = logging.getLogger(__name__)

# 默认本地入站端口(避开 Clash 常用 7890/7897/1080) — 中控台可改
DEFAULT_PORT = 7898

RUNTIME_DIR = Path(__file__).resolve().parent.parent.parent / ".runtime" / "singbox"
CONFIG_FILE = RUNTIME_DIR / "config.json"
PID_FILE = RUNTIME_DIR / "singbox.pid"
PORT_FILE = RUNTIME_DIR / "singbox.port"
LOG_FILE = RUNTIME_DIR / "singbox.log"

# 二进制解析顺序: 项目本地(换电脑 clone 即用) → 系统 PATH(brew 等)
PROJECT_BIN = Path(__file__).resolve().parent.parent.parent / "bin" / "sing-box"
SINGBOX_BIN = str(PROJECT_BIN) if PROJECT_BIN.exists() else "sing-box"


def parse_vless(url: str) -> dict:
    """解析 vless:// 链接 → sing-box outbound 参数字典

    支持 security=reality(常用) 和 security=tls 两种。
    """
    if not url or not url.startswith("vless://"):
        raise ValueError("链接必须以 vless:// 开头")
    p = urlparse(url)
    uuid = unquote(p.username or "")
    host, port = p.hostname, p.port
    if not uuid or not host:
        raise ValueError("链接格式无效: 缺少 UUID 或服务器地址")
    q = parse_qs(p.query)

    def one(k, d=""):
        v = q.get(k, [d])[0]
        return v if v else d

    security = one("security", "none")
    out = {
        "type": "vless",
        "tag": "vless-out",
        "server": host,
        "server_port": int(port or 443),
        "uuid": uuid,
    }
    if one("flow"):
        out["flow"] = one("flow")

    if security == "reality":
        if not one("pbk") or not one("sni"):
            raise ValueError("reality 链接缺少 pbk 或 sni 参数")
        out["tls"] = {
            "enabled": True,
            "server_name": one("sni"),
            "utls": {"enabled": True, "fingerprint": one("fp", "chrome")},
            "reality": {
                "enabled": True,
                "public_key": one("pbk"),
                "short_id": one("sid", ""),
            },
        }
    elif security == "tls":
        out["tls"] = {
            "enabled": True,
            "server_name": one("sni", host),
            "insecure": one("allowInsecure", "0") == "1",
        }
    return out


def build_config(parsed_outbound: dict, port: int = DEFAULT_PORT) -> dict:
    """生成 sing-box 完整配置: 本地 mixed 入站(仅127.0.0.1) + vless 出站

    注意: sniff 等 legacy 入站字段在 sing-box 1.13+ 已移除,
    不能加 — 否则配置解码直接失败。
    """
    return {
        "log": {"level": "info", "output": str(LOG_FILE), "timestamp": True},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": port,
            }
        ],
        "outbounds": [parsed_outbound],
    }


class ProxyManager:
    """sing-box 进程生命周期管理(单例)"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ── 进程管理 ──

    def is_running(self) -> bool:
        """进程存活且 PID 文件匹配"""
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # 信号0: 只检查存在性
            return True
        except Exception:
            return False

    def current_port(self) -> int:
        """当前运行的代理端口(进程已停止时返回默认端口)"""
        try:
            return int(PORT_FILE.read_text().strip())
        except Exception:
            return DEFAULT_PORT

    def start(self, vless_url: str, port: int = DEFAULT_PORT) -> dict:
        """写配置并启动 sing-box。已运行则先停再起(配置可能变了)。"""
        port = int(port)
        if not (1 <= port <= 65535):
            return {"success": False, "error": f"端口 {port} 无效"}
        try:
            parsed = parse_vless(vless_url)
            cfg = build_config(parsed, port)
        except Exception as e:
            return {"success": False, "error": f"链接解析失败: {e}"}

        self.stop()
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        PORT_FILE.write_text(str(port))

        try:
            with open(LOG_FILE, "ab") as logf:
                proc = subprocess.Popen(
                    [SINGBOX_BIN, "run", "-c", str(CONFIG_FILE)],
                    stdout=logf, stderr=logf,
                    start_new_session=True,  # 脱离 dashboard 进程组, 面板重启不杀代理
                )
        except FileNotFoundError:
            return {"success": False, "error": f"未找到 {SINGBOX_BIN}, 请先下载到项目 bin/ 或 brew install sing-box"}

        PID_FILE.write_text(str(proc.pid))
        # 等待入站就绪
        for _ in range(30):
            if self._port_open(port):
                logger.info(f"🌐 专用代理已启动 pid={proc.pid} port={port}")
                return {"success": True, "pid": proc.pid, "port": port}
            if proc.poll() is not None:
                tail = self._log_tail()
                return {"success": False, "error": f"sing-box 启动即退出: {tail}"}
            time.sleep(0.2)
        return {"success": False, "error": "sing-box 启动超时(30s内入站未就绪)"}

    def stop(self) -> dict:
        """停止代理进程"""
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if self._pid_gone(pid):
                    break
                time.sleep(0.2)
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)  # 兜底强杀
            except (OSError, ProcessLookupError):
                pass
        except Exception:
            pass
        if PID_FILE.exists():
            PID_FILE.unlink()
        logger.info("🛑 专用代理已停止")
        return {"success": True}

    def restart(self, vless_url: str, port: int = DEFAULT_PORT) -> dict:
        return self.start(vless_url, port)

    def status(self) -> dict:
        running = self.is_running()
        pid = None
        if running:
            try:
                pid = int(PID_FILE.read_text().strip())
            except Exception:
                pass
        return {"running": running, "pid": pid, "port": self.current_port()}

    # ── 连通性测试 ──

    def test_connectivity(self, timeout: float = 10.0) -> dict:
        """通过专用代理测试连通性: gstatic 204 + OANDA 可达"""
        if not self.is_running():
            return {"success": False, "error": "代理进程未运行", "internet": False, "oanda": False}
        proxy_url = f"http://127.0.0.1:{self.current_port()}"
        result = {"success": False, "internet": False, "oanda": False, "error": None, "latency_ms": None}
        t0 = time.time()
        try:
            r = httpx.get("https://www.gstatic.com/generate_204", proxy=proxy_url, timeout=timeout)
            result["internet"] = r.status_code in (200, 204)
        except Exception as e:
            result["error"] = f"通用连通失败: {str(e)[:80]}"
            return result
        result["latency_ms"] = round((time.time() - t0) * 1000)
        try:
            r = httpx.get("https://api-fxpractice.oanda.com", proxy=proxy_url, timeout=timeout)
            # 401/404 也算可达 — 说明 TCP+TLS 已通到 OANDA
            result["oanda"] = r.status_code < 500
        except Exception as e:
            result["error"] = f"OANDA 不可达: {str(e)[:80]}"
            return result
        result["success"] = result["internet"] and result["oanda"]
        if result["success"]:
            result.pop("error", None)
        return result

    # ── 内部工具 ──

    @staticmethod
    def _port_open(port: int) -> bool:
        import socket
        try:
            with socket.socket() as s:
                s.settimeout(1)
                return s.connect_ex(("127.0.0.1", port)) == 0
        except Exception:
            return False

    @staticmethod
    def _pid_gone(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return False
        except (OSError, ProcessLookupError):
            return True

    def _log_tail(self, n: int = 300) -> str:
        try:
            text = LOG_FILE.read_text(errors="replace")
            return " | ".join(text.strip().splitlines()[-4:])[-n:]
        except Exception:
            return "无日志"
