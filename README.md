# 盤前暖男台股財報（每日盤前政經與題材選股 Gmail 電子報機器人）

結合最新版 `google-genai`（Google Search Grounding 即時連網查證）、英倫復古孔雀藍綠 × 香檳燙金排版、動態翻牌數據卡 GIF（美股收盤＋台股重點關注三支股票）與 3D 互動翻牌網頁版，於每個交易日台灣時間早上 **08:30** 自動透過 Gmail Bcc 密件副本寄送給所有訂閱者。

## 本機執行方式

```bash
pip install -r requirements.txt

# 預覽模式（僅產生 preview_report.html 與 docs/index.html，不寄信）
python main.py --dry-run

# 單人測試模式（僅寄送給 SENDER_EMAIL 本人）
python main.py --test-self

# 正式群發模式（以 Bcc 寄給 RECIPIENT_EMAILS 所有人）
python main.py
```

## GitHub Actions 必要 Secrets 設定

於 Repository 的 **Settings → Secrets and variables → Actions** 新增：

| Secret 名稱 | 說明 |
| --- | --- |
| `GEMINI_API_KEY` | Google AI Studio 取得的 Gemini API 金鑰 |
| `SENDER_EMAIL` | 寄件者 Gmail 信箱 |
| `GMAIL_APP_PASSWORD` | Gmail 16 位應用程式密碼 |
| `RECIPIENT_EMAILS` | 訂閱者 Email 清單（逗號分隔，寄送時自動使用 Bcc） |
| `MARKET_FOCUS` | 市場關注焦點（預設 `台股與美股連動`） |
| `INTERACTIVE_URL` | （選填）GitHub Pages 網址，設定後信中會附「開啟 3D 互動翻牌網頁版」按鈕 |
