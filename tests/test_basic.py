"""各模組的單元測試。全部離線：不碰網路，也不呼叫 Gemini。執行：python -m unittest -v"""
import importlib
import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock as mock
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path

import requests

import update
from hnzh import extract, hn, render, store, translate


# ---------------------------------------------------------------- hn.py
class HnClient(unittest.TestCase):
    def with_api(self, table):
        """table: {路徑結尾: 回傳值 或 Exception}"""
        def fake(url):
            value = table.get(url.removeprefix(hn.BASE))   # 沒列出的就當作不存在
            if isinstance(value, Exception):
                raise value
            return value
        return mock.patch.object(hn, "_get_json", fake)

    def test_html_to_text(self):
        self.assertEqual(hn.html_to_text("Hello<p>world &amp; <i>more</i></p>"), "Hello\nworld & more")

    def test_items_separates_missing_from_failed(self):
        table = {
            "/item/1.json": {"id": 1, "type": "story"},
            "/item/2.json": None,                              # 不存在
            "/item/3.json": {"id": 3, "deleted": True},        # 被刪除
            "/item/4.json": {"id": 4, "dead": True},           # 被標記 dead
            "/item/5.json": hn.FetchError("timeout"),          # 網路出錯
        }
        with self.with_api(table), mock.patch("builtins.print"):
            found, failed = hn.items([1, 2, 3, 4, 5])
        self.assertEqual(list(found), [1])
        self.assertEqual(failed, {5})

    def test_get_json_retries_then_raises(self):
        calls = []

        def boom(url, timeout):
            calls.append(url)
            raise requests.ConnectionError("down")

        with mock.patch.object(hn.session, "get", boom), mock.patch.object(hn.time, "sleep"), self.assertRaises(hn.FetchError):
            hn._get_json(hn.BASE + "/topstories.json")
        self.assertEqual(len(calls), hn.ATTEMPTS)

    def test_top_comments_skips_deleted_and_stops_at_n(self):
        table = {f"/item/{i}.json": {"id": i, "type": "comment", "by": f"u{i}", "text": f"c{i}"} for i in (11, 13, 14, 15)}
        table["/item/12.json"] = {"id": 12, "deleted": True}
        with self.with_api(table):
            got = hn.top_comments({"id": 1, "kids": [11, 12, 13, 14, 15, 16, 17]}, 3)
        self.assertEqual([c["id"] for c in got], [11, 13, 14])
        self.assertEqual(got[0]["url"], hn.hn_url(11))

    def test_top_comments_raises_when_a_needed_comment_failed(self):
        table = {"/item/11.json": hn.FetchError("x"), "/item/12.json": {"id": 12, "type": "comment", "text": "c"}}
        with self.with_api(table), mock.patch("builtins.print"), self.assertRaises(hn.FetchError):
            hn.top_comments({"id": 1, "kids": [11, 12]}, 3)

    def test_top_comments_empty_when_no_kids(self):
        self.assertEqual(hn.top_comments({"id": 1}, 3), [])


# ---------------------------------------------------------------- extract.py
ARTICLE = ("<html><head><title>T</title></head><body><nav>menu</nav><article><h1>Robots</h1>"
           + "".join(f"<p>Paragraph {i} explains how the robot arm folds laundry with reinforcement learning.</p>" for i in range(8))
           + "</article></body></html>").encode()


class FakeResponse:
    def __init__(self, body=b"", status=200, ctype="text/html; charset=utf-8", chunk=65536):
        self.body, self.status_code, self.headers, self.chunk = body, status, {"Content-Type": ctype}, chunk
        self.ok = status < 400

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, size):
        for i in range(0, len(self.body), self.chunk):
            yield self.body[i : i + self.chunk]


class Extract(unittest.TestCase):
    def fetch(self, response, url="https://example.com/post"):
        get = (lambda *a, **k: response) if not isinstance(response, Exception) else mock.Mock(side_effect=response)
        with mock.patch.object(extract.requests, "get", get), mock.patch("builtins.print") as out, \
             mock.patch.object(extract.time, "sleep"):
            text = extract.article_text(url)
        return text, " ".join(str(c.args[0]) for c in out.call_args_list)

    def test_html_article_is_extracted(self):
        text, _ = self.fetch(FakeResponse(ARTICLE))
        self.assertIn("robot arm folds laundry", text)
        self.assertNotIn("menu", text)

    def test_error_page_is_not_summarised(self):
        text, log = self.fetch(FakeResponse(ARTICLE, status=403))
        self.assertEqual(text, "")
        self.assertIn("HTTP 403", log)

    def test_non_html_is_rejected_before_reading_body(self):
        resp = FakeResponse(b"x" * 100, ctype="video/mp4")
        resp.iter_content = mock.Mock(side_effect=AssertionError("不該讀內容"))
        text, log = self.fetch(resp)
        self.assertEqual(text, "")
        self.assertIn("不是網頁", log)

    def test_oversized_body_is_abandoned(self):
        with mock.patch.object(extract, "MAX_BYTES", 1000):
            text, log = self.fetch(FakeResponse(b"<html>" + b"a" * 5000, chunk=400))
        self.assertEqual(text, "")
        self.assertIn("超過", log)

    def test_slow_drip_hits_deadline(self):
        with mock.patch.object(extract, "DEADLINE", -1):
            text, log = self.fetch(FakeResponse(ARTICLE, chunk=50))
        self.assertEqual(text, "")
        self.assertIn("下載超過", log)

    def test_plain_text_is_decoded_as_utf8(self):
        body = ("純文字檔案，內容是繁體中文。" * 10).encode("utf-8")
        text, _ = self.fetch(FakeResponse(body, ctype="text/plain"))   # 沒有標明 charset
        self.assertTrue(text.startswith("純文字檔案"))

    def test_too_little_text_counts_as_nothing(self):
        text, log = self.fetch(FakeResponse(b"<html><body><p>Please enable JavaScript.</p></body></html>"))
        self.assertEqual(text, "")
        self.assertIn("抽不出正文", log)

    def test_timeout_and_connection_error(self):
        self.assertEqual(self.fetch(requests.ReadTimeout("slow"))[0], "")
        self.assertIn("連線失敗", self.fetch(requests.ConnectionError("refused"))[1])

    def test_skipped_without_request(self):
        with mock.patch.object(extract.requests, "get", mock.Mock(side_effect=AssertionError("不該連線"))), mock.patch("builtins.print"):
            self.assertEqual(extract.article_text("https://www.youtube.com/watch?v=1"), "")
            self.assertEqual(extract.article_text("https://example.com/paper.PDF?x=1"), "")
            self.assertEqual(extract.article_text("http://[bad/x"), "")
            self.assertEqual(extract.article_text(""), "")

    def test_parsing_runs_in_a_separate_process(self):
        # 真的開子程序解析（不 mock），確認結果一樣、而且是走 subprocess 而非在本程序內呼叫
        with mock.patch.object(extract.subprocess, "run", wraps=extract.subprocess.run) as run:
            text, _ = self.fetch(FakeResponse(ARTICLE))
        self.assertIn("robot arm folds laundry", text)
        self.assertEqual(run.call_count, 1)

    def test_parser_crash_only_loses_that_story(self):
        # lxml 當掉時子程序被 signal 11 殺掉；主程序要活著，並把原因記下來
        crashed = subprocess.CompletedProcess(args=[], returncode=-11, stdout=b"", stderr=b"")
        with mock.patch.object(extract.subprocess, "run", return_value=crashed):
            text, log = self.fetch(FakeResponse(ARTICLE))
        self.assertEqual(text, "")
        self.assertIn("解析器當掉（signal 11）", log)

    def test_parser_hang_is_cut_off(self):
        with mock.patch.object(extract.subprocess, "run", side_effect=subprocess.TimeoutExpired("python", 60)):
            text, log = self.fetch(FakeResponse(ARTICLE))
        self.assertEqual(text, "")
        self.assertIn("解析超過", log)

    def test_parser_exception_is_reported(self):
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"Traceback...\nValueError: bad html\n")
        with mock.patch.object(extract.subprocess, "run", return_value=failed):
            text, log = self.fetch(FakeResponse(ARTICLE))
        self.assertEqual(text, "")
        self.assertIn("ValueError: bad html", log)


# ---------------------------------------------------------------- store.py
class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(store, "DATA_FILE", Path(self.dir.name) / "data.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_missing_file(self):
        self.assertEqual(store.load()["stories"], {})

    def test_save_round_trip_leaves_no_temp_file(self):
        store.save({"stories": {"1": {"title_zh": "標題"}}})
        self.assertEqual(os.listdir(self.dir.name), ["data.json"])
        self.assertEqual(json.loads(store.DATA_FILE.read_text(encoding="utf-8"))["stories"]["1"]["title_zh"], "標題")

    def test_save_survives_unpaired_surrogate(self):
        store.save({"stories": {"1": {"summary_zh": "壞字元 \ud83d 在這"}}})
        self.assertIn("壞字元", store.DATA_FILE.read_text(encoding="utf-8"))

    def test_failed_save_keeps_the_old_file(self):
        store.save({"stories": {"1": {}}})
        with self.assertRaises(TypeError):
            store.save({"stories": {"2": {"x": {1, 2}}}})       # set 不能轉成 JSON
        self.assertIn('"1"', store.DATA_FILE.read_text(encoding="utf-8"))

    def test_load_adds_new_fields_to_old_records(self):
        old = {"updated_at": None, "stories": {
            "1": {"title_zh": "有", "summary_zh": "摘要（無法取得原文內容，僅依標題判斷）", "translate_tries": 1},
            "2": {"title_zh": "有", "summary_zh": "正常摘要", "summary_en": "ok", "translate_tries": 1},
        }}
        store.DATA_FILE.write_text(json.dumps(old), encoding="utf-8")
        s = store.load()["stories"]
        self.assertEqual((s["1"]["summary_en"], s["1"]["translate_tries"], s["1"]["content_missing"]), (None, 0, True))
        self.assertEqual((s["2"]["translate_tries"], s["2"]["content_missing"], s["2"]["retranslate"], s["2"]["removed"]), (1, False, False, False))

    def test_prune(self):
        now = store.now()
        data = {"stories": {"old": {"last_seen": store.to_iso(now - timedelta(days=8))},
                            "new": {"last_seen": store.to_iso(now - timedelta(days=6))}}}
        self.assertEqual(store.prune(data), 1)
        self.assertEqual(list(data["stories"]), ["new"])


# ---------------------------------------------------------------- translate.py
class MatchItems(unittest.TestCase):
    batch = [{"id": 1, "title": "Rust 2.0 released"}, {"id": 2, "title": "Why the sky is blue"}]

    def match(self, items, batch=None):
        with mock.patch("builtins.print"):
            return translate.match_items(batch or self.batch, items)

    def test_title_wins_over_wrong_id(self):
        m = self.match([{"id": 2, "title": "Rust 2.0 released", "x": "a"}, {"id": 1, "title": "Why the sky is blue", "x": "b"}])
        self.assertEqual((m[1]["x"], m[2]["x"]), ("a", "b"))

    def test_fuzzy_title(self):
        self.assertEqual(self.match([{"id": 9, "title": "Rust 2.0 Released!", "x": "a"}])[1]["x"], "a")

    def test_unmatched_is_dropped(self):
        self.assertEqual(self.match([{"id": 1, "title": "Completely different", "x": "a"}]), {})

    def test_rewritten_title_accepted_only_with_matching_id(self):
        batch = [{"id": 7, "title": "The Bitter Lesson [pdf]"}, {"id": 8, "title": "Zed is 1.0"}]
        self.assertIn(7, self.match([{"id": 7, "title": "The Bitter Lesson"}], batch))
        self.assertEqual(self.match([{"id": 8, "title": "The Bitter Lesson"}], batch), {})

    def test_garbage_items_are_ignored(self):
        self.assertEqual(self.match(["oops", None, {"id": 1, "title": None}]), {})


class TranslateHelpers(unittest.TestCase):
    def test_clean_title_removes_note_only(self):
        self.assertEqual(translate.clean_title("北韓假面試事件（無法取得原文內容，僅依標題判斷）"), "北韓假面試事件")
        self.assertEqual(translate.clean_title("機械手臂（robotic arm）"), "機械手臂（robotic arm）")

    def test_clean_text_strips_control_and_unpaired_characters(self):
        self.assertEqual(translate.clean_text("  摘要\x0b一\x00二 \ud83d三\n"), "摘要一二 三")

    def test_empty_env_falls_back_to_default_models(self):
        with mock.patch.dict(os.environ, {"GEMINI_MODELS": ""}):   # Actions 沒設定變數時就是這樣
            mod = importlib.reload(translate)
            try:
                self.assertEqual(mod.MODELS[0], "gemini-3.5-flash-lite")
                self.assertGreater(len(mod.MODELS), 3)
            finally:
                os.environ.pop("GEMINI_MODELS", None)
                importlib.reload(translate)


def ok_body(obj):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}, "finishReason": "STOP"}]}


class Generate(unittest.TestCase):
    """用假的 requests.post 驗證模型之間怎麼切換。script: {模型: 結果}，
    結果可以是 "timeout"、HTTP 狀態碼、(狀態碼, 內文)，或是 200 回應的 JSON（dict）。"""

    def setUp(self):
        self.down = set()

    def run_generate(self, script):
        calls, sleeps = [], []

        def post(url, **kw):
            model = url.split("/models/")[1].split(":")[0]
            calls.append(model)
            outcome = script[model]
            if outcome == "timeout":
                raise requests.ReadTimeout("slow")
            if isinstance(outcome, int):
                return mock.Mock(status_code=outcome, text=f"http {outcome}")
            if isinstance(outcome, tuple):
                return mock.Mock(status_code=outcome[0], text=outcome[1])
            return mock.Mock(status_code=200, json=lambda: outcome)

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x"}), \
             mock.patch.object(translate, "MODELS", list(script)), \
             mock.patch.object(translate, "_down", self.down), \
             mock.patch.object(translate.time, "sleep", sleeps.append), \
             mock.patch.object(translate.requests, "post", post), \
             mock.patch("builtins.print"):
            try:
                return translate._generate("p", translate.STORY_SCHEMA), calls, sleeps
            except Exception as e:
                e.calls, e.sleeps = calls, sleeps
                raise

    def test_timeout_and_503_move_to_next_model(self):
        (data, model), calls, _ = self.run_generate({"m1": "timeout", "m2": 503, "m3": ok_body({"items": []})})
        self.assertEqual((model, calls), ("m3", ["m1", "m2", "m2", "m3"]))   # 503 會重試一次，逾時不會
        self.assertEqual(self.down, {"m1", "m2"})

    def test_failed_model_is_skipped_for_later_batches(self):
        script = {"m1": "timeout", "m2": ok_body({"items": []})}
        self.run_generate(script)
        _, calls, _ = self.run_generate(script)      # 同一次執行的第二批
        self.assertEqual(calls, ["m2"])

    def test_unsupported_model_moves_on(self):
        (_, model), calls, _ = self.run_generate({"m1": 400, "m2": ok_body({"items": []})})
        self.assertEqual((model, calls), ("m2", ["m1", "m2"]))

    def test_all_down_raises_quota(self):
        with self.assertRaises(translate.QuotaExhausted):
            self.run_generate({"m1": 429, "m2": "timeout"})

    def test_unusable_answer_tries_next_model_without_benching(self):
        blocked = {"candidates": [{"finishReason": "SAFETY"}]}
        (_, model), calls, _ = self.run_generate({"m1": blocked, "m2": ok_body({"items": []})})
        self.assertEqual((model, calls), ("m2", ["m1", "m2"]))
        self.assertEqual(self.down, set(), "模型沒壞，下一批還要用它")

    def test_all_unusable_is_batch_failed(self):
        bad = {"candidates": [{"content": {"parts": [{"text": "{not json"}]}}]}
        for script in ({"m1": bad}, {"m1": {"promptFeedback": {"blockReason": "SAFETY"}}}, {"m1": ok_body([1, 2])}, {"m1": bad, "m2": 503}):
            with self.subTest(script=list(script.values())), self.assertRaises(translate.BatchFailed):
                self.run_generate(script)

    def test_json_split_across_parts_is_joined_and_thoughts_ignored(self):
        body = {"candidates": [{"content": {"parts": [{"text": "thinking", "thought": True}, {"text": '{"items": '}, {"text": "[]}"}]}}]}
        (data, _), _, _ = self.run_generate({"m1": body})
        self.assertEqual(data, {"items": []})

    def test_429_waits_for_the_suggested_delay(self):
        _, _, sleeps = self.run_generate({"m1": (429, '{"retryDelay": "7s"}'), "m2": ok_body({"items": []})})
        self.assertEqual(sleeps, [8.0])

    def test_429_with_long_delay_is_not_waited_for(self):
        _, calls, sleeps = self.run_generate({"m1": (429, '{"retryDelay": "3600s"}'), "m2": ok_body({"items": []})})
        self.assertEqual((calls, sleeps), (["m1", "m2"], []))


class TranslateCalls(unittest.TestCase):
    def call(self, fn, batch, items):
        with mock.patch.object(translate, "_generate", lambda prompt, schema: ({"items": items}, "m")), mock.patch("builtins.print"):
            return fn(batch)[0]

    def test_incomplete_story_fields_are_skipped(self):
        batch = [{"id": 1, "title": "A", "content": "x"}, {"id": 2, "title": "B", "content": ""}]
        got = self.call(translate.translate_stories, batch, [
            {"id": 1, "title": "A", "title_zh": "甲（無法取得原文內容，僅依標題判斷）", "summary_zh": " 摘要 ", "summary_en": "Sum."},
            {"id": 2, "title": "B", "title_zh": "乙", "summary_zh": "摘要"},          # 少了英文摘要
        ])
        self.assertEqual(got, {1: {"title_zh": "甲", "summary_zh": "摘要", "summary_en": "Sum."}})

    def test_comment_count_and_empty_summaries_are_rejected(self):
        c = [{"by": "a", "text": "x"}, {"by": "b", "text": "y"}]
        batch = [{"id": i, "title": t, "comments": c} for i, t in ((1, "A"), (2, "B"), (3, "C"), (4, "D"))]
        got = self.call(translate.summarize_comments, batch, [
            {"id": 1, "title": "A", "comments": ["一", "二"]},
            {"id": 2, "title": "B", "comments": ["只有一則"]},
            {"id": 3, "title": "C", "comments": ["有", "  "]},
            {"id": 4, "title": "D", "comments": "不是陣列"},
        ])
        self.assertEqual(got, {1: ["一", "二"]})


# ---------------------------------------------------------------- render.py
def story(**kw):
    now = store.now()
    base = {
        "id": 1, "title": "A robot arm", "title_zh": "機械手臂", "summary_zh": "中文摘要。", "summary_en": "English summary.",
        "url": "https://example.com/a", "domain": "example.com", "hn_url": hn.hn_url(1), "is_hn_post": False,
        "score": 10, "comments_count": 2, "posted_at": store.to_iso(now), "first_seen": store.to_iso(now),
        "last_seen": store.to_iso(now), "on_front_page": True, "rank": 1, "removed": False,
        "comments": [{"by": "alice", "url": hn.hn_url(9), "summary_zh": "評論摘要。"}], "comments_done": True,
    }
    return {**base, **kw}


class Tags(unittest.TestCase):
    def tags(self, **kw):
        return render.tags_for({"title": "", "title_zh": "", "summary_zh": "", "summary_en": "", "domain": "", **kw})

    def test_word_boundary(self):
        self.assertEqual(self.tags(title="He said hi"), [])
        self.assertEqual(self.tags(title="AI eats software"), ["AI"])

    def test_latin_next_to_chinese_without_spaces(self):
        self.assertEqual(self.tags(summary_zh="使用AI模型訓練"), ["AI"])

    def test_chinese_substring(self):
        self.assertEqual(self.tags(title_zh="學會摺衣服的機械手臂"), ["robotics"])

    def test_english_summary_is_searched(self):
        self.assertEqual(self.tags(summary_en="An open-source robot simulator"), ["robotics", "simulation"])

    def test_known_false_positives(self):
        for text in ("Kitty 0.50", "Why robots.txt matters", "Silicon Valley layoffs", "Claude Shannon's legacy",
                     "Isaac Newton's notes", "Fast DOM manipulation", "The User-Agent header", "Power transformers explained"):
            with self.subTest(text=text):
                self.assertEqual(self.tags(title=text), [])

    def test_terminal_emulator_as_the_model_writes_it(self):
        # 正式資料裡 Kitty 那篇的寫法：模型會在中文後面加括號英文
        zh = "基於 GPU 加速的跨平台終端機（terminal）模擬器 Kitty 發布 0.50 新版本"
        self.assertNotIn("simulation", self.tags(summary_zh=zh, summary_en="Kitty, a cross-platform terminal emulator"))
        self.assertIn("simulation", self.tags(summary_zh="用物理模擬訓練機器人"))


class Render(unittest.TestCase):
    def test_translate_url(self):
        u = render.translate_url("https://example.com/a?b=1&c=2")
        self.assertTrue(u.startswith("https://translate.google.com/translate?"))
        self.assertIn("tl=zh-TW", u)
        self.assertIn("u=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1%26c%3D2", u)
        for bad in ("https://example.com/paper.PDF", "https://www.youtube.com/watch?v=x", "http://[bad/x", ""):
            self.assertIsNone(render.translate_url(bad))

    def test_select_stories_window_and_removed(self):
        now = store.now()
        old = store.to_iso(now - timedelta(hours=60))
        data = {"stories": {
            "1": story(id=1, rank=2),
            "2": story(id=2, rank=1, first_seen=old),                                   # 上首頁超過 48 小時但還在首頁
            "3": story(id=3, on_front_page=False, rank=None, first_seen=old),           # 太舊
            "4": story(id=4, on_front_page=False, rank=None, removed=True),             # 被 HN 移除
            "5": story(id=5, on_front_page=False, rank=None, first_seen=store.to_iso(now - timedelta(hours=3))),
        }}
        front, rest = render.select_stories(data)
        self.assertEqual(([s["id"] for s in front], [s["id"] for s in rest]), ([2, 1], [5]))
        self.assertEqual(front[0]["tags"], ["robotics"])

    def test_rss_is_well_formed_and_skips_untranslated(self):
        data = {"updated_at": store.to_iso(store.now()), "stories": {}}
        stories = [
            {**story(id=1, title_zh="含控制字元\x0b的標題", summary_zh="摘要 <b>&</b> \x00"), "tags": ["AI"], "translate_url": None},
            {**story(id=2, title_zh=None, summary_zh=None, summary_en=None), "tags": [], "translate_url": None},
        ]
        root = ET.fromstring(render.render_rss(data, stories))          # 解析得過就代表是合法的 XML
        items = root.findall("./channel/item")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].findtext("title"), "含控制字元的標題 | A robot arm")
        self.assertEqual(items[0].findtext("category"), "AI")
        self.assertIn("&lt;b&gt;&amp;&lt;/b&gt;", items[0].findtext("description"))

    def test_rss_links_are_valid_urls_and_titles_use_numeric_entities(self):
        data = {"updated_at": store.to_iso(store.now()), "stories": {}}
        s = {**story(title="AT&T <vs> T-Mobile", url="https://example.com/a b/caf\u00e9?x=1&y=%20z\u2019"), "tags": [], "translate_url": None}
        raw = render.render_rss(data, [s]).decode("utf-8")
        item = ET.fromstring(raw).find("./channel/item")
        self.assertEqual(item.findtext("link"), "https://example.com/a%20b/caf%C3%A9?x=1&y=%20z%E2%80%99")
        self.assertEqual(item.findtext("title"), "機械手臂 | AT&T <vs> T-Mobile")        # 解析後內容不變
        self.assertIn("AT&#x26;T &#x3C;vs&gt; T-Mobile</title>", raw)

    def test_html_escapes_hostile_strings(self):
        data = {"updated_at": store.to_iso(store.now()), "stories": {}}
        evil = story(title='"><script>alert(1)</script>', title_zh="<img src=x onerror=alert(1)>", summary_zh="</p><script>x</script>",
                     comments=[{"by": "<b>bob</b>", "url": hn.hn_url(9), "summary_zh": "<i>hi</i>"}])
        evil.update(tags=["AI"], translate_url=render.translate_url(evil["url"]))
        html = render.render_html(data, [evil], [])
        self.assertEqual(html.count("<script>"), 1, "只能有頁面自己的那一段 script")
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<b>bob</b>", html)


# ---------------------------------------------------------------- update.py 的小工具
class UpdateHelpers(unittest.TestCase):
    def test_domain_of(self):
        self.assertEqual(update.domain_of("https://www.example.com/a?b=c"), "example.com")
        self.assertEqual(update.domain_of("https://blog.example.com/"), "blog.example.com")
        self.assertEqual(update.domain_of("http://[bad/x"), "")

    def test_safe_url_only_allows_http(self):
        self.assertEqual(update.safe_url({"id": 5, "url": "https://example.com/x"}), "https://example.com/x")
        for bad in ("javascript:alert(1)", "data:text/html,x", "", None):
            self.assertEqual(update.safe_url({"id": 5, "url": bad}), hn.hn_url(5))

    def test_warning_is_one_line_on_github(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), mock.patch("builtins.print") as out:
            update.warn("第一行\n第二行 100%")
        self.assertEqual(out.call_args.args[0], "::warning::第一行%0A第二行 100%25")


if __name__ == "__main__":
    unittest.main()
