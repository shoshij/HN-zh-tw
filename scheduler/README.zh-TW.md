# 準時排程器 · On-time scheduler

[English](README.md) · **中文**

GitHub 自己的排程對小 repo 很不可靠：我們的紀錄裡一天只觸發 3 到 5 次，而不是每兩小時一次。這個 10 分鐘的設定用免費的 Google Apps Script 計時器準時觸發流程。不需要新帳號，就用你現有的 Google 帳號。

## 1. 建立 GitHub 權杖

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token。
2. Token name 隨意，例如 `HN-zh-tw scheduler`。Expiration 選 1 年。
3. Repository access 選 **Only select repositories** → `HN-zh-tw`。
4. Permissions → **Add permissions** → 勾 **Actions**。回到清單，把 Actions 從 Read-only 改成 **Read and write**。Metadata 會自動加上。
5. 產生後複製權杖，之後看不到第二次。

這把權杖只能啟動這個 repo 的流程，不能讀程式碼、secrets 或設定。

## 2. 建立指令碼

1. 開 <https://script.google.com> → **新專案**。
2. 把 `Code.gs` 的內容換成這個 repo 裡的 `scheduler/Code.gs`，存檔（Ctrl+S）。如果你是 fork 的，把 `OWNER` 改成你的 GitHub 帳號。
3. 專案設定（齒輪）→ **指令碼屬性** → 新增：名稱 `GITHUB_TOKEN`，值貼上第 1 步的權杖。儲存。
4. 回到編輯器，選函式 `updateHackerNews`，按一次 **執行**。Google 會警告這個應用程式未經驗證，這是正常的：畫面上的開發者就是你自己，要求的權限只有「連線到外部服務」。按繼續。然後到 GitHub → Actions 看，應該多了一個剛開始的 Update 執行。

## 3. 加上計時器

一樣在 Apps Script 裡：觸發條件（左側時鐘圖示）→ **新增觸發條件** → 函式 `updateHackerNews`，事件來源 **時間驅動**，類型 **小時計時器**，間隔 **每 2 小時**。儲存。

如果函式下拉選單一直轉圈，先確認指令碼已存檔，再重新整理頁面。

完成。之後流程每兩小時啟動一次。GitHub 自己的排程保留當備援，兩邊撞在一起時後到的會自己跳過。

## 權杖過期時

每次執行失敗 Google 都會寄信給你。重做第 1 步，把新權杖貼進指令碼屬性即可。
