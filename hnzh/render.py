"""第二步：把 data.json 變成靜態網頁（site/index.html）與 RSS（site/feed.xml）。

這一步完全不需要網路，也不需要任何金鑰。
"""
from __future__ import annotations

import html
import os
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import store

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"
WINDOW = timedelta(hours=48)  # 頁面與 RSS 顯示過去 48 小時內上過首頁的文章
TAIPEI = ZoneInfo("Asia/Taipei")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")  # GitHub Actions 會傳入 Pages 網址，給 RSS 用
REPO_URL = os.environ.get("REPO_URL", "").rstrip("/")  # 頁尾的「原始碼」連結

# 預設主題標籤。每個標籤是一組關鍵字：英文用 \b 單字邊界（避免 "ai" 命中 "said"），中文用子字串。
# 標籤在這裡算好、寫進網頁（data-tags）與 RSS（<category>），瀏覽器端的 JavaScript 只負責切換顯示。
TAGS: dict[str, list[str]] = {
    "robotics": [r"\brobot", "機器人", "機械手臂", r"\bhumanoid", r"\bdrone", "無人機", r"\bros\b",
                 r"\bmanipulat", r"\bactuator", "致動器"],
    "hardware": [r"\bhardware\b", "硬體", r"\bchip", "晶片", "半導體", r"\bsemiconductor", r"\bfpga\b", r"\bpcb\b",
                 r"\bgpu", r"\bcpu", r"\bsilicon", r"\bsensor", "感測器", r"\bfirmware", "韌體", r"\barduino",
                 r"\braspberry pi", r"\bmicrocontroller", "微控制器"],
    "AI": [r"\bai\b", "人工智慧", r"\bllm", "語言模型", r"\bmachine learning", "機器學習", r"\bneural", "神經網路",
           r"\bgpt", r"\bclaude", r"\bgemini", r"\bopenai", r"\banthropic", r"\bdeep learning", "深度學習",
           r"\btransformer", r"\bagent", r"\bdiffusion"],
    "simulation": [r"\bsimulat", "模擬", r"\bphysics engine", "物理引擎", r"\bdigital twin", "數位孿生",
                   r"\bgazebo", r"\bmujoco", r"\bisaac", r"\bemulat"],
}
_COMPILED = {tag: [re.compile(p, re.IGNORECASE) for p in pats] for tag, pats in TAGS.items()}


def tags_for(story: dict) -> list[str]:
    """看標題、中文標題、摘要、網域，回傳命中的標籤。"""
    text = " ".join(filter(None, [story.get("title"), story.get("title_zh"), story.get("summary_zh"), story.get("domain")]))
    return [tag for tag, pats in _COMPILED.items() if any(p.search(text) for p in pats)]


def select_stories(data: dict) -> tuple[list[dict], list[dict]]:
    """回傳 (目前首頁的文章依排名, 其他 48 小時內的文章依出現時間新到舊)，並附上 tags。"""
    cutoff = store.now() - WINDOW
    recent = [s for s in data["stories"].values() if store.from_iso(s["first_seen"]) >= cutoff]
    for s in recent:
        s["tags"] = tags_for(s)
    front = sorted((s for s in recent if s["on_front_page"]), key=lambda s: s["rank"])
    rest = sorted((s for s in recent if not s["on_front_page"]), key=lambda s: s["first_seen"], reverse=True)
    return front, rest


def taipei(iso: str) -> str:
    """ISO 時間字串 → 台北時間「11/04 14:05」。"""
    return store.from_iso(iso).astimezone(TAIPEI).strftime("%m/%d %H:%M")


def render_html(data: dict, front: list[dict], rest: list[dict]) -> str:
    env = Environment(
        loader=FileSystemLoader(ROOT / "hnzh" / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["taipei"] = taipei
    return env.get_template("index.html").render(
        front=front, rest=rest, tags=list(TAGS), updated_at=data["updated_at"],
        site_url=SITE_URL, repo_url=REPO_URL,
    )


def render_rss(data: dict, stories: list[dict]) -> bytes:
    rss = ET.Element("rss", {"version": "2.0", "xmlns:atom": "http://www.w3.org/2005/Atom"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Hacker News 繁體中文版"
    ET.SubElement(ch, "link").text = f"{SITE_URL}/" if SITE_URL else "https://news.ycombinator.com/"
    ET.SubElement(ch, "description").text = (
        "Hacker News 首頁文章的繁體中文翻譯與摘要，每小時更新。翻譯與摘要由 AI 產生，可能有誤，請以原文為準。"
    )
    ET.SubElement(ch, "language").text = "zh-TW"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(store.from_iso(data["updated_at"]))
    ET.SubElement(ch, "generator").text = "HN-zh-tw"
    if SITE_URL:
        ET.SubElement(ch, "atom:link", {"href": f"{SITE_URL}/feed.xml", "rel": "self", "type": "application/rss+xml"})

    for s in stories:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = f"{s['title_zh']} | {s['title']}" if s["title_zh"] else s["title"]
        ET.SubElement(it, "link").text = s["url"]
        ET.SubElement(it, "guid").text = s["hn_url"]
        ET.SubElement(it, "comments").text = s["hn_url"]
        ET.SubElement(it, "pubDate").text = format_datetime(store.from_iso(s["first_seen"]))
        for tag in s.get("tags", []):
            ET.SubElement(it, "category").text = tag

        e = html.escape  # description 裡放 HTML，所以每個字串都要跳脫
        desc = [f"<p>{e(s['summary_zh'] or '（尚未翻譯）')}</p>"]
        desc.append(
            f"<p>▲ {s['score']} 分 · {s['comments_count']} 則評論 · {e(s['domain'])} · "
            f"<a href=\"{e(s['hn_url'])}\">HN 討論</a></p>"
        )
        if s["comments"]:
            desc.append("<p>評論摘要：</p><ol>")
            desc += [f"<li>{e(c['by'])}：{e(c['summary_zh'])} <a href=\"{e(c['url'])}\">↗</a></li>" for c in s["comments"]]
            desc.append("</ol>")
        ET.SubElement(it, "description").text = "".join(desc)

    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def build() -> None:
    data = store.load()
    if not data["updated_at"]:
        raise SystemExit("data.json 是空的，請先執行 python update.py")
    front, rest = select_stories(data)
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(render_html(data, front, rest), encoding="utf-8")
    (SITE_DIR / "feed.xml").write_bytes(render_rss(data, front + rest))
    print(f"site/index.html：首頁 {len(front)} 篇 + 過去 48 小時 {len(rest)} 篇；site/feed.xml 已產生")
