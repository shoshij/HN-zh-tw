# Hacker News in Traditional Chinese · Hacker News 繁體中文版

**English** · [中文](README.zh-TW.md)

- Site: <https://shoshij.github.io/HN-zh-tw/>
- RSS: <https://shoshij.github.io/HN-zh-tw/feed.xml>

> AI-generated translations and summaries may contain errors. Please refer to the original.

The Hacker News front page in Taiwanese Traditional Chinese, updated every two hours. Each story has a Chinese title, Chinese and English summaries, and summaries of the top comments.

## How it works

Every two hours, GitHub Actions runs two scripts:

1. `update.py` fetches the top 30 stories, asks Gemini to translate and summarise the new ones, and saves the results to `data.json`.
2. `build.py` turns `data.json` into the web page and RSS feed, then deploys them to GitHub Pages.

A story is translated once, and sent again only if HN edits its title or link. Comment summaries are added once, about 6 hours after a story reaches the front page.

GitHub's own scheduler often fires hours late. For updates that are actually on time, set up the free Google Apps Script timer in [scheduler/README.md](scheduler/README.md); it takes about 10 minutes. The workflow skips a run when the last update is under 100 minutes old, so the two schedulers never double up.

## Setup

1. Fork this repo. It must be public for free GitHub Pages.
2. Get a free Gemini API key at <https://aistudio.google.com/apikey>.
3. Add it as a repository secret named `GEMINI_API_KEY`.
4. In Settings → Pages, set Source to **GitHub Actions**.
5. In Actions → Update, click **Run workflow**. After that it runs every two hours.
6. Optional but recommended: the on-time scheduler in [scheduler/README.md](scheduler/README.md).

## Run locally

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your-key
python update.py --limit 3 --data /tmp/test.json   # top 3 stories, into a scratch file
python build.py --data /tmp/test.json              # write site/index.html and site/feed.xml
python -m unittest                                 # run the tests
```

## Customize

- **Models:** the `GEMINI_MODELS` repository variable.
- **Topic tags:** `TAGS` in `hnzh/render.py`.
- **Translation style:** `SYSTEM` in `hnzh/translate.py`.
- **Look:** the CSS variables in `hnzh/templates/index.html`.

## Cost

Free. It uses the Gemini free tier and GitHub's free Actions and Pages. If the Gemini quota runs out, new stories stay in English until a later run. If translation fails six runs in a row, the workflow turns red and GitHub emails you.
