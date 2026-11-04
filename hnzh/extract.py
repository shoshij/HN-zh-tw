"""抓原文網頁並抽出正文，給模型做摘要用。

用 trafilatura：它會去掉導覽列、廣告、側欄，只留文章本體。
抓不到（PDF、需要登入、純 JavaScript 頁面、逾時…）就回傳空字串，
摘要會改以標題為準。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import requests
import trafilatura

TIMEOUT = 15
MAX_BYTES = 3_000_000  # 超過 3 MB 的頁面不處理
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HN-zh-tw/1.0)"}


def article_text(url: str, max_chars: int = 4000) -> str:
    """回傳正文前 max_chars 個字元；失敗回傳空字串。"""
    if not url or url.lower().endswith(".pdf"):
        return ""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if "html" not in r.headers.get("Content-Type", "") or len(r.content) > MAX_BYTES:
            return ""
        # 傳 bytes 進去，讓 trafilatura 自己判斷編碼，避免中文或其他語言頁面變亂碼。
        text = trafilatura.extract(
            r.content, url=url, include_comments=False, include_tables=False
        )
    except Exception:
        return ""
    return (text or "")[:max_chars]


def article_texts(urls: list[str], workers: int = 8) -> dict[str, str]:
    """平行抓多個網址，回傳 {url: 正文}。"""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(urls, pool.map(article_text, urls)))
