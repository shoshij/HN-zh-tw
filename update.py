#!/usr/bin/env python3
"""第一步：抓 HN 首頁、翻譯新文章、整理評論摘要，結果寫進 data.json。

用法：
  python update.py                                   # 正常執行（GitHub Actions 跑這個）
  python update.py --limit 3 --data /tmp/test.json   # 本機測試：只看前 3 篇，寫到別的檔案
  python update.py --skip-translate                  # 不呼叫 Gemini，只更新分數與評論數
  python update.py --check-health                    # 連續多次翻譯失敗就回傳錯誤（workflow 最後一步用）

翻譯階段的任何問題（額度用完、模型太忙、回應壞掉）都只會印警告，
不會讓程式失敗，因為不管怎樣都要把這次看到的首頁狀態存進 data.json。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hnzh import extract, hn, store, translate

FRONT_PAGE = 30
COMMENTS_AFTER = timedelta(hours=6)  # 上首頁滿 6 小時後才抓評論（讓討論累積）
WINDOW = timedelta(hours=48)         # 超過 48 小時的文章就不再處理（頁面也不會顯示了）
TOP_COMMENTS = 3
MAX_TRIES = 3            # 同一篇文章最多送給模型幾次，之後就放棄
MAX_BATCHES = 6          # 每次執行最多送幾批翻譯（約 48 篇），避免一次把免費額度用光
MAX_SINGLE_RETRIES = 16  # 整批回應壞掉時改成一篇一篇送；每次執行最多這樣多送幾個請求
CONTENT_RETRIES = 1      # 抓不到原文的文章，之後再試抓幾次
TIME_BUDGET = 15 * 60    # 秒；翻譯與評論摘要最多花這麼久，超過就留到下次，不要卡住排程
MAX_FAILED_ITEMS = 3     # 首頁文章超過這麼多篇抓不到，就當 HN API 不穩，這次不更新
FAIL_STREAK_ALERT = 6    # 連續這麼多次「有文章要翻卻一篇都沒翻成」，workflow 就亮紅燈通知

Checkpoint = Callable[[], None]


@dataclass
class Snapshot:
    """這次執行從 HN 抓到的原始資料，後面的步驟共用，不用重抓。"""
    items: dict[int, dict]  # 抓到的 item
    failed: set[int]        # 因網路錯誤沒抓到的 id（不是被刪除）


def warn(msg: str) -> None:
    """印警告。在 GitHub Actions 裡會變成執行摘要上的黃色標記。"""
    if os.environ.get("GITHUB_ACTIONS"):
        # GitHub 的標記只吃一行，換行要寫成 %0A
        print("::warning::" + msg.replace("%", "%25").replace("\r", "").replace("\n", "%0A"))
    else:
        print(f"[警告] {msg}")


def domain_of(url: str) -> str:
    return extract.host_of(url)


def safe_url(item: dict) -> str:
    """文章連結。沒有外部連結（Ask HN 等）或不是 http(s) 的，就用 HN 討論頁。"""
    url = item.get("url") or ""
    return url if url.startswith(("http://", "https://")) else hn.hn_url(item["id"])


def new_story(item: dict, now: str) -> dict:
    """第一次看到這篇文章時建立的紀錄。"""
    url = safe_url(item)
    return {
        "id": item["id"],
        "title": item.get("title", ""),
        "url": url,
        "domain": domain_of(url),
        "hn_url": hn.hn_url(item["id"]),
        "is_hn_post": url == hn.hn_url(item["id"]),  # Ask HN / Show HN 之類沒有外部連結的文章
        "by": item.get("by", ""),
        "posted_at": store.to_iso(datetime.fromtimestamp(item["time"], UTC)),
        "first_seen": now,
        "removed": False,          # 被 HN 刪除或標記 dead 之後設為 True，頁面不再顯示
        "title_zh": None,
        "summary_zh": None,
        "summary_en": None,
        "translated_at": None,
        "model": None,
        "translate_tries": 0,
        "retranslate": False,      # 標題或連結被改過，需要重翻（重翻好之前先顯示舊的翻譯）
        "content_missing": None,   # 翻譯時有沒有抓到原文；False 表示有
        "content_retries": 0,
        "comments": None,
        "comments_done": False,
        "comments_at": None,
        "comment_tries": 0,
    }


def sync_front_page(data: dict, limit: int) -> Snapshot:
    """抓首頁，更新 data["stories"] 裡每篇文章的排名、分數與評論數。回傳這次從 HN 抓到的原始資料。"""
    now = store.to_iso(store.now())
    ids = hn.top_story_ids(limit)
    raw, failed = hn.items(ids)
    if len(failed) > MAX_FAILED_ITEMS:
        # 太多篇抓不到時整次不更新：否則還在首頁的文章會全被標成「已離開首頁」
        raise hn.FetchError(f"首頁 {len(ids)} 篇裡有 {len(failed)} 篇抓不到，HN API 可能不穩，這次不更新")
    stories = data["stories"]

    for s in stories.values():  # 先全部標成「不在首頁」，下面再把還在的標回來
        s["on_front_page"] = False
        s["rank"] = None

    for rank, sid in enumerate(ids, 1):
        key = str(sid)
        if sid in failed:
            if key in stories:  # 只是暫時抓不到：它還在首頁，分數先沿用上次的
                stories[key].update({"last_seen": now, "on_front_page": True, "rank": rank})
            continue
        item = raw.get(sid)
        if not item or item.get("type") not in ("story", "poll"):  # 略過徵才廣告（job）等
            continue
        s = stories.get(key)
        if s is None:
            s = stories[key] = new_story(item, now)
        else:
            title, url = item.get("title", ""), safe_url(item)
            if title != s["title"] or url != s["url"]:
                # HN 版主常改標題或修正連結。舊的翻譯先留著，等新的翻好再換掉。
                print(f"[同步] 標題或連結變更，重新翻譯：{title[:60]!r}")
                s.update({
                    "title": title, "url": url, "domain": domain_of(url), "is_hn_post": url == hn.hn_url(sid),
                    "retranslate": True, "translate_tries": 0,
                })
        s.update({
            "score": item.get("score", 0),
            "comments_count": item.get("descendants", 0),
            "last_seen": now,
            "on_front_page": True,
            "rank": rank,
            "removed": False,
        })

    # 離開首頁的文章分數和評論數還會繼續變，而頁面會再顯示它們 48 小時，所以也要更新。
    cutoff = store.now() - WINDOW
    off = [
        s["id"] for s in stories.values()
        if not s["on_front_page"] and not s.get("removed") and store.from_iso(s["first_seen"]) >= cutoff
    ]
    more, more_failed = hn.items(off)
    for sid in off:
        s = stories[str(sid)]
        if sid in more:
            s["score"] = more[sid].get("score", s["score"])
            s["comments_count"] = more[sid].get("descendants", s["comments_count"])
        elif sid not in more_failed:  # 不是網路問題，是真的不見了：被刪除或被標記 dead
            print(f"[同步] 文章已被 HN 移除，頁面不再顯示：{s['title'][:60]!r}")
            s["removed"] = True
    raw.update(more)
    return Snapshot(raw, failed | more_failed)


def out_of_time(deadline: float, what: str) -> bool:
    if time.monotonic() < deadline:
        return False
    warn(f"{what}：這次的時間預算用完，剩下的下次繼續。")
    return True


def call_isolating(fn: Callable, batch: list[dict], singles: list[int], what: str) -> dict[int, tuple]:
    """呼叫 fn(batch)，回傳 {文章 id: (結果, 使用的模型)}。

    整批的回應不能用時（例如其中一篇觸發模型的安全過濾），改成一篇一篇送，
    這樣只有真正有問題的那一篇受影響，其他篇照常完成。
    singles[0] 是這次執行還能多送幾個「單篇」請求。QuotaExhausted 不在這裡處理，會往外丟。
    """
    try:
        result, model = fn(batch)
        return {sid: (value, model) for sid, value in result.items()}
    except translate.BatchFailed as e:
        if len(batch) == 1 or singles[0] < len(batch):
            warn(f"{what}：模型的回應不能用，下次再試。{e}")
            return {}
        warn(f"{what}：整批的回應不能用，改成一篇一篇送。{e}")
    singles[0] -= len(batch)
    out: dict[int, tuple] = {}
    for one in batch:
        try:
            result, model = fn([one])
            out.update({sid: (value, model) for sid, value in result.items()})
        except translate.BatchFailed as e:
            warn(f"{what}：這一篇無法處理：{one['title'][:50]!r}。{e}")
    return out


def needs_model(s: dict) -> bool:
    """這篇一定要送給模型嗎？（還沒翻、缺英文摘要，或標題連結被改過）"""
    return not s["title_zh"] or not s.get("summary_en") or bool(s.get("retranslate"))


def wants_content_retry(s: dict) -> bool:
    """上次抓不到原文、只能依標題寫摘要的文章，之後再試抓一次（網站可能只是當時連不上）。"""
    return bool(s.get("content_missing")) and s.get("content_retries", 0) < CONTENT_RETRIES and not s["is_hn_post"]


def translate_new(data: dict, snap: Snapshot, deadline: float, checkpoint: Checkpoint | None = None) -> tuple[int, int]:
    """翻譯需要翻譯的文章，首頁的優先。回傳 (翻好的篇數, 這次想送給模型的篇數)。"""
    stories = data["stories"]
    cutoff = store.now() - WINDOW
    todo = [
        s for s in stories.values()
        if (needs_model(s) or wants_content_retry(s))
        and s.get("translate_tries", 0) < MAX_TRIES
        and not s.get("removed")
        and store.from_iso(s["first_seen"]) >= cutoff
    ]
    todo.sort(key=lambda s: (not s["on_front_page"], s["rank"] or 0))
    if not todo:
        return 0, 0

    texts = extract.article_texts([s["url"] for s in todo if not s["is_hn_post"]])
    payload, contents = [], {}
    for s in todo:
        if s["is_hn_post"]:
            content = hn.html_to_text((snap.items.get(s["id"]) or {}).get("text", ""))
        else:
            content = texts.get(s["url"], "")
        if not needs_model(s):  # 只是想再試抓一次原文的
            s["content_retries"] = s.get("content_retries", 0) + 1
            if not content:
                continue  # 還是抓不到，維持原本依標題寫的摘要
            print(f"[翻譯] 這次抓到原文了，重寫摘要：{s['title'][:60]!r}")
        contents[s["id"]] = content
        payload.append({"id": s["id"], "title": s["title"], "url": s["url"], "content": content})

    done = 0
    singles = [MAX_SINGLE_RETRIES]
    batches = list(translate.chunks(payload, translate.BATCH_SIZE))
    if len(batches) > MAX_BATCHES:
        print(f"[翻譯] 待翻 {len(payload)} 篇，這次先做 {MAX_BATCHES * translate.BATCH_SIZE} 篇，其餘下次繼續")
    for batch in batches[:MAX_BATCHES]:
        if out_of_time(deadline, "翻譯"):
            break
        try:
            result = call_isolating(translate.translate_stories, batch, singles, "翻譯")
        except translate.QuotaExhausted as e:
            warn(f"翻譯：所有模型都不可用，剩下的文章下次再試。{e}")
            break
        now = store.to_iso(store.now())
        for p in batch:
            s = stories[str(p["id"])]
            s["translate_tries"] = s.get("translate_tries", 0) + 1
            if p["id"] not in result:
                continue
            zh, model = result[p["id"]]
            s.update(zh)
            s.update({
                "translated_at": now, "model": model, "retranslate": False,
                "content_missing": not contents[p["id"]],
            })
            done += 1
        if checkpoint:
            checkpoint()
    return done, len(payload)


def summarize_due_comments(data: dict, snap: Snapshot, deadline: float, checkpoint: Checkpoint | None = None) -> int:
    """對上首頁滿 6 小時、還沒做過評論摘要的文章，抓前 3 則評論並摘要。每篇只做一次。"""
    now = store.now()
    due = []
    for s in data["stories"].values():
        if s["comments_done"] or s.get("removed"):
            continue
        age = now - store.from_iso(s["first_seen"])
        if age < COMMENTS_AFTER:
            continue
        if age > WINDOW or s.get("comment_tries", 0) >= MAX_TRIES:
            s["comments_done"] = True  # 太舊了或試太多次，不再處理
            continue
        due.append(s)
    if not due:
        return 0

    # 正常情況下這些文章都已經在 snap 裡（首頁 + 48 小時內），這裡只是保險
    unknown = [s["id"] for s in due if s["id"] not in snap.items and s["id"] not in snap.failed]
    extra, extra_failed = hn.items(unknown) if unknown else ({}, set())

    stamp = store.to_iso(now)
    entries = []
    for s in due:
        sid = s["id"]
        if sid in snap.failed or sid in extra_failed:
            print(f"[評論] 文章 {sid} 暫時抓不到，下次再試")
            continue
        item = snap.items.get(sid) or extra.get(sid)
        try:
            comments = hn.top_comments(item, TOP_COMMENTS) if item else []
        except hn.FetchError as e:
            print(f"[評論] {e}，下次再試")
            continue
        if not comments:  # 真的沒有評論（或文章被刪了）也算做完，不要每次重抓
            s.update({"comments": [], "comments_done": True, "comments_at": stamp})
            continue
        entries.append({"id": sid, "title": s["title"], "title_zh": s["title_zh"], "comments": comments})

    done = 0
    singles = [MAX_SINGLE_RETRIES]
    for batch in translate.chunks(entries, translate.BATCH_SIZE):
        if out_of_time(deadline, "評論摘要"):
            break
        try:
            result = call_isolating(translate.summarize_comments, batch, singles, "評論摘要")
        except translate.QuotaExhausted as e:
            warn(f"評論摘要：所有模型都不可用，下次再試。{e}")
            break
        for e in batch:
            s = data["stories"][str(e["id"])]
            s["comment_tries"] = s.get("comment_tries", 0) + 1
            if e["id"] not in result:
                continue
            summaries, _model = result[e["id"]]
            s["comments"] = [
                {"by": c["by"], "url": c["url"], "summary_zh": zh}
                for c, zh in zip(e["comments"], summaries, strict=True)  # 數量已在 translate 裡核對過
            ]
            s["comments_done"] = True
            s["comments_at"] = stamp
            done += 1
        if checkpoint:
            checkpoint()
    return done


def check_health() -> int:
    """翻譯連續失敗太多次就回傳 1，讓 workflow 亮紅燈（GitHub 會寄信通知）。"""
    streak = store.load().get("translate_fail_streak", 0)
    if streak >= FAIL_STREAK_ALERT:
        msg = f"已經連續 {streak} 次有文章要翻卻一篇都沒翻成。請檢查 GEMINI_API_KEY 是否還有效、免費額度是否用完。"
        print(f"::error::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"[錯誤] {msg}")
        return 1
    print(f"翻譯狀態正常（連續失敗 {streak} 次）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=FRONT_PAGE, help="只看首頁前 N 篇（測試用，請搭配 --data）")
    ap.add_argument("--data", type=Path, help="改用別的資料檔，避免動到 data.json（本機測試用）")
    ap.add_argument("--skip-translate", action="store_true", help="不呼叫 Gemini，只更新分數與評論數")
    ap.add_argument("--check-health", action="store_true", help="只檢查翻譯是否連續失敗，不抓資料")
    args = ap.parse_args()
    if args.data:
        store.DATA_FILE = args.data.resolve()
    if args.check_health:
        return check_health()

    data = store.load()
    snap = sync_front_page(data, args.limit)  # HN 本身掛了就讓程式失敗，因為沒東西可更新
    print(f"首頁 {sum(1 for s in data['stories'].values() if s['on_front_page'])} 篇")

    def checkpoint() -> None:
        store.save(data)

    checkpoint()  # 先存一次：就算後面被取消或逾時，這次看到的首頁狀態也留得住

    if not args.skip_translate:
        deadline = time.monotonic() + TIME_BUDGET
        try:
            done, wanted = translate_new(data, snap, deadline, checkpoint)
            print(f"翻譯 {done} 篇")
            if done:
                data["translate_fail_streak"] = 0
            elif wanted:
                data["translate_fail_streak"] = data.get("translate_fail_streak", 0) + 1
        except Exception:  # noqa: BLE001 - 這裡出錯也要繼續做評論摘要並存檔
            data["translate_fail_streak"] = data.get("translate_fail_streak", 0) + 1
            warn("翻譯階段發生未預期的錯誤，已完成的部分照常儲存：\n" + traceback.format_exc())
        try:
            print(f"評論摘要 {summarize_due_comments(data, snap, deadline, checkpoint)} 篇")
        except Exception:  # noqa: BLE001
            warn("評論摘要階段發生未預期的錯誤，已完成的部分照常儲存：\n" + traceback.format_exc())

    removed = store.prune(data)
    store.save(data)
    print(f"移除 {removed} 篇舊文章，data.json 共 {len(data['stories'])} 篇")
    return 0


if __name__ == "__main__":
    sys.exit(main())
