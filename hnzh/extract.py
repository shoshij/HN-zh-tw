"""抓原文網頁並抽出正文，給模型做摘要用。

用 trafilatura：它會去掉導覽列、廣告、側欄，只留文章本體。
抓不到（PDF、需要登入、擋機器人、純 JavaScript 頁面、逾時…）就回傳空字串，
摘要會改以標題為準，並註明「僅依標題判斷」。每次失敗都會印出原因，方便日後查。

下載時先看標頭再讀內容、邊讀邊算大小，並限制總時間，
避免一個很大或很慢的連結拖垮整次執行。

解析交給獨立的子程序做。trafilatura 底層的 lxml 是 C 程式，多個執行緒同時解析時
會不定期整個當掉（segmentation fault），try/except 擋不住，會把整次執行一起拖下水。
改成每一頁各開一個子程序解析，當掉或卡住就只是那一篇抓不到原文，摘要改依標題。
"""
from __future__ import annotations

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
import trafilatura  # noqa: F401 - 啟動時就確認有安裝，不要等到子程序裡才發現

TIMEOUT = 15            # 秒；連線與每次讀取的逾時
DEADLINE = 25           # 秒；單一網址從頭到尾最多花多久（防止伺服器一點一點慢慢吐）
PARSE_TIMEOUT = 60      # 秒；解析一頁最多花多久（防止解析器卡死）
MAX_BYTES = 10_000_000  # 解壓縮後超過 10 MB 就放棄（新聞網站的頁面常有好幾 MB）
MIN_CHARS = 100         # 抽出來不到這麼多字，多半是「請開啟 JavaScript」或驗證頁，不拿來摘要
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HN-zh-tw/1.0)"}  # 誠實表明是程式，被網站擋下就算了
# 這些網站抓回來的只有導覽列或「請登入」，不如直接依標題判斷
SKIP_DOMAINS = {"youtube.com", "youtu.be", "twitter.com", "x.com", "reddit.com", "old.reddit.com"}

# 子程序跑的程式：從 stdin 讀網頁、把正文寫到 stdout。傳 bytes 進去，讓 trafilatura 自己判斷編碼。
_PARSER = """
import sys, trafilatura
text = trafilatura.extract(sys.stdin.buffer.read(), url=sys.argv[1], include_comments=False, include_tables=False)
sys.stdout.buffer.write((text or "").encode("utf-8"))
"""


def host_of(url: str) -> str:
    """網址的主機名稱（去掉 www.）。網址格式不對就回傳空字串。"""
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _download(url: str) -> tuple[bytes, str, str]:
    """回傳 (內容, Content-Type, 失敗原因)。成功時失敗原因是空字串。"""
    start = time.monotonic()
    with requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True) as r:
        if not r.ok:  # 404/403 的錯誤頁也是 HTML，不能拿來摘要
            return b"", "", f"HTTP {r.status_code}"
        ctype = r.headers.get("Content-Type", "").lower()
        if not (ctype.startswith("text/plain") or "html" in ctype):
            return b"", ctype, f"不是網頁（{ctype.split(';')[0] or '未知類型'}）"
        chunks, size = [], 0
        for chunk in r.iter_content(65536):  # 已解壓縮的內容，所以壓縮炸彈也會被下面的上限擋住
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                return b"", ctype, f"超過 {MAX_BYTES // 1_000_000} MB"
            if time.monotonic() - start > DEADLINE:
                return b"", ctype, f"下載超過 {DEADLINE} 秒"
        return b"".join(chunks), ctype, ""


def _decode(body: bytes) -> str:
    """純文字檔沒有標明編碼時，先當 UTF-8（現在絕大多數都是），不行再容錯解碼。"""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


def _parse(body: bytes, url: str) -> tuple[str, str]:
    """在子程序裡抽正文。回傳 (正文, 失敗原因)；成功時失敗原因是空字串。"""
    try:
        r = subprocess.run([sys.executable, "-I", "-c", _PARSER, url], input=body, capture_output=True, timeout=PARSE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return "", f"解析超過 {PARSE_TIMEOUT} 秒"
    if r.returncode < 0:          # 被訊號殺掉：11 是 segmentation fault，6 是 abort
        return "", f"解析器當掉（signal {-r.returncode}）"
    if r.returncode:              # Python 例外；stderr 最後一行是例外名稱與訊息
        lines = r.stderr.decode("utf-8", errors="replace").strip().splitlines()
        return "", f"解析失敗（{lines[-1][:80] if lines else f'exit {r.returncode}'}）"
    return r.stdout.decode("utf-8", errors="replace"), ""


def _fail(host: str, reason: str) -> str:
    print(f"[原文] {host}：{reason}，摘要改依標題")
    return ""


def article_text(url: str, max_chars: int = 4000) -> str:
    """回傳正文前 max_chars 個字元；抓不到就印出原因並回傳空字串。"""
    host = host_of(url)
    if not host:
        return ""
    if url.lower().split("?")[0].endswith(".pdf"):
        return _fail(host, "PDF")
    if host in SKIP_DOMAINS:
        return _fail(host, "這類網站抓不到內容")

    body, ctype, reason = b"", "", ""
    for attempt in range(2):
        try:
            body, ctype, reason = _download(url)
            retry = reason.startswith("HTTP 5")       # 伺服器暫時故障，值得再試一次
        except requests.Timeout:
            reason, retry = "逾時", False              # 已經等很久了，不再試
        except requests.RequestException as e:
            reason, retry = f"連線失敗（{type(e).__name__}）", True
        if not reason or not retry or attempt == 1:
            break
        time.sleep(2)
    if reason:
        return _fail(host, reason)

    if ctype.startswith("text/plain"):
        text = _decode(body)
    else:
        text, reason = _parse(body, url)
        if reason:
            return _fail(host, reason)
    text = text.strip()
    if len(text) < MIN_CHARS:
        return _fail(host, "抽不出正文（可能是純 JavaScript 或驗證頁）")
    return text[:max_chars]


def article_texts(urls: list[str], workers: int = 8) -> dict[str, str]:
    """平行抓多個網址，回傳 {url: 正文}。"""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(urls, pool.map(article_text, urls), strict=True))
