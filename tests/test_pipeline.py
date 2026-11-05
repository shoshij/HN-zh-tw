"""整條 update.py 流程的離線測試。

HN、抓原文、Gemini 全部換成假的，時間也用假的時鐘往前撥，
所以可以在一瞬間演完「連續好幾個小時的執行」，檢查每篇文章的狀態怎麼變化。
"""
import time
import unittest
import unittest.mock as mock
from datetime import UTC, datetime, timedelta

import update
from hnzh import extract, hn, store, translate

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def hn_item(i, **kw):
    d = {"id": i, "type": "story", "title": f"Story {i}", "url": f"https://example.com/{i}", "time": 1_700_000_000 + i,
         "score": 10 + i, "descendants": i, "by": f"user{i}", "kids": [i * 100 + k for k in range(5)]}
    d.update(kw)
    return {k: v for k, v in d.items() if v is not None}


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.items = {i: hn_item(i) for i in range(1, 6)}   # HN 上現有的文章
        self.order = [1, 2, 3, 4, 5]                        # 首頁排名
        self.limit = 30
        self.now = T0
        self.net_down = set()        # 這些 id 暫時抓不到（網路錯誤）
        self.no_text = set()         # 這些文章抓不到原文
        self.sent = []               # 每次送給模型的文章 id
        self.comment_sent = []
        self.drop = set()            # 模型「漏掉」的文章 id
        self.poison = set()          # 只要批次裡有這些文章，模型的回應就不能用
        self.translate_error = None
        self.comment_error = None
        self.comment_bad = set()     # 模型回的評論摘要數量不對
        self.comment_fetch_error = set()
        self.warnings = []
        patches = [
            mock.patch.object(store, "now", lambda: self.now),
            mock.patch.object(hn, "top_story_ids", lambda limit=30: self.order[:limit]),
            mock.patch.object(hn, "items", self.fake_items),
            mock.patch.object(hn, "top_comments", self.fake_top_comments),
            mock.patch.object(extract, "article_texts", lambda urls, workers=8: {
                u: "" if int(u.rsplit("/", 1)[1]) in self.no_text else f"text of {u}" for u in urls}),
            mock.patch.object(translate, "translate_stories", self.fake_translate),
            mock.patch.object(translate, "summarize_comments", self.fake_comments),
            mock.patch.object(update, "warn", self.warnings.append),
            mock.patch("builtins.print"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.data = {"updated_at": None, "translate_fail_streak": 0, "stories": {}}

    # ---- 假的 HN 與模型 ----
    def fake_items(self, ids, workers=10):
        found = {i: self.items[i] for i in ids if i in self.items and i not in self.net_down}
        return found, {i for i in ids if i in self.net_down}

    def fake_top_comments(self, story, n=3):
        if story["id"] in self.comment_fetch_error:
            raise hn.FetchError("評論暫時抓不到")
        return [{"id": c, "by": f"c{c}", "text": f"comment {c}", "url": hn.hn_url(c)} for c in (story.get("kids") or [])[:n]]

    def fake_translate(self, batch):
        if self.translate_error:
            raise self.translate_error
        ids = [b["id"] for b in batch]
        self.sent.append(ids)
        self.last_payload = batch
        if self.poison & set(ids):
            raise translate.BatchFailed("SAFETY")
        return {b["id"]: {"title_zh": f"標題{b['id']}：{b['title']}", "summary_zh": "摘要。", "summary_en": "Summary."}
                for b in batch if b["id"] not in self.drop}, "fake-model"

    def fake_comments(self, batch):
        if self.comment_error:
            raise self.comment_error
        self.comment_sent.append([b["id"] for b in batch])
        return {b["id"]: [f"摘要 {c['id']}" for c in b["comments"]] for b in batch if b["id"] not in self.comment_bad}, "fake-model"

    def run_hour(self, hours=1):
        """把時鐘往前撥，然後跑一次完整流程。回傳 (翻譯篇數, 評論摘要篇數)。"""
        self.now += timedelta(hours=hours)
        snap = update.sync_front_page(self.data, self.limit)
        deadline = time.monotonic() + 60
        done, _wanted = update.translate_new(self.data, snap, deadline)
        commented = update.summarize_due_comments(self.data, snap, deadline)
        store.prune(self.data)
        return done, commented

    def story(self, i):
        return self.data["stories"][str(i)]

    # ---- 同步首頁 ----
    def test_new_stories_translated_once(self):
        self.assertEqual(self.run_hour()[0], 5)
        self.assertEqual(self.sent, [[1, 2, 3, 4, 5]])
        s = self.story(3)
        self.assertEqual((s["summary_en"], s["model"], s["rank"], s["on_front_page"], s["content_missing"]),
                         ("Summary.", "fake-model", 3, True, False))
        self.assertEqual(self.run_hour()[0], 0)
        self.assertEqual(len(self.sent), 1, "已翻過的文章不該再送一次")

    def test_scores_refresh_without_retranslating(self):
        self.run_hour()
        self.items[2].update(score=999, descendants=77)
        self.run_hour()
        self.assertEqual((self.story(2)["score"], self.story(2)["comments_count"]), (999, 77))
        self.assertEqual(len(self.sent), 1)

    def test_counts_keep_updating_after_leaving_front_page(self):
        self.run_hour()
        self.order = [1, 2, 3, 4]
        self.items[5].update(score=500, descendants=31)
        self.run_hour()
        s = self.story(5)
        self.assertEqual((s["on_front_page"], s["rank"], s["score"], s["comments_count"]), (False, None, 500, 31))

    def test_story_removed_by_hn_is_flagged_and_can_return(self):
        self.run_hour()
        self.order = [1, 2, 3, 4]
        gone = self.items.pop(5)                 # 被刪除或標記 dead：API 不再回傳
        self.run_hour()
        self.assertTrue(self.story(5)["removed"])
        self.items[5] = gone
        self.order = [1, 2, 3, 4, 5]
        self.run_hour()
        self.assertFalse(self.story(5)["removed"])

    def test_network_failure_is_not_treated_as_removed(self):
        self.run_hour()
        self.order = [1, 2, 3, 4]
        self.net_down = {5}
        self.run_hour()
        self.assertFalse(self.story(5)["removed"])

    def test_one_failed_item_keeps_its_place_on_front_page(self):
        self.run_hour()
        self.net_down = {2}
        self.run_hour()
        s = self.story(2)
        self.assertEqual((s["on_front_page"], s["rank"], s["score"]), (True, 2, 12))

    def test_many_failed_items_abort_without_touching_state(self):
        self.run_hour()
        self.net_down = {1, 2, 3, 4}
        with self.assertRaises(hn.FetchError):
            self.run_hour()
        self.assertTrue(all(s["on_front_page"] for s in self.data["stories"].values()), "不能把還在首頁的文章標成已離開")

    def test_job_posts_are_skipped_and_ranks_follow_hn(self):
        self.items[3]["type"] = "job"
        self.run_hour()
        self.assertNotIn("3", self.data["stories"])
        self.assertEqual(self.story(4)["rank"], 4)

    def test_non_http_link_falls_back_to_hn_page(self):
        self.items[1]["url"] = "javascript:alert(1)"
        self.run_hour()
        self.assertEqual(self.story(1)["url"], hn.hn_url(1))

    def test_ask_hn_uses_post_text(self):
        self.items[1] = hn_item(1, url=None, title="Ask HN: Why?", text="Hello<p>world &amp; all")
        self.run_hour()
        s = self.story(1)
        self.assertEqual((s["is_hn_post"], s["url"], s["domain"]), (True, hn.hn_url(1), "news.ycombinator.com"))
        self.assertEqual(next(p for p in self.last_payload if p["id"] == 1)["content"], "Hello\nworld & all")

    # ---- 翻譯 ----
    def test_title_edit_retranslates_only_that_story(self):
        self.run_hour()
        self.items[2]["title"] = "Story 2 (2019)"
        self.run_hour()
        self.assertEqual(self.sent[1], [2])
        self.assertEqual((self.story(2)["title"], self.story(2)["title_zh"], self.story(2)["retranslate"]),
                         ("Story 2 (2019)", "標題2：Story 2 (2019)", False))

    def test_title_edit_keeps_old_translation_until_new_one_arrives(self):
        self.run_hour()
        self.items[2]["title"] = "Story 2 (2019)"
        self.translate_error = translate.QuotaExhausted("all busy")
        self.run_hour()
        s = self.story(2)
        self.assertEqual((s["title_zh"], s["retranslate"]), ("標題2：Story 2", True), "模型掛掉時舊翻譯要留著")
        self.translate_error = None
        self.run_hour()
        self.assertEqual(self.story(2)["title_zh"], "標題2：Story 2 (2019)")

    def test_link_edit_retranslates(self):
        self.run_hour()
        self.items[4]["url"] = "https://other.example.org/4"
        self.run_hour()
        self.assertEqual(self.sent[1], [4])
        self.assertEqual(self.story(4)["domain"], "other.example.org")

    def test_dropped_story_retried_then_given_up(self):
        self.drop = {2}
        for _ in range(5):
            self.run_hour()
        self.assertEqual([b for b in self.sent if 2 in b], [[1, 2, 3, 4, 5], [2], [2]], "最多試 MAX_TRIES 次")
        self.assertIsNone(self.story(2)["title_zh"])

    def test_quota_exhausted_keeps_state_and_costs_no_try(self):
        self.translate_error = translate.QuotaExhausted("no models")
        self.assertEqual(self.run_hour()[0], 0)
        self.assertEqual(len(self.data["stories"]), 5, "文章仍要記錄下來，6 小時的計時才會開始")
        self.assertEqual(self.story(1)["translate_tries"], 0)
        self.translate_error = None
        self.assertEqual(self.run_hour()[0], 5)

    def test_poisonous_story_is_isolated(self):
        self.poison = {3}
        self.assertEqual(self.run_hour()[0], 4, "其他四篇不該被牽連")
        self.assertEqual(self.sent, [[1, 2, 3, 4, 5], [1], [2], [3], [4], [5]])
        self.assertEqual([self.story(i)["translate_tries"] for i in (1, 3)], [1, 1])
        self.assertIsNone(self.story(3)["title_zh"])
        self.assertTrue(any("無法處理" in w for w in self.warnings))

    def test_single_request_budget_limits_isolation(self):
        self.poison = {3}
        with mock.patch.object(update, "MAX_SINGLE_RETRIES", 4):      # 不夠拆 5 篇
            self.assertEqual(self.run_hour()[0], 0)
        self.assertEqual(self.sent, [[1, 2, 3, 4, 5]])

    def test_batch_cap_takes_front_page_first(self):
        self.items = {i: hn_item(i) for i in range(1, 71)}
        self.order = list(range(1, 71))
        self.limit = 70
        done, _ = self.run_hour()
        self.assertEqual(done, update.MAX_BATCHES * translate.BATCH_SIZE)
        self.assertEqual(self.sent[0][:3], [1, 2, 3])
        self.assertEqual(self.run_hour()[0], 70 - done, "剩下的下次補上")

    def test_time_budget_exhausted_sends_nothing(self):
        self.now += timedelta(hours=1)
        snap = update.sync_front_page(self.data, 30)
        self.assertEqual(update.translate_new(self.data, snap, time.monotonic() - 1), (0, 5))
        self.assertEqual(self.sent, [])

    def test_checkpoint_called_after_every_batch(self):
        self.items = {i: hn_item(i) for i in range(1, 21)}
        self.order = list(range(1, 21))
        self.now += timedelta(hours=1)
        snap = update.sync_front_page(self.data, 30)
        saves = []
        update.translate_new(self.data, snap, time.monotonic() + 60, lambda: saves.append(1))
        self.assertEqual(len(saves), 3)   # 20 篇 = 3 批

    def test_missing_article_text_gets_one_more_try(self):
        self.no_text = {4}
        self.run_hour()
        self.assertEqual((self.story(4)["content_missing"], self.story(4)["content_retries"]), (True, 0))
        self.run_hour()                                  # 還是抓不到：不再打擾模型
        self.assertEqual((self.story(4)["content_retries"], len(self.sent)), (1, 1))
        self.run_hour()
        self.assertEqual(len(self.sent), 1, "只再試一次")

    def test_article_text_recovered_on_retry_rewrites_summary(self):
        self.no_text = {4}
        self.run_hour()
        self.no_text = set()
        self.run_hour()
        self.assertEqual(self.sent[1], [4])
        self.assertEqual((self.story(4)["content_missing"], self.story(4)["translate_tries"]), (False, 2))

    # ---- 評論 ----
    def test_comments_after_six_hours_exactly_once(self):
        self.run_hour()                      # 第一次看到
        for _ in range(5):
            self.assertEqual(self.run_hour()[1], 0, "未滿 6 小時不該抓評論")
        self.assertEqual(self.run_hour()[1], 5)   # 滿 6 小時
        s = self.story(1)
        self.assertEqual([c["summary_zh"] for c in s["comments"]], ["摘要 100", "摘要 101", "摘要 102"])
        self.assertEqual((s["comments"][0]["by"], s["comments"][0]["url"], s["comments_done"]), ("c100", hn.hn_url(100), True))
        self.assertEqual(self.run_hour()[1], 0)
        self.assertEqual(len(self.comment_sent), 1, "只做一次")

    def test_comments_done_even_if_story_left_front_page(self):
        self.run_hour()
        self.order = [1]
        self.run_hour(6)
        self.assertEqual(len(self.story(5)["comments"]), 3)

    def test_story_without_comments_is_marked_done(self):
        self.items[2]["kids"] = []
        self.run_hour()
        self.run_hour(6)
        self.assertEqual((self.story(2)["comments"], self.story(2)["comments_done"]), ([], True))

    def test_network_failure_does_not_burn_the_only_attempt(self):
        self.run_hour()
        self.net_down = {2}                     # 文章本身抓不到
        self.comment_fetch_error = {3}          # 評論抓不到
        self.assertEqual(self.run_hour(6)[1], 3)
        for i in (2, 3):
            self.assertEqual((self.story(i)["comments_done"], self.story(i)["comments"], self.story(i)["comment_tries"]), (False, None, 0))
        self.net_down, self.comment_fetch_error = set(), set()
        self.assertEqual(self.run_hour()[1], 2)
        self.assertEqual(len(self.story(2)["comments"]), 3)

    def test_comment_mismatch_retried_then_given_up(self):
        self.comment_bad = {3}
        self.run_hour()
        self.run_hour(6)
        self.assertFalse(self.story(3)["comments_done"])
        for _ in range(4):
            self.run_hour()
        self.assertEqual((self.story(3)["comments_done"], self.story(3)["comments"]), (True, None))
        self.assertEqual(sum(1 for b in self.comment_sent if 3 in b), update.MAX_TRIES)

    def test_comment_quota_exhausted_retries_next_hour(self):
        self.run_hour()
        self.comment_error = translate.QuotaExhausted("x")
        self.assertEqual(self.run_hour(6)[1], 0)
        self.assertEqual(self.story(1)["comment_tries"], 0)
        self.comment_error = None
        self.assertEqual(self.run_hour()[1], 5)

    def test_stories_older_than_window_skip_comments(self):
        self.run_hour()
        self.comment_error = translate.QuotaExhausted("x")
        self.run_hour(6)
        self.comment_error = None
        self.run_hour(48)
        self.assertEqual(self.comment_sent, [])
        self.assertTrue(self.story(1)["comments_done"])

    # ---- 清理、整體流程、健康檢查 ----
    def test_prune_after_keep_days(self):
        self.run_hour()
        self.order = [1]
        self.run_hour(24 * store.KEEP_DAYS + 1)
        self.assertEqual(list(self.data["stories"]), ["1"])

    def run_main(self, *argv):
        saved = []
        with mock.patch.object(store, "load", lambda: self.data), \
             mock.patch.object(store, "save", lambda d: saved.append(len(d["stories"]))), \
             mock.patch("sys.argv", ["update.py", *argv]):
            self.now += timedelta(hours=1)
            code = update.main()
        return code, saved

    def test_main_saves_early_and_after_each_step(self):
        code, saved = self.run_main()
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(saved), 3, "同步後、每批翻譯後、最後各存一次")
        self.assertEqual(saved[0], 5)
        self.assertEqual(self.data["translate_fail_streak"], 0)

    def test_main_survives_a_crash_in_translation_and_still_does_comments(self):
        self.run_hour()
        self.now += timedelta(hours=5)
        self.items[6] = hn_item(6)
        self.order = [1, 2, 3, 4, 5, 6]
        self.translate_error = RuntimeError("boom")
        code, saved = self.run_main()
        self.assertEqual(code, 0)
        self.assertTrue(saved)
        self.assertEqual(len(self.story(1)["comments"]), 3, "翻譯掛掉不該連評論摘要都跳過")
        self.assertEqual(self.data["translate_fail_streak"], 1)
        self.assertTrue(any("boom" in w for w in self.warnings))

    def test_fail_streak_counts_runs_with_work_but_no_result(self):
        self.translate_error = translate.QuotaExhausted("key revoked")
        for _ in range(3):
            self.run_main()
        self.assertEqual(self.data["translate_fail_streak"], 3)
        self.translate_error = None
        self.run_main()
        self.assertEqual(self.data["translate_fail_streak"], 0)
        self.run_main()                                   # 沒有新文章：不算失敗
        self.assertEqual(self.data["translate_fail_streak"], 0)

    def test_check_health(self):
        with mock.patch.object(store, "load", lambda: {"translate_fail_streak": update.FAIL_STREAK_ALERT - 1}):
            self.assertEqual(update.check_health(), 0)
        with mock.patch.object(store, "load", lambda: {"translate_fail_streak": update.FAIL_STREAK_ALERT}):
            self.assertEqual(update.check_health(), 1)


if __name__ == "__main__":
    unittest.main()
