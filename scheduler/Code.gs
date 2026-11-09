// 每兩小時叫 GitHub 跑一次更新。
// 貼到 Google Apps Script（script.google.com）的專案裡，照 README 設定觸發器與權杖。

const OWNER = 'shoshij';
const REPO = 'HN-zh-tw';
const WORKFLOW = 'update.yml';
const BRANCH = 'main';

function updateHackerNews() {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    throw new Error('還沒設定權杖：專案設定 → 指令碼屬性 → 新增 GITHUB_TOKEN');
  }
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    // source: scheduler 讓 workflow 套用「剛更新過就跳過」的規則，不會和 GitHub 自己的排程重複
    payload: JSON.stringify({ ref: BRANCH, inputs: { source: 'scheduler' } }),
    muteHttpExceptions: true,
  });
  const code = response.getResponseCode();
  if (code !== 204) {
    // 丟出錯誤，Google 才會寄信通知你（例如權杖過期時會收到 401）
    throw new Error(`GitHub 回應 ${code}：${response.getContentText()}`);
  }
  console.log('已觸發更新');
}
