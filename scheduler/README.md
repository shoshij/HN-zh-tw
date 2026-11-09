# On-time scheduler · 準時排程器

**English** · [中文](README.zh-TW.md)

GitHub's own cron is unreliable for small repos: in our logs it fired only 3 to 5 times a day instead of every 2 hours. This 10-minute setup uses a free Google Apps Script timer to trigger the workflow on time. No new account is needed; it runs under the Google account you already have.

## 1. Create a GitHub token

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.
2. Token name: anything, e.g. `HN-zh-tw scheduler`. Expiration: 1 year.
3. Repository access: **Only select repositories** → `HN-zh-tw`.
4. Permissions → **Add permissions** → tick **Actions**. Back in the list, change Actions from Read-only to **Read and write**. Metadata is added automatically.
5. Generate, then copy the token. You won't see it again.

This token can only start workflows in this one repo. It cannot read code, secrets or settings.

## 2. Create the script

1. Open <https://script.google.com> → **New project**.
2. Replace the contents of `Code.gs` with the file `scheduler/Code.gs` from this repo and save (Ctrl+S). If you forked the repo, change `OWNER` to your GitHub username.
3. Project Settings (gear icon) → **Script Properties** → Add property: name `GITHUB_TOKEN`, value = the token from step 1. Save.
4. Back in the editor, select the function `updateHackerNews` and click **Run** once. Google warns that the app is unverified: that is normal, the developer shown is you, and the only permission requested is "connect to an external service". Continue. Then check GitHub → Actions: a new "Update" run should have started.

## 3. Add the timer

Still in Apps Script: Triggers (clock icon in the left bar) → **Add Trigger** → function `updateHackerNews`, event source **Time-driven**, type **Hour timer**, interval **Every 2 hours**. Save.

If the function dropdown keeps spinning, make sure the script is saved, then reload the page.

Done. From now on the workflow starts every 2 hours. GitHub's own schedule stays on as a backup; when both fire, the second one skips itself.

## When the token expires

Each run that fails makes Google email you. Repeat step 1 and paste the new token into Script Properties.
