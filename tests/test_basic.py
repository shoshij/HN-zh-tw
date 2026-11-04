"""純邏輯的單元測試，不碰網路也不碰 Gemini。執行：python -m unittest -v"""
import unittest

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
