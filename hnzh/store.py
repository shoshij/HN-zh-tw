"""data.json 的讀寫。

所有翻譯結果都存在這個檔案裡，GitHub Actions 每小時執行完會把它 commit 回 repo，
所以下一次執行能接續上一次，不會重複翻譯已處理過的文章。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "data.json"
KEEP_DAYS = 7  # 超過 7 天沒再出現在首頁的文章就移除，避免檔案無限長大


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    return dt.isoformat()


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def load() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"updated_at": None, "stories": {}}


def save(data: dict) -> None:
    data["updated_at"] = to_iso(now())
    # indent=1 讓 git diff 一行一欄位，容易看出每小時改了什麼。
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def prune(data: dict, days: int = KEEP_DAYS) -> int:
    """移除太舊的文章，回傳移除的篇數。"""
    cutoff = now() - timedelta(days=days)
    old = [k for k, s in data["stories"].items() if from_iso(s["last_seen"]) < cutoff]
    for k in old:
        del data["stories"][k]
    return len(old)
