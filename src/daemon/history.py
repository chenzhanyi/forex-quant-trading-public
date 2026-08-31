"""信号历史存储 — 追加式 JSON 日志，无数据库依赖"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SignalHistory:
    """信号历史记录器

    每次生成信号时追加记录，用于后续复盘分析。
    存储格式: data/signal_history.jsonl（每行一个 JSON）
    """

    def __init__(self):
        self.data_dir = Path(__file__).resolve().parent.parent.parent / "data"
        self.filepath = self.data_dir / "signal_history.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # 保留策略：最多 90 天或 2000 条
    MAX_RECORDS = 2000
    MAX_DAYS = 90

    def record(self, signal_data: Dict) -> None:
        """记录一条信号（自动修剪过期记录）"""
        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **signal_data,
        }
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info(f"📝 信号已记录: {record.get('direction','?')} @ {record.get('timestamp','?')}")

        # 每 50 条触发一次修剪，避免每次都重写
        file_size = self.filepath.stat().st_size if self.filepath.exists() else 0
        if file_size > 500_000:  # >500KB 触发
            self._trim()

    def _trim(self) -> None:
        """修剪过期记录，保留最近 MAX_DAYS 天 + 最多 MAX_RECORDS 条"""
        all_records = self.load_all()
        if len(all_records) <= self.MAX_RECORDS:
            return

        cutoff = datetime.now(timezone.utc).timestamp() - self.MAX_DAYS * 86400
        kept = [r for r in all_records
                if datetime.fromisoformat(r.get("recorded_at", "2000-01-01T00:00:00")).timestamp() >= cutoff]
        # 日期内超量则只保留最近 MAX_RECORDS 条
        if len(kept) > self.MAX_RECORDS:
            kept = kept[-self.MAX_RECORDS:]

        if len(kept) < len(all_records):
            with open(self.filepath, "w", encoding="utf-8") as f:
                for r in kept:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            logger.info(f"🗑️ 历史修剪: {len(all_records)} → {len(kept)} 条")

    def load_all(self) -> List[Dict]:
        """加载全部历史记录"""
        if not self.filepath.exists():
            return []
        records = []
        with open(self.filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return records

    def load_recent(self, days: int = 7) -> List[Dict]:
        """加载最近 N 天的记录"""
        all_records = self.load_all()
        if not all_records:
            return []
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        return [r for r in all_records
                if r.get("recorded_at", "") and
                datetime.fromisoformat(r["recorded_at"]).timestamp() >= cutoff]

    def summary(self, days: int = 7) -> Dict:
        """生成近期信号统计摘要"""
        recent = self.load_recent(days)
        if not recent:
            return {"total": 0, "message": "暂无信号记录"}

        total = len(recent)
        trade_signals = [r for r in recent if r.get("can_trade", False)]
        observe = [r for r in recent if not r.get("can_trade", False)]

        return {
            "total": total,
            "trade_signals": len(trade_signals),
            "observe": len(observe),
            "latest": recent[-1] if recent else None,
        }

    def get_daily_counts(self, days: int = 30) -> Dict[str, int]:
        """按日期统计信号数量"""
        recent = self.load_recent(days)
        counts = {}
        for r in recent:
            day = r.get("recorded_at", "")[:10]
            if day:
                counts[day] = counts.get(day, 0) + 1
        return dict(sorted(counts.items()))
