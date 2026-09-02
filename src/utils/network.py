"""网络通道公共工具 — 中控台专用代理的决策与候选序列

专用代理(sing-box)由中控台 proxy 设置控制, 各数据源共用同一份配置:
  - oanda.py / twelvedata.py / calendar.py / news_collector.py
  - mode=off     → 只走默认通道(直连/Clash)
  - mode=auto    → 默认通道优先, 失败后自动切专用代理重试
  - mode=always  → 专用代理优先, 失败回默认通道

专用代理只监听 127.0.0.1, 不影响 Clash 及其他应用。
"""
import socket
from typing import List, Optional

DEFAULT_PROXY_PORT = 7898


def get_proxy_settings() -> dict:
    """中控台专用代理设置(未配置返回空 dict)"""
    try:
        from src.utils.dashboard_settings import load
        return load().get("proxy") or {}
    except Exception:
        return {}


def get_dedicated_proxy_url() -> Optional[str]:
    """专用代理地址 — 模式 off / 未配置 / 进程未运行 时返回 None

    进程未运行时不返回地址 → 调用方自动退回默认通道, 不阻塞数据刷新。
    """
    try:
        p = get_proxy_settings()
        mode = p.get("mode", "off")
        if mode not in ("auto", "always"):
            return None
        port = int(p.get("port", DEFAULT_PROXY_PORT))
        # 端口探测: sing-box 没起来时退回默认通道, 避免请求全部失败
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return None
        return f"http://127.0.0.1:{port}"
    except Exception:
        return None


def get_proxy_candidates(default_proxy: Optional[str] = None) -> List[Optional[str]]:
    """按中控台模式组装代理尝试序列

    None 表示走默认通道(该模块原有行为: 直连或 Clash/系统代理),
    proxy url 表示显式走专用代理。
    """
    dedicated = get_dedicated_proxy_url()
    mode = get_proxy_settings().get("mode", "off")
    if mode == "always":
        seq = [dedicated, default_proxy] if dedicated else [default_proxy]
    elif mode == "auto":
        seq = [default_proxy, dedicated] if dedicated else [default_proxy]
    else:  # off / 未配置
        seq = [default_proxy]
    # 去重保序(None 与 url 都可能重复)
    out: List[Optional[str]] = []
    for c in seq:
        if c not in out:
            out.append(c)
    return out
