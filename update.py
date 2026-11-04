#!/usr/bin/env python3
"""第一步：抓 HN 首頁、翻譯新文章、整理評論摘要，結果寫進 data.json。

用法：
  python update.py            # 正常執行（GitHub Actions 每小時跑這個）
  python update.py --limit 3  # 只看首頁前 3 篇，用來測試
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from hnzh import extract, hn, store, translate

FRONT_PAGE = 30
COMMENTS_AFTER = timedelta(hours=6)    # 上首頁滿 6 小時後才抓評論（讓討論累積）
COMMENTS_WINDOW = timedelta(hours=48)  # 超過 48 小時的文章就不再補評論（頁面也不會顯示了）
TOP_COMMENTS = 3


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


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
        if key not in stories:  # 第一次看到這篇
            url = item.get("url") or hn.hn_url(sid)
            stories[key] = {
                "id": sid,
                "title": item.get("title", ""),
                "url": url,
                "domain": domain_of(url),
                "hn_url": hn.hn_url(sid),
                "is_hn_post": "url" not in item,  # Ask HN / Show HN 之類沒有外部連結的文章
                "by": item.get("by", ""),
                "posted_at": store.to_iso(datetime.fromtimestamp(item["time"], timezone.utc)),
                "first_seen": now,
                "title_zh": None,
                "summary_zh": None,
                "translated_at": None,
                "model": None,
                "translate_tries": 0,  # 模型偶爾會漏掉一篇，最多重試 3 次就放棄
                "comments": None,
                "comments_done": False,
                "comments_at": None,
                "comment_tries": 0,
            }
        stories[key].update({
            "score": item.get("score", 0),
            "comments_count": item.get("descendants", 0),
            "last_seen": now,
            "on_front_page": True,
            "rank": rank,
        })
    return front


def translate_new(data: dict, front: list[dict]) -> int:
    """翻譯還沒有中文標題的文章（首頁的優先）。回傳翻譯篇數。"""
    stories = data["stories"]
    cutoff = store.now() - COMMENTS_WINDOW
    todo = [
        s for s in stories.values()
        if not s["title_zh"] and s.get("translate_tries", 0) < 3 and store.from_iso(s["first_seen"]) >= cutoff
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
    for batch in translate.chunks(payload, translate.BATCH_SIZE):
        try:
            result, model = translate.translate_stories(batch)
        except translate.QuotaExhausted as e:
            print(f"[翻譯] 額度用完，剩下的下小時再試：{e}")
            break
        for p in batch:
            stories[str(p["id"])]["translate_tries"] = stories[str(p["id"])].get("translate_tries", 0) + 1
        now = store.to_iso(store.now())
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
        if age > COMMENTS_WINDOW or s.get("comment_tries", 0) >= 3:
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
            print(f"[評論] 額度用完，剩下的下小時再試：{e}")
            break
        for e in batch:
            data["stories"][str(e["id"])]["comment_tries"] = data["stories"][str(e["id"])].get("comment_tries", 0) + 1
        for e in batch:
            summaries = result.get(e["id"])
            if not summaries:
                continue
            s = data["stories"][str(e["id"])]
            s["comments"] = [
                {"by": c["by"], "url": c["url"], "summary_zh": zh}
                for c, zh in zip(e["comments"], summaries)
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
    front = sync_front_page(data, args.limit)
    print(f"首頁 {len(front)} 篇")
    if not args.skip_translate:
        print(f"翻譯 {translate_new(data, front)} 篇")
        print(f"評論摘要 {summarize_due_comments(data)} 篇")
    removed = store.prune(data)
    store.save(data)
    print(f"移除 {removed} 篇舊文章，data.json 共 {len(data['stories'])} 篇")


if __name__ == "__main__":
    main()
