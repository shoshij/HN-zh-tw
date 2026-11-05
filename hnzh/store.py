"""data.json 的讀寫。

所有翻譯結果都存在這個檔案裡，GitHub Actions 每次執行完會把它 commit 回 repo，
所以下一次執行能接續上一次，不會重複翻譯已處理過的文章。
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "data.json"  # 本機測試可用 --data 換成別的檔案
KEEP_DAYS = 7  # 超過 7 天沒再出現在首頁的文章就移除，避免檔案無限長大


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    return dt.isoformat()


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def load() -> dict:
    if not DATA_FILE.exists():
        return {"updated_at": None, "translate_fail_streak": 0, "stories": {}}
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    data.setdefault("translate_fail_streak", 0)
    # 舊資料補上後來才加的欄位。每個欄位只會補一次。
    for s in data["stories"].values():
        if "summary_en" not in s:  # 沒有英文摘要：補上欄位並重新排進翻譯
            s["summary_en"] = None
            s["translate_tries"] = 0
        if "content_missing" not in s:  # 當初抓不到原文、只靠標題寫摘要的文章，之後會再試一次
            s["content_missing"] = "無法取得原文" in (s.get("summary_zh") or "")
            s["content_retries"] = 0
        s.setdefault("retranslate", False)
        s.setdefault("removed", False)
    return data


def save(data: dict) -> None:
    """寫檔。先寫到暫存檔再整個換過去，所以中途被打斷也不會留下壞掉的 data.json。"""
    data["updated_at"] = to_iso(now())
    # indent=1 讓 git diff 一行一欄位，容易看出每次改了什麼。
    text = json.dumps(data, ensure_ascii=False, indent=1)
    tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8", errors="replace"))  # 模型偶爾吐出不成對的字元，換成 ? 而不是讓存檔失敗
    os.replace(tmp, DATA_FILE)


def prune(data: dict, days: int = KEEP_DAYS) -> int:
    """移除太舊的文章，回傳移除的篇數。"""
    cutoff = now() - timedelta(days=days)
    old = [k for k, s in data["stories"].items() if from_iso(s["last_seen"]) < cutoff]
    for k in old:
        del data["stories"][k]
    return len(old)
