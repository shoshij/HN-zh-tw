"""純邏輯的單元測試，不碰網路也不碰 Gemini。執行：python -m unittest -v"""
import unittest
import unittest.mock

import update
from hnzh import hn, render, translate


class HtmlToText(unittest.TestCase):
    def test_strips_tags_and_entities(self):
        self.assertEqual(hn.html_to_text("Hello<p>world &amp; <i>more</i></p>"), "Hello\nworld & more")


class MatchItems(unittest.TestCase):
    batch = [{"id": 1, "title": "Rust 2.0 released"}, {"id": 2, "title": "Why the sky is blue"}]

    def test_title_wins_over_wrong_id(self):
        items = [
            {"id": 2, "title": "Rust 2.0 released", "x": "a"},   # 模型把 id 抄錯了
            {"id": 1, "title": "Why the sky is blue", "x": "b"},
        ]
        m = translate.match_items(self.batch, items)
        self.assertEqual(m[1]["x"], "a")
        self.assertEqual(m[2]["x"], "b")

    def test_fuzzy_title(self):
        items = [{"id": 9, "title": "Rust 2.0 Released!", "x": "a"}]
        self.assertEqual(translate.match_items(self.batch, items)[1]["x"], "a")

    def test_unmatched_is_dropped(self):
        items = [{"id": 1, "title": "Completely different", "x": "a"}]
        self.assertEqual(translate.match_items(self.batch, items), {})


class CleanTitle(unittest.TestCase):
    def test_removes_note(self):
        self.assertEqual(translate.clean_title("北韓假面試事件（無法取得原文內容，僅依標題判斷）"), "北韓假面試事件")

    def test_keeps_other_parentheses(self):
        self.assertEqual(translate.clean_title("機械手臂（robotic arm）"), "機械手臂（robotic arm）")


class Tags(unittest.TestCase):
    def story(self, **kw):
        return {"title": "", "title_zh": "", "summary_zh": "", "domain": "", **kw}

    def test_word_boundary(self):
        self.assertNotIn("AI", render.tags_for(self.story(title="He said hi")))
        self.assertIn("AI", render.tags_for(self.story(title="AI eats software")))

    def test_chinese_substring(self):
        self.assertIn("robotics", render.tags_for(self.story(title_zh="學會摺衣服的機械手臂")))

    def test_no_false_positive(self):
        self.assertEqual(render.tags_for(self.story(title="Kitty 0.50")), [])
        self.assertNotIn("simulation", render.tags_for(self.story(summary_zh="跨平台終端機模擬器 Kitty 發布新版")))
        self.assertIn("simulation", render.tags_for(self.story(summary_zh="用物理模擬訓練機器人")))


class Fallback(unittest.TestCase):
    """用假的 requests.post 驗證：逾時與 503 會換下一個模型，429 等過還是 429 也會換。"""

    def setUp(self):
        self.down = set()  # 每個測試用自己的「失敗過的模型」清單

    def fake_post(self, script):
        import requests
        calls = []

        def post(url, **kw):
            model = url.split("/models/")[1].split(":")[0]
            calls.append(model)
            outcome = script[model]
            if outcome == "timeout":
                raise requests.ReadTimeout("slow")
            if isinstance(outcome, int):
                return unittest.mock.Mock(status_code=outcome, text=f"http {outcome}")
            body = {"candidates": [{"content": {"parts": [{"text": outcome}]}}]}
            return unittest.mock.Mock(status_code=200, json=lambda: body)

        return post, calls

    def run_generate(self, script):
        with unittest.mock.patch.dict("os.environ", {"GEMINI_API_KEY": "x"}), \
             unittest.mock.patch.object(translate, "MODELS", list(script)), \
             unittest.mock.patch.object(translate, "_down", self.down), \
             unittest.mock.patch.object(translate.time, "sleep"):
            post, calls = self.fake_post(script)
            with unittest.mock.patch.object(translate.requests, "post", post):
                return translate._generate("p", translate.STORY_SCHEMA), calls

    def test_timeout_and_503_move_to_next_model(self):
        (data, model), calls = self.run_generate({"m1": "timeout", "m2": 503, "m3": '{"items": []}'})
        self.assertEqual(model, "m3")
        self.assertEqual(calls, ["m1", "m2", "m2", "m3"])  # 503 會重試一次，逾時不會

    def test_failed_model_is_skipped_for_later_batches(self):
        script = {"m1": "timeout", "m2": '{"items": []}'}
        self.run_generate(script)
        _, calls = self.run_generate(script)      # 同一次執行的第二批
        self.assertEqual(calls, ["m2"])           # 不再等 m1 逾時

    def test_unsupported_model_moves_on(self):
        (_, model), calls = self.run_generate({"m1": 400, "m2": '{"items": []}'})
        self.assertEqual((model, calls), ("m2", ["m1", "m2"]))

    def test_all_down_raises_quota(self):
        with self.assertRaises(translate.QuotaExhausted):
            self.run_generate({"m1": 429, "m2": "timeout"})

    def test_bad_json_is_batch_failed(self):
        with self.assertRaises(translate.BatchFailed):
            self.run_generate({"m1": "{not json"})


class ModelList(unittest.TestCase):
    def test_empty_env_falls_back_to_default(self):
        import importlib
        import os
        os.environ["GEMINI_MODELS"] = ""          # Actions 沒設定變數時就是這樣
        try:
            mod = importlib.reload(translate)
            self.assertEqual(mod.MODELS[0], "gemini-3.8-flash")
            self.assertGreater(len(mod.MODELS), 3)
        finally:
            del os.environ["GEMINI_MODELS"]
            importlib.reload(translate)


class Domain(unittest.TestCase):
    def test_strips_www(self):
        self.assertEqual(update.domain_of("https://www.example.com/a?b=c"), "example.com")

    def test_keeps_subdomain(self):
        self.assertEqual(update.domain_of("https://blog.example.com/"), "blog.example.com")


if __name__ == "__main__":
    unittest.main()
