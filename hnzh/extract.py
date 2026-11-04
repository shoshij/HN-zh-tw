"""抓原文網頁並抽出正文，給模型做摘要用。

用 trafilatura：它會去掉導覽列、廣告、側欄，只留文章本體。
抓不到（PDF、需要登入、純 JavaScript 頁面、逾時…）就回傳空字串，
摘要會改以標題為準，並註明「僅依標題判斷」。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
import trafilatura

TIMEOUT = 15
MAX_BYTES = 3_000_000  # 超過 3 MB 的頁面不處理
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HN-zh-tw/1.0)"}
# 這些網站抓回來的只有導覽列或「請登入」，不如直接依標題判斷
SKIP_DOMAINS = {"youtube.com", "youtu.be", "twitter.com", "x.com", "reddit.com", "old.reddit.com"}


def article_text(url: str, max_chars: int = 4000) -> str:
    """回傳正文前 max_chars 個字元；失敗回傳空字串。"""
    if not url or url.lower().endswith(".pdf"):
        return ""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host in SKIP_DOMAINS:
        return ""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if not r.ok or len(r.content) > MAX_BYTES:  # 404/403 的錯誤頁也是 HTML，不能拿來摘要
            return ""
        ctype = r.headers.get("Content-Type", "")
        if ctype.startswith("text/plain"):
            text = r.text
        elif "html" in ctype:
            # 傳 bytes 進去，讓 trafilatura 自己判斷編碼，避免非英文頁面變亂碼。
            text = trafilatura.extract(r.content, url=url, include_comments=False, include_tables=False)
        else:
            return ""
    except Exception as e:  # noqa: BLE001 - 任何抓取錯誤都只是「沒有正文」，不該中斷流程
        print(f"[原文] {url} 抓取失敗：{type(e).__name__}")
        return ""
    return (text or "")[:max_chars]


def article_texts(urls: list[str], workers: int = 8) -> dict[str, str]:
    """平行抓多個網址，回傳 {url: 正文}。"""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(urls, pool.map(article_text, urls), strict=True))
