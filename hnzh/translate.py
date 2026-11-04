"""用 Gemini API 做翻譯與摘要。

為什麼要「批次」？
免費額度主要是以「每天的請求數」計算，不是以字數計算，
所以一次把好幾篇文章塞進同一個請求，就能用很少的請求數處理完所有文章。

輸出用 JSON schema 強制格式（responseSchema），所以不用自己解析模型的自由文字。
API 文件：https://ai.google.dev/api/generate-content
"""
from __future__ import annotations

import difflib
import json
import os
import re
import time

import requests

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# 依序嘗試的模型。免費額度是「每個模型各自計算」，而且熱門模型常回 503（太多人用），
# 所以一個不行就換下一個。可用環境變數 GEMINI_MODELS 覆寫（逗號分隔）。
DEFAULT_MODELS = (
    "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-flash-lite-latest,"
    "gemini-3-flash-preview,gemini-3.7-flash,gemini-3.1-flash-lite"
)
# GitHub Actions 沒設定 GEMINI_MODELS 變數時會傳空字串進來，所以用 `or` 而不是 .get() 的預設值
MODELS = [m.strip() for m in (os.environ.get("GEMINI_MODELS") or DEFAULT_MODELS).split(",") if m.strip()]
BATCH_SIZE = 8    # 每個請求最多塞幾篇文章（太多篇模型容易張冠李戴）
TIMEOUT = 180     # 秒；一次處理 10 篇會比較久


class QuotaExhausted(Exception):
    """所有模型都不可用（額度用完或太忙）。呼叫端應停止翻譯，留到下一小時再試。"""


class BatchFailed(Exception):
    """這一批的回應壞掉（JSON 被截斷、沒有內容）。呼叫端可以跳過這批，繼續處理下一批。"""


SYSTEM = """你是 Hacker News 的繁體中文編輯，讀者是台灣的軟體與硬體工程師。

用語規則：
1. 一律使用台灣慣用的繁體中文與資訊科技用語：軟體、程式、程式碼、伺服器、資料庫、網路、硬體、演算法、記憶體、使用者、介面、函式、物件、執行緒、快取、編譯器、開放原始碼、人工智慧、機器學習、晶片、韌體、影片、資訊。不要使用中國大陸用語（例如軟件、程序、服务器、数据库、网络、硬件、算法、内存、用户、视频、信息、質量、優化）。
2. 專有名詞與技術術語第一次出現時用「中文（英文）」格式，例如「機械手臂（robotic arm）」、「大型語言模型（LLM）」、「強化學習（reinforcement learning）」。公司名、產品名、程式語言、人名若沒有通用的中文譯名就直接保留英文（例如 Rust、PostgreSQL、Nvidia、GitHub）。
3. 摘要要客觀、具體，說清楚文章在講什麼、重點發現或結論是什麼。不要加入個人評論，不要用「本文」「這篇文章」開頭。
4. 標題翻譯要簡潔自然，像台灣科技媒體的標題；不要附上英文原標題（頁面會另外顯示）。
"""

STORY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "INTEGER"},
                    "title": {"type": "STRING"},
                    "title_zh": {"type": "STRING"},
                    "summary_zh": {"type": "STRING"},
                },
                "required": ["id", "title", "title_zh", "summary_zh"],
            },
        }
    },
    "required": ["items"],
}

COMMENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "INTEGER"},
                    "title": {"type": "STRING"},
                    "comments": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["id", "title", "comments"],
            },
        }
    },
    "required": ["items"],
}


def _norm(title: str) -> str:
    return " ".join(title.lower().split())


def match_items(batch: list[dict], items: list[dict]) -> dict[int, dict]:
    """把模型回傳的項目對回輸入的文章，回傳 {輸入文章 id: 回傳項目}。

    模型偶爾會把 id 抄錯或把兩篇弄混，所以不信任它回傳的 id，
    改用它照抄的英文標題來比對（先完全相同，再用相似度 >= 0.9 補救）。對不上的就丟掉，下小時重試。
    """
    by_title = {_norm(b["title"]): b for b in batch}
    matched: dict[int, dict] = {}
    leftovers = []
    for it in items:
        b = by_title.get(_norm(it.get("title", "")))
        if b and b["id"] not in matched:
            matched[b["id"]] = it
        else:
            leftovers.append(it)
    for it in leftovers:
        candidates = [b for b in batch if b["id"] not in matched]
        best = difflib.get_close_matches(_norm(it.get("title", "")), [_norm(b["title"]) for b in candidates], n=1, cutoff=0.9)
        if best:
            b = next(b for b in candidates if _norm(b["title"]) == best[0])
            matched[b["id"]] = it
        else:
            print(f"[核對] 模型回傳的標題對不上任何輸入，略過：{it.get('title', '')[:80]!r}")
    return matched


_NOTE = re.compile(r"\s*[（(]\s*無法取得原文內容[^）)]*[）)]\s*")


def clean_title(title_zh: str) -> str:
    """模型偶爾會把「（無法取得原文內容…）」寫進標題，拿掉它。"""
    return _NOTE.sub("", title_zh).strip()


def chunks(seq: list, n: int):
    """把 list 切成每 n 個一組。"""
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _generate(prompt: str, schema: dict) -> tuple[dict, str]:
    """送一個請求給 Gemini，回傳 (解析好的 JSON, 實際使用的模型)。

    429（額度用完）、503（模型太忙）、5xx：先小等一下重試一次，再不行就換下一個模型。
    404（模型下架）：直接換下一個。全部都不行才丟出 QuotaExhausted。
    """
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("請先設定環境變數 GEMINI_API_KEY")
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 16384,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    headers = {"x-goog-api-key": key}  # 金鑰放 header，不要放網址，避免出現在 log
    last = ""
    for model in MODELS:
        for attempt in range(2):
            r = requests.post(API.format(model=model), headers=headers, json=body, timeout=TIMEOUT)
            if r.status_code == 200:
                try:
                    cand = r.json()["candidates"][0]
                    parts = [p for p in cand.get("content", {}).get("parts", []) if "text" in p and not p.get("thought")]
                    if not parts:
                        raise BatchFailed(f"{model} 沒有回傳內容：finishReason={cand.get('finishReason')}")
                    return json.loads(parts[0]["text"]), model
                except (KeyError, IndexError, ValueError) as e:  # ValueError 包含 JSONDecodeError
                    raise BatchFailed(f"{model} 的回應無法解析：{e}") from e
            last = f"{model} -> {r.status_code} {r.text[:200]}"
            if r.status_code == 404:
                break
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == 0:
                    time.sleep(15 if r.status_code == 429 else 10)  # 429：每分鐘額度；503：太多人用
                    continue
                break
            r.raise_for_status()  # 其他錯誤（例如 400 請求格式錯）直接讓程式停下來
        print(f"[Gemini] {last}，換下一個模型")
    raise QuotaExhausted(f"所有模型都不可用：{last}")


def translate_stories(batch: list[dict]) -> tuple[dict[int, dict], str]:
    """翻譯一批文章（最多 BATCH_SIZE 篇）。

    每筆需要 id, title, url, content。
    回傳 ({id: {"title_zh": ..., "summary_zh": ...}}, 使用的模型)。
    """
    blocks = []
    for i, s in enumerate(batch, 1):
        content = s.get("content") or "（無法取得原文內容）"
        blocks.append(
            f"### 文章 {i}\nid: {s['id']}\ntitle: {s['title']}\nurl: {s.get('url') or ''}\ncontent:\n{content}"
        )
    prompt = (
        f"以下是 {len(batch)} 篇 Hacker News 文章，每篇有 id、英文標題、網址與正文節錄。\n"
        "請對每一篇輸出 title（把輸入的英文標題一字不差照抄，用來核對）、"
        "title_zh（標題的繁體中文翻譯）與 summary_zh（2～3 句中文摘要）。\n"
        "若 content 是「（無法取得原文內容）」，就依標題推測主題寫一句摘要，"
        "並在 summary_zh 句尾加上「（無法取得原文內容，僅依標題判斷）」；title_zh 不要加任何註記。\n"
        "id 必須原樣照抄。\n\n" + "\n\n".join(blocks)
    )
    data, model = _generate(prompt, STORY_SCHEMA)
    return {
        sid: {"title_zh": clean_title(it["title_zh"]), "summary_zh": it["summary_zh"].strip()}
        for sid, it in match_items(batch, data["items"]).items()
    }, model


def summarize_comments(batch: list[dict]) -> tuple[dict[int, list[str]], str]:
    """把一批文章的評論各濃縮成 1～2 句。

    每筆需要 id, title, title_zh, comments（list of {"by", "text"}）。
    回傳 ({id: [摘要, ...]}, 使用的模型)。
    """
    blocks = []
    for i, e in enumerate(batch, 1):
        lines = [f"### 文章 {i}", f"id: {e['id']}", f"title: {e['title']}", f"title_zh: {e.get('title_zh') or ''}"]
        for j, c in enumerate(e["comments"], 1):
            lines.append(f"comment {j} (by {c['by']}):\n{c['text'][:1500]}")
        blocks.append("\n".join(lines))
    prompt = (
        f"以下是 {len(batch)} 篇 Hacker News 文章各自的熱門評論。\n"
        "請把每則評論濃縮成 1～2 句繁體中文摘要，保留評論者的主要論點、反駁或補充資訊，直接寫內容即可。\n"
        "每篇回傳 title（把輸入的英文標題一字不差照抄，用來核對）與 comments；"
        "comments 陣列的長度與順序必須和輸入的評論一致。id 必須原樣照抄。\n\n"
        + "\n\n".join(blocks)
    )
    data, model = _generate(prompt, COMMENT_SCHEMA)
    by_id = {e["id"]: e for e in batch}
    result = {}
    for sid, it in match_items(batch, data["items"]).items():
        if len(it["comments"]) != len(by_id[sid]["comments"]):
            print(f"[核對] 評論數量對不上（輸入 {len(by_id[sid]['comments'])}、回傳 {len(it['comments'])}），略過：{by_id[sid]['title'][:60]!r}")
            continue
        result[sid] = [c.strip() for c in it["comments"]]
    return result, model
