# On-time scheduler · 準時排程器

[English](README.md#english) · **中文**

## English

See [README.md](README.md#english).

<!--

GitHub's own cron is unreliable for small repos: in our logs it fired only 3 to 5 times a day instead of every 2 hours. This 10-minute setup uses a free Google Apps Script timer to trigger the workflow on time. No new account is needed; it runs under the Google account you already have.

### 1. Create a GitHub token

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.
2. Repository access: **Only select repositories** → `HN-zh-tw`.
3. Permissions → Repository permissions → **Actions: Read and write**. Leave everything else at No access.
4. Expiration: 1 year. Generate, then copy the token. You won't see it again.

This token can only start workflows in this one repo. It cannot read code, secrets or settings.

### 2. Create the script

1. Open <https://script.google.com> → **New project**.
2. Replace the contents of `Code.gs` with the file `scheduler/Code.gs` from this repo. If you forked the repo, change `OWNER` to your GitHub username.
3. Project Settings (gear icon) → **Script Properties** → Add property: name `GITHUB_TOKEN`, value = the token from step 1. Save.
4. Back in the editor, select the function `updateHackerNews` and click **Run** once. Google asks you to authorise the script to connect to an external service; accept. Then check GitHub → Actions: a new "Update" run should have started.

### 3. Add the timer

Triggers (clock icon) → **Add Trigger** → function `updateHackerNews`, event source **Time-driven**, type **Hour timer**, interval **Every 2 hours**. Save.

Done. From now on the workflow starts every 2 hours. GitHub's own schedule stays on as a backup; when both fire, the second one skips itself.

### When the token expires

Each run that fails makes Google email you. Repeat step 1 and paste the new token into Script Properties.

-->

## 中文

GitHub 自己的排程對小 repo 很不可靠：我們的紀錄裡一天只觸發 3 到 5 次，而不是每兩小時一次。這個 10 分鐘的設定用免費的 Google Apps Script 計時器準時觸發流程。不需要新帳號，就用你現有的 Google 帳號。

### 1. 建立 GitHub 權杖

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token。
2. Repository access 選 **Only select repositories** → `HN-zh-tw`。
3. Permissions → Repository permissions → **Actions: Read and write**，其他全部維持 No access。
4. Expiration 選 1 年。產生後複製權杖，之後看不到第二次。

這把權杖只能啟動這個 repo 的流程，不能讀程式碼、secrets 或設定。

### 2. 建立指令碼

1. 開 <https://script.google.com> → **新專案**。
2. 把 `Code.gs` 的內容換成這個 repo 裡的 `scheduler/Code.gs`。如果你是 fork 的，把 `OWNER` 改成你的 GitHub 帳號。
3. 專案設定（齒輪）→ **指令碼屬性** → 新增：名稱 `GITHUB_TOKEN`，值貼上第 1 步的權杖。儲存。
4. 回到編輯器，選函式 `updateHackerNews`，按一次 **執行**。Google 會要求你授權這個指令碼連到外部服務，按同意。然後到 GitHub → Actions 看，應該多了一個剛開始的 Update 執行。

### 3. 加上計時器

觸發條件（時鐘圖示）→ **新增觸發條件** → 函式 `updateHackerNews`，事件來源 **時間驅動**，類型 **小時計時器**，間隔 **每 2 小時**。儲存。

完成。之後流程每兩小時啟動一次。GitHub 自己的排程保留當備援，兩邊撞在一起時後到的會自己跳過。

### 權杖過期時

每次執行失敗 Google 都會寄信給你。重做第 1 步，把新權杖貼進指令碼屬性即可。
