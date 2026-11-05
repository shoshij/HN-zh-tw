"""Hacker News 官方 API 的最小封裝。

HN 的 API 是放在 Firebase 上的公開 JSON，不需要金鑰。
每筆資料都要各自抓一次，所以用執行緒池平行抓，並設定逾時避免卡住。
文件：https://github.com/HackerNews/API

這裡把「這筆資料不存在」和「網路出錯沒抓到」分開回報：
前者是事實（文章被刪了），後者只是暫時的，下次執行應該再試。
"""
from __future__ import annotations

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://hacker-news.firebaseio.com/v0"
TIMEOUT = 15
ATTEMPTS = 3  # 暫時性的網路錯誤最多試幾次

session = requests.Session()
session.headers["User-Agent"] = "HN-zh-tw/1.0"


class FetchError(Exception):
    """因為網路問題沒抓到（不是資料不存在）。呼叫端應該下次再試。"""


def _get_json(url: str):
    """抓一個 JSON。暫時性的錯誤會重試，全部失敗就丟 FetchError。"""
    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            if attempt < ATTEMPTS - 1:
                time.sleep(1 + attempt)
    raise FetchError(f"HN API {url.removeprefix(BASE)} 抓取失敗：{type(last).__name__}")


def top_story_ids(limit: int = 30) -> list[int]:
    """回傳目前首頁前 limit 篇的 id（HN 已依排名排好序）。"""
    ids = _get_json(f"{BASE}/topstories.json")
    if not isinstance(ids, list) or not ids:
        raise FetchError("HN API 的 topstories 沒有回傳清單")
    return ids[:limit]


def item(item_id: int) -> dict | None:
    """抓單一 item（story 或 comment）。被刪除或被標記 dead 的回傳 None；網路出錯丟 FetchError。"""
    data = _get_json(f"{BASE}/item/{item_id}.json")
    if not isinstance(data, dict) or data.get("deleted") or data.get("dead"):
        return None
    return data


def items(ids: list[int], workers: int = 10) -> tuple[dict[int, dict], set[int]]:
    """平行抓多筆 item。

    回傳 (抓到的 {id: item}, 因網路錯誤沒抓到的 id)。
    被刪除的 item 兩邊都不會出現，所以呼叫端分得出「不存在」和「暫時抓不到」。
    """
    def fetch(item_id: int) -> tuple[int, dict | None, bool]:
        try:
            return item_id, item(item_id), False
        except FetchError as e:
            print(f"[HN] {e}")
            return item_id, None, True

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(fetch, ids))
    found = {i: d for i, d, _ in results if d}
    failed = {i for i, _, bad in results if bad}
    return found, failed


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
    如果輪到的那則是「網路出錯沒抓到」，就丟 FetchError 讓呼叫端下次再試，
    而不是拿排名比較後面的評論湊數，或把文章當成沒有評論。
    """
    kids = (story.get("kids") or [])[: n * 2]
    fetched, failed = items(kids)
    out = []
    for cid in kids:
        if cid in failed:
            raise FetchError(f"文章 {story.get('id')} 的評論 {cid} 暫時抓不到")
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
