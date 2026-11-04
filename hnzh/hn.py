"""Hacker News 官方 API 的最小封裝。

HN 的 API 是放在 Firebase 上的公開 JSON，不需要金鑰。
每筆資料都要各自抓一次，所以用執行緒池平行抓，並設定逾時避免卡住。
文件：https://github.com/HackerNews/API
"""
from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://hacker-news.firebaseio.com/v0"
TIMEOUT = 15

session = requests.Session()
session.headers["User-Agent"] = "HN-zh-tw/1.0"


def top_story_ids(limit: int = 30) -> list[int]:
    """回傳目前首頁前 limit 篇的 id（HN 已依排名排好序）。"""
    r = session.get(f"{BASE}/topstories.json", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()[:limit]


def item(item_id: int) -> dict | None:
    """抓單一 item（story 或 comment）。被刪除或被標記 dead 的回傳 None。"""
    r = session.get(f"{BASE}/item/{item_id}.json", timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not data or data.get("deleted") or data.get("dead"):
        return None
    return data


def items(ids: list[int], workers: int = 10) -> dict[int, dict]:
    """平行抓多筆 item，回傳 {id: item}；抓不到的直接略過。"""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(item, ids))
    return {d["id"]: d for d in results if d}


def hn_url(item_id: int) -> str:
    return f"https://news.ycombinator.com/item?id={item_id}"


_TAG = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    """HN 的評論與 Ask HN 內文是 HTML 片段（<p>、<a>、<i>、<code>），轉成純文字。"""
    text = raw.replace("<p>", "\n").replace("</p>", "")
    text = _TAG.sub("", text)
    return html.unescape(text).strip()


def top_comments(story: dict, n: int = 3) -> list[dict]:
    """取文章的前 n 則頂層評論。HN 的 kids 已依排名排序，前面的就是最熱門的。

    多抓一倍（n*2）備用，略過被刪除或沒內容的，湊滿 n 則就停。
    """
    kids = (story.get("kids") or [])[: n * 2]
    fetched = items(kids)
    out = []
    for cid in kids:
        c = fetched.get(cid)
        if not c or c.get("type") != "comment" or not c.get("text"):
            continue
        out.append({
            "id": cid,
            "by": c.get("by", ""),
            "text": html_to_text(c["text"]),
            "url": hn_url(cid),
        })
        if len(out) == n:
            break
    return out
