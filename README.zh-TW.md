# Hacker News 繁體中文版 · Hacker News in Traditional Chinese

[English](README.md) · **中文**

- 網頁：<https://shoshij.github.io/HN-zh-tw/>
- RSS：<https://shoshij.github.io/HN-zh-tw/feed.xml>

> 翻譯與摘要由 AI 產生，可能有誤，請以原文為準。

Hacker News 首頁的台灣繁體中文版，每小時更新。每篇文章有中文標題、中英文摘要，以及熱門評論的摘要。

## 運作方式

GitHub Actions 每小時執行兩支程式：

1. `update.py` 抓首頁前 30 篇，請 Gemini 翻譯並摘要新出現的文章，結果存進 `data.json`。
2. `build.py` 把 `data.json` 變成網頁和 RSS，再部署到 GitHub Pages。

翻過的文章不會再送一次。評論摘要在文章上首頁滿 6 小時後補上，只做一次。

## 設定

1. Fork 這個 repo。必須是 public，GitHub Pages 才免費。
2. 到 <https://aistudio.google.com/apikey> 取得免費的 Gemini API key。
3. 把它加成 repo 的 secret，名稱是 `GEMINI_API_KEY`。
4. 在 Settings → Pages 把 Source 設成 **GitHub Actions**。
5. 在 Actions → Update 按 **Run workflow**。之後每小時自動執行。

## 本機執行

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=你的金鑰
python update.py --limit 3   # 翻譯首頁前 3 篇
python build.py              # 產生 site/index.html 和 site/feed.xml
python -m unittest           # 執行測試
```

## 自訂

- **模型**：repo 變數 `GEMINI_MODELS`。
- **主題標籤**：`hnzh/render.py` 的 `TAGS`。
- **翻譯風格**：`hnzh/translate.py` 的 `SYSTEM`。
- **外觀**：`hnzh/templates/index.html` 的 CSS 變數。

## 費用

免費。使用 Gemini 免費額度，以及 GitHub 免費的 Actions 和 Pages。額度用完時，新文章先以英文顯示，下一小時再翻。
