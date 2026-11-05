# Hacker News in Traditional Chinese · Hacker News 繁體中文版

**[English](#english)** · **[中文](#中文)**

- Site · 網頁：<https://shoshij.github.io/HN-zh-tw/>
- RSS：<https://shoshij.github.io/HN-zh-tw/feed.xml>

> Translations and summaries are AI-generated and may contain errors. Please refer to the original article.
>
> 翻譯與摘要由 AI 產生，可能有誤，請以原文為準。

---

## English

The top 30 stories on the [Hacker News](https://news.ycombinator.com/) front page, translated into Taiwanese Traditional Chinese. Each story gets a Chinese title, a 2–3 sentence Chinese summary, a sentence-aligned English summary for side-by-side reading, and summaries of the top comments. It updates every hour and is published on GitHub Pages.

### How it works

GitHub Actions runs two steps every hour:

```
update.py                                      build.py
─────────────────────────────────────────      ──────────────────────────────
Fetch the top 30 from the HN API               Read data.json
  → fetch each article, extract the text         → write site/index.html
  → ask Gemini for the title and summaries       → write site/feed.xml (RSS 2.0)
  → save to data.json, commit it to the repo     → deploy to GitHub Pages
```

The rules:

- **No repeat translation.** data.json remembers every translated story, so each hour only new ones are sent. A story is re-translated only if HN moderators change its title or link.
- **Bilingual summaries.** The English summary matches the Chinese one sentence by sentence, so you can read them side by side.
- **Comment summaries.** Six hours after a story first reaches the front page, the top 3 comments are each summarised in 1–2 sentences. This happens once per story.
- **Full-text translation.** The button beside each title opens the article through Google Translate in Traditional Chinese. It is only a link: Google translates when the reader clicks, so it uses no Gemini quota.
- **What is shown.** The page and the RSS feed show stories that were on the front page in the last 48 hours. data.json keeps 7 days.
- **Failures don't break the site.** When Gemini is out of quota, busy, or returns a bad response, the script prints a warning (a yellow annotation on the Actions run). New stories appear with their English title and are translated the next hour. The site and data.json still update.
- **Bounded runs.** Each run sends at most 6 batches of 8 stories and spends at most 15 minutes on translation, so a slow hour never blocks the next one.

### First-time setup

1. **Create the repo.** Fork this repo, or push these files to your own **public** repo. GitHub Pages on the free plan only works for public repos.
2. **Get a Gemini API key (free).** Create one at <https://aistudio.google.com/apikey>.
3. **Add it as a secret.** In the repo: Settings → Secrets and variables → Actions → New repository secret. Name it `GEMINI_API_KEY` and paste the key as the value.
4. **Enable GitHub Pages.** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
5. **Run it once by hand.** Actions → Update → Run workflow. The site URL appears on the workflow page when it finishes. After that it runs every hour.

### Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export GEMINI_API_KEY=your-key
python update.py --limit 3   # only the top 3 stories, for testing
python build.py              # writes site/index.html and site/feed.xml
open site/index.html         # macOS; use xdg-open on Linux

python -m unittest -v        # unit tests (no network)
```

`python update.py --skip-translate` only refreshes scores and comment counts without calling Gemini.

### Files

| File | Purpose |
|---|---|
| `update.py` | Step 1: fetch, translate, write data.json |
| `build.py` | Step 2: data.json → web page and RSS |
| `hnzh/hn.py` | Hacker News API wrapper (front page, stories, comments) |
| `hnzh/extract.py` | Fetches articles and extracts the main text (trafilatura) |
| `hnzh/translate.py` | Gemini calls: prompts, JSON schema, batching, model fallback, result checking |
| `hnzh/store.py` | Reads, writes and prunes data.json |
| `hnzh/render.py` | Builds the HTML and RSS; topic tag keywords live here too |
| `hnzh/templates/index.html` | Page template (CSS and the filtering JavaScript included) |
| `tests/` | Unit tests |
| `data.json` | All translations (committed automatically by Actions) |
| `.github/workflows/update.yml` | Hourly schedule |
| `.github/workflows/test.yml` | Runs the tests on push |

### Customize

- **Models.** By default it tries, in order: `gemini-3.8-flash` → `3.6-flash` → `3.5-flash-lite` → `flash-lite-latest` → `3-flash-preview` → `3.7-flash` → `3.1-flash-lite`. Popular models often return 503, so when one fails the next is tried. To change the list, add a repository **variable** named `GEMINI_MODELS` (comma-separated) under Settings → Secrets and variables → Actions → Variables.
- **Topic tags.** `TAGS` in `hnzh/render.py`. Each tag is a list of keywords: English ones match on word boundaries (`\b`), Chinese ones match as substrings. Tags are computed when the page is built and also go into RSS `<category>`.
- **Timing and counts.** `FRONT_PAGE`, `COMMENTS_AFTER`, `COMMENTS_WINDOW`, `TOP_COMMENTS`, `MAX_BATCHES` and `TIME_BUDGET` at the top of `update.py`; `WINDOW` in `hnzh/render.py`.
- **Translation style.** The `SYSTEM` prompt in `hnzh/translate.py`.
- **Page style.** The CSS variables at the top of `hnzh/templates/index.html` (background, text, accent colour, fonts).

### Costs and limits

- **Gemini free tier.** Each model has a daily request limit. The actual numbers are shown in AI Studio and change over time. The script packs up to 8 stories into one request, so a normal day uses roughly 30–70 requests.
- **Google uses free-tier content to improve its products.** Only public HN articles and comments are sent.
- **GitHub Actions.** Free for public repos. GitHub disables a schedule after 60 days without repository activity; re-enable it on the Actions page.
- **Articles that can't be fetched.** PDFs, login-walled pages and JavaScript-only sites (YouTube, X and similar) yield no text. Their summary is based on the title alone and says so.

---

## 中文

把 [Hacker News](https://news.ycombinator.com/) 首頁前 30 篇文章翻成台灣繁體中文。每篇有中文標題、2～3 句中文摘要、一句對一句的英文摘要（方便中英對照），以及熱門評論的摘要。每小時自動更新，發佈在 GitHub Pages。

### 運作方式

GitHub Actions 每小時跑一次，分成兩步：

```
update.py                                   build.py
──────────────────────────────────────      ─────────────────────────────
HN API 抓首頁 30 篇                          讀 data.json
  → 抓原文網頁、抽出正文                        → 產生 site/index.html
  → 丟給 Gemini 翻標題、寫摘要                  → 產生 site/feed.xml（RSS 2.0）
  → 存進 data.json，commit 回 repo              → 部署到 GitHub Pages
```

幾個規則：

- **不重複翻譯**：data.json 記得每篇翻過的文章，每小時只翻新出現的。HN 版主改了標題或連結才會重翻。
- **中英對照摘要**：英文摘要和中文摘要一句對一句，可以上下對照著讀。
- **評論摘要**：文章第一次上首頁滿 6 小時後，抓前 3 則評論各做 1～2 句摘要，只做一次。
- **全文中譯**：每篇標題旁的按鈕會用 Google 翻譯開啟原文的繁體中文版。這只是連結，翻譯由 Google 在讀者點擊時即時進行，不用 Gemini 額度。
- **顯示範圍**：頁面與 RSS 顯示過去 48 小時內上過首頁的文章；data.json 保留 7 天。
- **出錯不會壞**：Gemini 額度用完、模型太忙或回應壞掉時，程式只印警告（Actions 上會看到黃色標記），
  新文章先用英文標題顯示，下一小時再補翻。網站與 data.json 照常更新。
- **每次執行有上限**：一次最多送 6 批、每批 8 篇，翻譯最多花 15 分鐘，所以某個小時跑得慢也不會卡住下一小時。

### 第一次設定

1. **建立 repo**：Fork 這個 repo，或把這些檔案 push 到你自己的 **public** repo（GitHub Pages 免費方案只支援 public repo）。
2. **取得 Gemini API key（免費）**：到 <https://aistudio.google.com/apikey> 建一把。
3. **放進 Secrets**：repo 的 Settings → Secrets and variables → Actions → New repository secret，
   名稱填 `GEMINI_API_KEY`，值貼上金鑰。
4. **啟用 GitHub Pages**：Settings → Pages → Build and deployment → Source 選 **GitHub Actions**。
5. **手動跑第一次**：Actions → Update → Run workflow。跑完後網址會顯示在 workflow 頁面。之後每小時自動跑。

### 本機執行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export GEMINI_API_KEY=你的金鑰
python update.py --limit 3   # 只處理首頁前 3 篇，測試用
python build.py              # 產生 site/index.html 與 site/feed.xml
open site/index.html         # macOS；Linux 用 xdg-open

python -m unittest -v        # 單元測試（不碰網路）
```

`python update.py --skip-translate` 只更新分數與評論數，不呼叫 Gemini。

### 檔案說明

| 檔案 | 用途 |
|---|---|
| `update.py` | 第一步：抓資料、翻譯、寫 data.json |
| `build.py` | 第二步：data.json → 網頁與 RSS |
| `hnzh/hn.py` | Hacker News API 封裝（首頁、文章、評論） |
| `hnzh/extract.py` | 抓原文網頁、抽正文（trafilatura） |
| `hnzh/translate.py` | 呼叫 Gemini：提示詞、JSON schema、批次、換模型、核對結果 |
| `hnzh/store.py` | data.json 讀寫與清理 |
| `hnzh/render.py` | 產生 HTML 與 RSS；主題標籤的關鍵字也在這裡 |
| `hnzh/templates/index.html` | 網頁模板（含 CSS 與篩選用的 JavaScript） |
| `tests/` | 單元測試 |
| `data.json` | 所有翻譯結果（由 Actions 自動 commit） |
| `.github/workflows/update.yml` | 每小時排程 |
| `.github/workflows/test.yml` | push 時跑測試 |

### 自訂

- **換模型**：預設依序嘗試 `gemini-3.8-flash` → `3.6-flash` → `3.5-flash-lite` → `flash-lite-latest` → `3-flash-preview` → `3.7-flash` → `3.1-flash-lite`
  （熱門模型常回 503，所以一個不行就換下一個）。要改就到 Settings → Secrets and variables → Actions → **Variables**
  新增 `GEMINI_MODELS`，逗號分隔。
- **主題標籤**：`hnzh/render.py` 的 `TAGS`。每個標籤是一組關鍵字，英文用 `\b` 單字邊界比對，中文用子字串。
  標籤在產生網頁時算好，RSS 也會放進 `<category>`。
- **時間與篇數**：`update.py` 開頭的 `FRONT_PAGE`、`COMMENTS_AFTER`、`COMMENTS_WINDOW`、`TOP_COMMENTS`、`MAX_BATCHES`、`TIME_BUDGET`；`hnzh/render.py` 的 `WINDOW`。
- **翻譯風格**：`hnzh/translate.py` 的 `SYSTEM` 提示詞。
- **網頁樣式**：`hnzh/templates/index.html` 開頭的 CSS 變數（底色、文字、強調色、字型）。

### 費用與限制

- **Gemini 免費額度**：每個模型每天的請求數有上限（實際數字在 AI Studio 看得到，會變動）。
  程式一次把最多 8 篇塞進同一個請求，正常一天只用 30～70 個請求。
- **Google 會用免費額度的內容改進產品**。這裡送出的只有 HN 的公開文章與評論。
- **GitHub Actions**：public repo 免費。排程若 60 天都沒有任何活動會被 GitHub 自動停用，到 Actions 頁面重新啟用即可。
- **抓不到原文**：PDF、需要登入、純 JavaScript 的網站（YouTube、X 等）抓不到正文，摘要會註明「僅依標題判斷」。
