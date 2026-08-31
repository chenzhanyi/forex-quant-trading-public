"""重大数据公布黑洞窗口 — 非农等数据时段自动禁开仓

规则（北京时间, 与欧美盘 15-23 窗口重叠）：
  - 非农就业报告（NFP + 失业率 + 平均时薪）= 每月第一个周五 美东 08:30
      夏令时（美东 DST, 3月第2周日~11月第1周日）→ 北京时间 20:30
      冬令时                                              → 北京时间 21:30
  - 黑洞窗口 = 公布时刻 前 lead_hours 小时 ~ 后 trail_min 分钟（提前避开数据剧烈波动）

参数从 config.yaml calendar 段读取:
  friday_no_trade: false      # 周五放开, 由本模块拦数据窗口
  data_blackout: true         # 总开关
  blackout_lead_hours: 2.0    # 数据前 N 小时起禁
  blackout_trail_min: 30      # 数据后 N 分钟禁

用法:
  from src.utils.trade_blackout import is_blacked_out
  blacked, reason = is_blacked_out()
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.utils.config_loader import config

SH_TZ = timezone(timedelta(hours=8))


# ── 美东夏令时判断 ──

def _nth_sunday(year: int, month: int, nth: int) -> datetime:
    d = datetime(year, month, 1, tzinfo=SH_TZ)
    days = (6 - d.weekday()) % 7          # Sunday=6
    return d + timedelta(days=days + 7 * (nth - 1))


def _is_us_dst(dt: datetime) -> bool:
    """指定年份时刻是否处于美东夏令时（3月第2周日 ~ 11月第1周日）"""
    start = _nth_sunday(dt.year, 3, 2).replace(tzinfo=None)
    end = _nth_sunday(dt.year, 11, 1).replace(tzinfo=None)
    return start <= dt.replace(tzinfo=None) < end


def _us_release_to_sh(et_dt: datetime) -> datetime:
    """美东公布时刻 → 北京时间"""
    offset = 12 if _is_us_dst(et_dt) else 13
    return et_dt + timedelta(hours=offset)


def _first_weekday(year: int, month: int, wday: int) -> datetime:
    d = datetime(year, month, 1, tzinfo=SH_TZ)
    return d + timedelta(days=(wday - d.weekday()) % 7)


# ── 黑洞窗口集合 ──

RELEASES = [
    {"kind": "nfp", "label": "非农(NFP)+失业率+时薪",
     "weekday": 4, "ordinal": 1, "et_hour": 8, "et_min": 30},
    # 可扩展其它固定数据（month_week 布局按需加）
]


def _release_windows(year: int, month: int, lead_h: float, trail_min: int):
    for r in RELEASES:
        d = _first_weekday(year, month, r["weekday"])   # 第一个周五
        if d.month != month:
            continue
        et = d.replace(hour=r["et_hour"], minute=r["et_min"])
        sh = _us_release_to_sh(et)
        yield {
            "label": r["label"],
            "start": sh - timedelta(hours=lead_h),
            "end": sh + timedelta(minutes=trail_min),
        }


# ── 公开 API ──

def hits(now: Optional[datetime] = None) -> list:
    """返回当前命中的黑洞窗口列表（空列表 = 可交易）"""
    now = now or datetime.now(SH_TZ)
    cfg = config.load().get("calendar", {})
    if not cfg.get("data_blackout", True):
        return []
    lead = float(cfg.get("blackout_lead_hours", 2.0))
    trail = int(cfg.get("blackout_trail_min", 30))
    return [w for w in _release_windows(now.year, now.month, lead, trail)
            if w["start"] <= now <= w["end"]]


def is_blacked_out(now: Optional[datetime] = None) -> tuple:
    """是否处于重大数据黑洞窗口. 返回 (bool, reason)"""
    h = hits(now)
    if h:
        w = h[0]
        rng = f"{w['start'].strftime('%m-%d %H:%M')} → {w['end'].strftime('%H:%M')}"
        return True, f"⛔ 数据黑洞: {w['label']} {rng} 禁开仓（避免数据剧烈波动）"
    return False, ""