#!/usr/bin/env python3
"""第一步：抓 HN 首頁、翻譯新文章、整理評論摘要，結果寫進 data.json。

用法：
  python update.py                   # 正常執行（GitHub Actions 每小時跑這個）
  python update.py --limit 3         # 只看首頁前 3 篇，用來測試
  python update.py --skip-translate  # 不呼叫 Gemini，只更新分數與評論數

翻譯階段的任何問題（額度用完、模型太忙、回應壞掉）都只會印警告，
不會讓程式失敗，因為不管怎樣都要把這一小時的首頁狀態存進 data.json。
"""
from __future__ import annotations

import argparse
import os
import traceback
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from hnzh import extract, hn, store, translate

FRONT_PAGE = 30
COMMENTS_AFTER = timedelta(hours=6)    # 上首頁滿 6 小時後才抓評論（讓討論累積）
COMMENTS_WINDOW = timedelta(hours=48)  # 超過 48 小時的文章就不再處理（頁面也不會顯示了）
TOP_COMMENTS = 3
MAX_TRIES = 3                          # 模型偶爾會漏掉一篇，最多重試幾次就放棄
MAX_BATCHES = 6                        # 每小時最多送幾批翻譯（約 48 篇），避免一次把免費額度用光


def warn(msg: str) -> None:
    """印警告。在 GitHub Actions 裡會變成執行摘要上的黃色標記。"""
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") else "[警告] "
    print(f"{prefix}{msg}")


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.")


def new_story(item: dict, now: str) -> dict:
    """第一次看到這篇文章時建立的紀錄。"""
    url = item.get("url") or hn.hn_url(item["id"])
    return {
        "id": item["id"],
        "title": item.get("title", ""),
        "url": url,
        "domain": domain_of(url),
        "hn_url": hn.hn_url(item["id"]),
        "is_hn_post": "url" not in item,  # Ask HN / Show HN 之類沒有外部連結的文章
        "by": item.get("by", ""),
        "posted_at": store.to_iso(datetime.fromtimestamp(item["time"], UTC)),
        "first_seen": now,
        "title_zh": None,
        "summary_zh": None,
        "summary_en": None,
        "translated_at": None,
        "model": None,
        "translate_tries": 0,
        "comments": None,
        "comments_done": False,
        "comments_at": None,
        "comment_tries": 0,
    }


def sync_front_page(data: dict, limit: int) -> list[dict]:
    """抓首頁，更新 data["stories"]，回傳目前首頁文章的 HN 原始資料（依排名）。"""
    now = store.to_iso(store.now())
    ids = hn.top_story_ids(limit)
    raw = hn.items(ids)
    stories = data["stories"]

    for s in stories.values():  # 先全部標成「不在首頁」，下面再把還在的標回來
        s["on_front_page"] = False
        s["rank"] = None

    front = []
    for rank, sid in enumerate(ids, 1):
        item = raw.get(sid)
        if not item or item.get("type") != "story":  # 略過徵才廣告（job）等
            continue
        front.append(item)
        key = str(sid)
        s = stories.get(key)
        if s is None:
            s = stories[key] = new_story(item, now)
        else:
            url = item.get("url") or hn.hn_url(sid)
            if item.get("title", "") != s["title"] or url != s["url"]:
                # HN 版主常改標題或修正連結；改了就重翻（評論摘要保留）
                print(f"[同步] 標題或連結變更，重新翻譯：{item.get('title', '')[:60]!r}")
                s.update({
                    "title": item.get("title", ""), "url": url, "domain": domain_of(url),
                    "is_hn_post": "url" not in item,
                    "title_zh": None, "summary_zh": None, "summary_en": None, "translated_at": None,
                    "model": None, "translate_tries": 0,
                })
        s.update({
            "score": item.get("score", 0),
            "comments_count": item.get("descendants", 0),
            "last_seen": now,
            "on_front_page": True,
            "rank": rank,
        })
    return front


def translate_new(data: dict, front: list[dict]) -> int:
    """翻譯還沒有中文標題（或缺英文摘要）的文章，首頁的優先。回傳翻譯篇數。"""
    stories = data["stories"]
    cutoff = store.now() - COMMENTS_WINDOW
    todo = [
        s for s in stories.values()
        if (not s["title_zh"] or not s.get("summary_en"))
        and s.get("translate_tries", 0) < MAX_TRIES
        and store.from_iso(s["first_seen"]) >= cutoff
    ]
    todo.sort(key=lambda s: (not s["on_front_page"], s["rank"] or 0))
    if not todo:
        return 0

    raw = {it["id"]: it for it in front}
    missing = [s["id"] for s in todo if s["id"] not in raw]  # 已掉出首頁但上次沒翻完的
    raw.update(hn.items(missing))

    texts = extract.article_texts([s["url"] for s in todo if not s["is_hn_post"]])
    payload = []
    for s in todo:
        item = raw.get(s["id"], {})
        if s["is_hn_post"]:
            content = hn.html_to_text(item.get("text", ""))
        else:
            content = texts.get(s["url"], "")
        payload.append({"id": s["id"], "title": s["title"], "url": s["url"], "content": content})

    done = 0
    batches = list(translate.chunks(payload, translate.BATCH_SIZE))
    if len(batches) > MAX_BATCHES:
        print(f"[翻譯] 待翻 {len(payload)} 篇，這小時先做 {MAX_BATCHES * translate.BATCH_SIZE} 篇，其餘下小時繼續")
    for batch in batches[:MAX_BATCHES]:
        try:
            result, model = translate.translate_stories(batch)
        except translate.QuotaExhausted as e:
            warn(f"翻譯：所有模型都不可用，剩下的文章下小時再試。{e}")
            break
        except translate.BatchFailed as e:
            warn(f"翻譯：這一批回應壞掉，下小時再試。{e}")
            for p in batch:
                stories[str(p["id"])]["translate_tries"] = stories[str(p["id"])].get("translate_tries", 0) + 1
            continue
        now = store.to_iso(store.now())
        for p in batch:
            stories[str(p["id"])]["translate_tries"] = stories[str(p["id"])].get("translate_tries", 0) + 1
        for sid, zh in result.items():
            s = stories.get(str(sid))
            if not s:
                continue
            s.update(zh)
            s["translated_at"] = now
            s["model"] = model
            done += 1
    return done


def summarize_due_comments(data: dict) -> int:
    """對上首頁滿 6 小時、還沒做過評論摘要的文章，抓前 3 則評論並摘要。只做一次。"""
    now = store.now()
    due = []
    for s in data["stories"].values():
        if s["comments_done"]:
            continue
        age = now - store.from_iso(s["first_seen"])
        if age < COMMENTS_AFTER:
            continue
        if age > COMMENTS_WINDOW or s.get("comment_tries", 0) >= MAX_TRIES:
            s["comments_done"] = True  # 太舊了或試太多次，不再處理
            continue
        due.append(s)
    if not due:
        return 0

    raw = hn.items([s["id"] for s in due])
    entries = []
    for s in due:
        comments = hn.top_comments(raw[s["id"]], TOP_COMMENTS) if s["id"] in raw else []
        if not comments:  # 沒有評論也算做完，不要每小時重抓
            s.update({"comments": [], "comments_done": True, "comments_at": store.to_iso(now)})
            continue
        entries.append({"id": s["id"], "title": s["title"], "title_zh": s["title_zh"], "comments": comments})

    done = 0
    for batch in translate.chunks(entries, translate.BATCH_SIZE):
        try:
            result, model = translate.summarize_comments(batch)
        except translate.QuotaExhausted as e:
            warn(f"評論摘要：所有模型都不可用，下小時再試。{e}")
            break
        except translate.BatchFailed as e:
            warn(f"評論摘要：這一批回應壞掉，下小時再試。{e}")
            result = {}
        for e in batch:
            s = data["stories"][str(e["id"])]
            s["comment_tries"] = s.get("comment_tries", 0) + 1
            summaries = result.get(e["id"])
            if not summaries:
                continue
            s["comments"] = [
                {"by": c["by"], "url": c["url"], "summary_zh": zh}
                for c, zh in zip(e["comments"], summaries, strict=True)  # 長度已在 translate 裡核對過
            ]
            s["comments_done"] = True
            s["comments_at"] = store.to_iso(now)
            done += 1
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=FRONT_PAGE, help="只看首頁前 N 篇（測試用）")
    ap.add_argument("--skip-translate", action="store_true", help="不呼叫 Gemini，只更新分數與評論數")
    args = ap.parse_args()

    data = store.load()
    front = sync_front_page(data, args.limit)  # HN 本身掛了就讓程式失敗，因為沒東西可更新
    print(f"首頁 {len(front)} 篇")
    if not args.skip_translate:
        try:
            print(f"翻譯 {translate_new(data, front)} 篇")
            print(f"評論摘要 {summarize_due_comments(data)} 篇")
        except Exception:  # noqa: BLE001 - 翻譯階段出錯也要把同步結果存下來，否則 first_seen 等資訊會遺失
            warn("翻譯階段發生未預期的錯誤，先儲存已完成的部分：\n" + traceback.format_exc())
    removed = store.prune(data)
    store.save(data)
    print(f"移除 {removed} 篇舊文章，data.json 共 {len(data['stories'])} 篇")


if __name__ == "__main__":
    main()
