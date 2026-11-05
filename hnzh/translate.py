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
TIMEOUT = 90      # 秒；超過就當這個模型現在不穩，換下一個


# 這次執行中已經失敗過的模型。後面的批次直接跳過它們，不要每一批都重新等一次逾時。
# 下一小時是全新的程式，會從頭再試。
_down: set[str] = set()


class QuotaExhausted(Exception):
    """所有模型都不可用（額度用完或太忙）。呼叫端應停止翻譯，留到下一小時再試。"""


class BatchFailed(Exception):
    """模型有回應，但這一批的內容不能用（被安全過濾擋下、JSON 被截斷、格式不對）。

    每個可用的模型都試過才會丟出。呼叫端可以改成一篇一篇送，找出是哪一篇有問題。
    """


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
                    "summary_en": {"type": "STRING"},
                },
                "required": ["id", "title", "title_zh", "summary_zh", "summary_en"],
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


_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(text: str) -> str:
    """去掉控制字元與不成對的字元（它們會讓 RSS 變成不合法的 XML），並去頭尾空白。"""
    text = _BAD_CHARS.sub("", text)
    return text.encode("utf-8", errors="ignore").decode("utf-8").strip()


def _norm(title) -> str:
    return " ".join(str(title or "").lower().split())


def match_items(batch: list[dict], items: list) -> dict[int, dict]:
    """把模型回傳的項目對回輸入的文章，回傳 {輸入文章 id: 回傳項目}。

    模型偶爾會把 id 抄錯或把兩篇弄混，所以主要靠它照抄的英文標題來比對：
    1. 標題完全相同；
    2. 標題相似度 >= 0.9；
    3. 標題被改寫得比較多（例如少了「[pdf]」）時，如果它回傳的 id 也剛好是一篇還沒配對的文章，
       而且標題大致相像（>= 0.6），兩個弱線索加起來才採信。
    都對不上的就丟掉，之後重試。
    """
    by_title = {_norm(b["title"]): b for b in batch}
    matched: dict[int, dict] = {}
    leftovers = []
    for it in items:
        if not isinstance(it, dict):
            continue
        b = by_title.get(_norm(it.get("title")))
        if b and b["id"] not in matched:
            matched[b["id"]] = it
        else:
            leftovers.append(it)
    for it in leftovers:
        candidates = [b for b in batch if b["id"] not in matched]
        echoed = _norm(it.get("title"))
        best = difflib.get_close_matches(echoed, [_norm(b["title"]) for b in candidates], n=1, cutoff=0.9)
        b = next((c for c in candidates if _norm(c["title"]) == best[0]), None) if best else None
        if b is None:
            same_id = next((c for c in candidates if c["id"] == it.get("id")), None)
            if same_id and difflib.SequenceMatcher(None, echoed, _norm(same_id["title"])).ratio() >= 0.6:
                b = same_id
        if b:
            matched[b["id"]] = it
        else:
            print(f"[核對] 模型回傳的標題對不上任何輸入，略過：{str(it.get('title'))[:80]!r}")
    return matched


_NOTE = re.compile(r"\s*[（(]\s*無法取得原文內容[^）)]*[）)]\s*")


def clean_title(title_zh: str) -> str:
    """模型偶爾會把「（無法取得原文內容…）」寫進標題，拿掉它。"""
    return _NOTE.sub("", title_zh).strip()


def chunks(seq: list, n: int):
    """把 list 切成每 n 個一組。"""
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _parse(payload, model: str) -> dict:
    """從 Gemini 的回應取出 JSON。任何不合預期的形狀都丟 BatchFailed。"""
    try:
        cand = payload["candidates"][0]
        parts = (cand.get("content") or {}).get("parts") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        feedback = payload.get("promptFeedback") if isinstance(payload, dict) else None
        raise BatchFailed(f"{model} 沒有回傳候選答案（{feedback}）") from None
    reason = cand.get("finishReason")
    # JSON 偶爾會被拆成好幾段，全部接起來；模型的思考過程（thought）不算
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
    if not text.strip():
        raise BatchFailed(f"{model} 沒有回傳內容（finishReason={reason}）")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise BatchFailed(f"{model} 的回應不是合法的 JSON（finishReason={reason}）：{e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise BatchFailed(f"{model} 的回應格式不對：沒有 items 陣列")
    return data


def _retry_delay(r) -> float | None:
    """可重試的錯誤要等幾秒再試；回傳 None 表示不值得等（直接換模型）。"""
    if r.status_code != 429:
        return 10  # 5xx：伺服器太忙
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', r.text)
    if not m:
        return 15
    delay = float(m.group(1)) + 1
    return delay if delay <= 30 else None  # 要等很久通常是每日額度用完，等也沒用


def _generate(prompt: str, schema: dict) -> tuple[dict, str]:
    """送一個請求給 Gemini，回傳 (解析好的 JSON, 實際使用的模型)。依序嘗試每個模型：

    - 429（額度）、5xx（太忙）：等一下重試一次，再不行就換下一個模型；
    - 逾時、連線錯誤、404（模型下架）等：直接換下一個；
      以上失敗過的模型記在 _down，這次執行的後續批次不再嘗試。
    - 模型有回應但內容不能用：換下一個模型試同一批（模型本身沒壞，不記入 _down）。
    全部試過後：有模型回過不能用的內容就丟 BatchFailed，否則丟 QuotaExhausted。
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
    last = unusable = ""
    for model in MODELS:
        if model in _down:
            continue
        bench = True
        for attempt in range(2):
            try:
                r = requests.post(API.format(model=model), headers=headers, json=body, timeout=TIMEOUT)
            except requests.RequestException as e:  # 逾時、連線失敗：這個模型現在不穩，直接換下一個
                last = f"{model} -> {type(e).__name__}"
                break
            if r.status_code == 200:
                try:
                    return _parse(r.json(), model), model
                except BatchFailed as e:
                    unusable, bench = str(e), False
                except ValueError as e:  # 連外層都不是 JSON
                    unusable, bench = f"{model} 的回應不是 JSON：{e}", False
                print(f"[Gemini] {unusable}；換下一個模型試同一批")
                break
            last = f"{model} -> {r.status_code} {' '.join(r.text[:160].split())}"
            delay = _retry_delay(r) if r.status_code in (429, 500, 502, 503, 504) and attempt == 0 else None
            if delay is None:
                break  # 重試過還是不行，或是 404（模型下架）、400（這個模型不支援）等：換下一個
            time.sleep(delay)
        if bench:
            _down.add(model)
            print(f"[Gemini] {last}；這次執行先不用它，換下一個模型")
    if unusable:
        raise BatchFailed(unusable)
    raise QuotaExhausted(f"所有模型都不可用。最後的錯誤：{last or '每個模型都在這次執行中失敗過'}")


def _is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def translate_stories(batch: list[dict]) -> tuple[dict[int, dict], str]:
    """翻譯一批文章（最多 BATCH_SIZE 篇）。

    每筆需要 id, title, url, content。
    回傳 ({id: {"title_zh": ..., "summary_zh": ..., "summary_en": ...}}, 使用的模型)。
    模型漏掉或欄位不完整的文章不會出現在結果裡。
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
        "title_zh（標題的繁體中文翻譯）、summary_zh（2～3 句中文摘要）、"
        "summary_en（把 summary_zh 翻成自然的英文，句數與順序和中文完全一致、一句對一句，"
        "給想中英對照練習英文的讀者）。\n"
        "若 content 是「（無法取得原文內容）」，就依標題推測主題寫一句摘要，"
        "並在 summary_zh 句尾加上「（無法取得原文內容，僅依標題判斷）」；title_zh 不要加任何註記。\n"
        "id 必須原樣照抄。\n\n" + "\n\n".join(blocks)
    )
    data, model = _generate(prompt, STORY_SCHEMA)
    result = {}
    for sid, it in match_items(batch, data["items"]).items():
        if not all(_is_text(it.get(k)) for k in ("title_zh", "summary_zh", "summary_en")):
            print(f"[核對] 模型回傳的欄位不完整，略過文章 {sid}")
            continue
        result[sid] = {
            "title_zh": clean_title(clean_text(it["title_zh"])),
            "summary_zh": clean_text(it["summary_zh"]),
            "summary_en": clean_text(it["summary_en"]),
        }
    return result, model


def summarize_comments(batch: list[dict]) -> tuple[dict[int, list[str]], str]:
    """把一批文章的評論各濃縮成 1～2 句。

    每筆需要 id, title, title_zh, comments（list of {"by", "text"}）。
    回傳 ({id: [摘要, ...]}, 使用的模型)。數量對不上或有空白摘要的文章不會出現在結果裡。
    """
    blocks = []
    for i, e in enumerate(batch, 1):
        lines = [f"### 文章 {i}", f"id: {e['id']}", f"title: {e['title']}", f"title_zh: {e.get('title_zh') or ''}"]
        for j, c in enumerate(e["comments"], 1):
            lines.append(f"comment {j} (by {c['by']}):\n{c['text'][:1500]}")
        blocks.append("\n".join(lines))
    prompt = (
        f"以下是 {len(batch)} 篇 Hacker News 文章各自的熱門評論。\n"
        "請把每則評論濃縮成 1～2 句繁體中文摘要，保留主要論點、反駁或補充資訊。\n"
        "頁面已經會顯示留言者的帳號，所以直接陳述論點，不要用「評論者」「留言者」「作者」「這則評論」開頭"
        "（例如寫「這些裝置可能被用來…」，不要寫「評論者指出這些裝置可能被用來…」）。\n"
        "每篇回傳 title（把輸入的英文標題一字不差照抄，用來核對）與 comments；"
        "comments 陣列的長度與順序必須和輸入的評論一致。id 必須原樣照抄。\n\n"
        + "\n\n".join(blocks)
    )
    data, model = _generate(prompt, COMMENT_SCHEMA)
    by_id = {e["id"]: e for e in batch}
    result = {}
    for sid, it in match_items(batch, data["items"]).items():
        comments, want = it.get("comments"), len(by_id[sid]["comments"])
        if not isinstance(comments, list) or len(comments) != want or not all(_is_text(c) for c in comments):
            got = len(comments) if isinstance(comments, list) else "格式不對"
            print(f"[核對] 評論摘要對不上（輸入 {want} 則、回傳 {got}），略過：{by_id[sid]['title'][:60]!r}")
            continue
        result[sid] = [clean_text(c) for c in comments]
    return result, model
