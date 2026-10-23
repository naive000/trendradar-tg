# trendradar-tg

每天把科技榜單（Hacker News、GitHub Trending、Product Hunt、V2EX、少數派、掘金、IT 之家、Solidot）整理成日報發到 Telegram：頻道一則索引訊息（各榜前 3 名）＋「閱讀全文」按鈕 → Telegraph 全文頁（每榜前 15 名，中文標題簡轉繁）。

靈感來自 [sansan0/TrendRadar](https://github.com/sansan0/TrendRadar)（GPL-3.0）；本專案是獨立重寫的薄殼，未使用其程式碼。資料來自 [NewsNow](https://github.com/ourongxing/newsnow) 的公共 API。

- 流程：GitHub Actions 每天 08:37（台北）抓各榜單快照 → OpenCC `s2twp` 轉標題 → 建 Telegraph 頁 → 發索引訊息 → 成功才寫回 `state.json`。21:07 另有補發排程，今天已發過則無動作。無 LLM。
- 榜單清單在 `trendradar_tg.py` 的 `SOURCES`，要增減直接改。不含政治／娛樂熱搜。
- 少於 3 個榜單抓成功就整份不發（避免殘缺日報），Actions 會紅燈。
- 版權：本頁僅列標題與連結，點入閱讀原站。

## Secrets（Settings → Secrets → Actions）

| 名稱 | 說明 |
|---|---|
| `TG_BOT_TOKEN` | 專用 bot 的 token |
| `TG_CHAT_ID` | 頻道 id（`-100…`；bot 須是管理員且可發佈） |
| `TELEGRAPH_TOKEN` | Telegraph 帳號 token（`--create-telegraph-account` 產生） |

## 本機

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest -q
.venv/bin/python trendradar_tg.py --dry-run     # 只印訊息，不發送、不寫 state
```

Actions 的 `daily` 可手動觸發：`dry_run=true` 試跑（不需 secrets）、`force=true` 今天已發過也重發。

## 已知限制

- NewsNow 是第三方公共服務，掛了或封鎖時當天不發；長期穩定可自架 NewsNow 並改 `NEWSNOW_API`。
- NewsNow 只給當下快照，日報代表「抓取當下的榜單」，不是全天累積。
- 英文標題（HN、Product Hunt、GitHub）保持原文，不翻譯。
- Telegraph 頁建了刪不掉，別亂建測試頁；發送失敗重試會重用 `state.json` 的 `pages`。
