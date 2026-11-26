"""每日盤前政經與題材選股 Gmail 電子報機器人。

使用新版 google-genai 套件搭配 Google Search Grounding 蒐集過去 24 小時
美股收盤、台股盤前焦點與全球重大政經事件，生成幽默且具專業度的早報，
並透過 Gmail SMTP (Port 465 SSL) 以 Bcc 密件副本寄送給所有訂閱者。
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
import html
import io
import math
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
import markdown
from PIL import Image, ImageDraw, ImageFont

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
DEFAULT_MARKET_FOCUS = "台股與美股連動"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

WEEKDAY_ZH = ("一", "二", "三", "四", "五", "六", "日")

PLACEHOLDER_VALUES = {
    "your_gemini_api_key_here",
    "your_email@gmail.com",
    "xxxx_xxxx_xxxx_xxxx",
    "me@gmail.com,friend1@gmail.com",
}


def configure_console_encoding() -> None:
    """確保在 Windows 終端機輸出中文與 Emoji 不會發生編碼錯誤。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def get_taipei_now() -> datetime:
    """取得目前台灣時間（Asia/Taipei, UTC+8）。"""
    try:
        tz = ZoneInfo("Asia/Taipei")
    except ZoneInfoNotFoundError:
        tz = timezone(timedelta(hours=8))
    return datetime.now(tz)


def load_config(dry_run: bool = False, test_self: bool = False) -> dict[str, Any]:
    """讀取並驗證環境變數設定。"""
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_path)

    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    sender_email = os.getenv("SENDER_EMAIL", "").strip()
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    raw_recipients = os.getenv("RECIPIENT_EMAILS", "").strip()
    market_focus = os.getenv("MARKET_FOCUS", "").strip() or DEFAULT_MARKET_FOCUS
    gemini_model = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    interactive_url = os.getenv("INTERACTIVE_URL", "").strip()

    recipients = [
        email.strip()
        for email in raw_recipients.split(",")
        if email.strip()
    ]

    missing_or_invalid: list[str] = []
    if not gemini_api_key or gemini_api_key in PLACEHOLDER_VALUES:
        missing_or_invalid.append("GEMINI_API_KEY")

    if not dry_run:
        if not sender_email or sender_email in PLACEHOLDER_VALUES:
            missing_or_invalid.append("SENDER_EMAIL")
        if not gmail_app_password or gmail_app_password in PLACEHOLDER_VALUES:
            missing_or_invalid.append("GMAIL_APP_PASSWORD")
        if not test_self and (not recipients or raw_recipients in PLACEHOLDER_VALUES):
            missing_or_invalid.append("RECIPIENT_EMAILS")

    if test_self and sender_email:
        recipients = [sender_email]

    if missing_or_invalid:
        vars_str = ", ".join(missing_or_invalid)
        raise ValueError(
            f"環境變數尚未設定完成或仍為預設範例值：{vars_str}\n"
            "請先編輯 .env 檔案（或在 GitHub Repository Secrets 設定）後再執行。"
        )

    return {
        "gemini_api_key": gemini_api_key,
        "sender_email": sender_email,
        "gmail_app_password": gmail_app_password,
        "recipients": recipients,
        "market_focus": market_focus,
        "gemini_model": gemini_model,
        "interactive_url": interactive_url,
    }


def build_research_prompt(now_tw: datetime, market_focus: str) -> str:
    """建立第一階段：純事實連網查證提示詞（確保觸發 Google Search Grounding）。"""
    date_str = now_tw.strftime("%Y/%m/%d")
    yesterday_str = (now_tw - timedelta(days=1)).strftime("%Y/%m/%d")
    weekday_str = f"週{WEEKDAY_ZH[now_tw.weekday()]}"

    return f"""請立即使用 Google Search 查詢截至台灣時間 {date_str}（{weekday_str}）過去 24 小時（約 {yesterday_str} 至 {date_str}）的最新真實金融與政經新聞，並整理成精準的事實摘要（繁體中文）。

關注主軸：{market_focus}

請務必針對以下三個面向逐一搜尋並列出查到的具體數據、日期與事件來源：
1. **最新美股收盤表現**（請查最新一個美股交易日的實際收盤價與漲跌幅；若遇美國國定假日休市請明確標註休市日期與最近收盤日）：
   - 費城半導體指數（PHLX Semiconductor Index / SOX）
   - 輝達（NVIDIA / NVDA）
   - 台積電 ADR（TSMC ADR / TSM）
   - 美股三大指數（道瓊、S&P 500、那斯達克）收盤重點與驅動原因
2. **台股盤前焦點（過去 24 小時）**：
   - 台指期夜盤表現、台股最新收盤或外資動向
   - 台灣科技股、AI 供應鏈、半導體、航運/重電/生技等重要公司財報、法說會、營收或題材新聞
3. **全球重大政治與總經事件（過去 24 小時）**：
   - 央行利率動態與官員談話（Fed 聯準會、ECB、日本央行、台灣央行等）
   - 關稅政策、美中貿易/科技管制、地緣政治衝突
   - 各國重要產業政策、補貼或基礎建設消息
4. **今日重點關注 3 檔台股最新收盤行情**：
   - 根據今日最具即時性的新聞與題材，選出 3 檔頭號指標台股（含正確 4 碼股票代號與中文名稱，例如 `2330 台積電`、`2317 鴻海`、`2382 廣達` 等當日主角），**務必逐一用 Google Search 查出它們最近一個台股交易日的真實收盤價（新台幣）與漲跌幅（%）**。

【嚴格事實規範】
- 只能寫出透過 Google Search 查證到的真實數據與事件，必須標註資料日期。
- 若任何一項查無最新確認數字，請直接寫「截稿前查無最新確認數據」，嚴禁猜測或編造數字。
"""


def build_strategist_prompts(
    now_tw: datetime,
    market_focus: str,
    verified_intel: str,
) -> tuple[str, str]:
    """建立第二階段：幽默且具專業度的政經策略師撰稿提示詞。"""
    date_str = now_tw.strftime("%Y/%m/%d")
    weekday_str = f"週{WEEKDAY_ZH[now_tw.weekday()]}"
    time_str = now_tw.strftime("%H:%M")

    system_instruction = (
        "你是一位在台灣與華爾街打滾多年的資深「政經與題材選股策略師」，"
        "擅長用「極簡懶人包＋幽默搞笑＋法人級專業度」撰寫 30 秒就能看完的台股盤前電子報。\n\n"
        "【風格與字數鐵律】\n"
        "- **越精簡越好（懶人包極致化）**：禁止寫超過 2 行的長段落！全面使用條列、表格、箭頭與精準短句。\n"
        "- **字數精控**：全文盡量控制在 450～650 中文字內，句句是乾貨，絕不水字數、不寫教科書式公司簡介。\n"
        "- **幽默但俐落**：用一針見血的神比喻或微吐槽點綴即可，不講長篇笑話。\n"
        "- 全文使用台灣習慣的繁體中文與金融術語（費半、台積電 ADR、外資、公股、利多出盡）。\n\n"
        "【最高防幻覺鐵律】\n"
        "1. 所有指數、股價、漲跌幅、交易日期與政經事件，**必須嚴格依據 `<verified_intel>` 的已查證資料**。\n"
        "2. 嚴禁編造、竄改或腦補任何數字或新聞；查無最新確認數字請標註「查無最新數據」。\n"
        "3. 若美股遇休市，須用一行短註明休市日期與採用的最新收盤日。\n"
        "4. 提及台股個股時，務必確認 4 碼股票代號與公司名稱完全正確（如 `2330 台積電`）。"
    )

    user_prompt = f"""今天是台灣時間 {date_str}（{weekday_str}）{time_str}，台股即將開盤。
本次早報關注主軸：{market_focus}

以下是剛剛透過 Google Search 查證完成的過去 24 小時真實市場與政經情報：
<verified_intel>
{verified_intel}
</verified_intel>

請嚴格依據 `<verified_intel>` 的真實資料，按以下「極簡懶人包」架構輸出（不要加「好的」等開場廢話）：

1. **開頭直接點名今日關注題材**（最上方，先給重點！**禁止使用任何卡通 Emoji 如 🎯、🔥、☕**）：
   - 先用 **1 句**（25 字內）幽默早安短句破題今日氣氛。
   - 緊接著用引用區塊 `>` 直接點名今天要盯的 2~3 個核心題材與頭號指標股，格式必須嚴格如下（不要加任何 Emoji）：
     > **今日必盯題材**
     > 1. **題材關鍵字**：代表股（例如 `2317 鴻海`、`2382 廣達`）
     > 2. **題材關鍵字**：代表股（例如 `2330 台積電`）

2. `## 今日政經關鍵風向`（**不要寫文字段落！全面改用箭頭與數字呈現**）：
   - 先用一個乾淨的 Markdown 表格呈現最新美股收盤（標註收盤日期），漲跌欄一律使用 `▲ +X.XX%`（上升）或 `▼ -X.XX%`（下降）：
     | 指標 | 收盤 | 漲跌 |
     |---|---:|---:|
     | 費半 (SOX) | ... | ▲ +X.XX% / ▼ -X.XX% |
     | 輝達 (NVDA) | ... | ... |
     | 台積電 ADR (TSM) | ... | ... |
     | 道瓊 | ... | ... |
     | 那斯達克 | ... | ... |
     | S&P 500 | ... | ... |
     （若遇休市，表格下用 1 行短註明即可）
   - 接著用 **3~4 條極簡「箭頭＋數字」速讀條列** 交代關鍵政經與台股籌碼風向（**禁止長句敘述**，每條限 25 字內），例如：
     - **利率/通膨**：PCE `3.8%` ▲｜GDP `4.3%` ▲ → 升息壓力 ▲
     - **關稅/政治**：美擬對 14 國課稅｜晶片稅喊 `200%` ⚡ → 半導體承壓 ▼
     - **台股籌碼**：公股 ▲ `+720 億`｜外資空單 ▲ `10.5 萬口`

3. `## 今日必看 2~3 大題材與受惠股`（呼應開頭點名的題材，每個用 `### 題材 N：...`，只寫三行短重點）：
   - **受惠股**：列出 2~4 檔台股代號與名稱（如 `2317 鴻海`、`2382 廣達`，一行帶過，不寫長篇介紹）
   - **一句話邏輯**：限 1~2 句（45 字內），直講最新新聞利多與為何受惠
   - **盤前盯盤**：限 1 句（30 字內），點出開盤最該盯的價量或指標

4. `## 潛在風險與避雷提示`（極簡 2~3 條）：
   - 每條限 1~2 句（35 字內），用幽默但直白的語氣提醒今日最易踩雷點與操作紀律。

5. `## 今日重點關注三支股票`（放在最後、緊接在風險提示下方！精選今日最值得盯盤的 3 檔代表台股，只用以下 4 欄 Markdown 表格呈現，數據必須完全來自 `<verified_intel>` 的最新台股收盤）：
   | 代號 | 名稱 | 最新收盤 | 漲跌 |
   |---|---|---:|---:|
   | 2330 | 台積電 | ... | ▲ +X.XX% / ▼ -X.XX% |
   | ... | ... | ... | ... |
   | ... | ... | ... | ... |
   （剛好 3 檔台股；若某檔查無確認收盤數字請填 `--`，嚴禁編造！）
"""
    return system_instruction, user_prompt


def extract_grounding_sources(response: Any, max_sources: int = 10) -> list[tuple[str, str]]:
    """從 Gemini Google Search Grounding 回應中擷取參考新聞來源。"""
    sources: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return sources

    metadata = getattr(candidates[0], "grounding_metadata", None)
    if not metadata:
        return sources

    chunks = getattr(metadata, "grounding_chunks", None) or []
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        if not web:
            continue
        url = (getattr(web, "uri", None) or "").strip()
        title = (getattr(web, "title", None) or "").strip() or url
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append((title, url))
        if len(sources) >= max_sources:
            break

    return sources


def extract_search_queries(response: Any) -> list[str]:
    """擷取 Gemini 實際執行的 Google Search 關鍵字清單，方便驗證是否成功連網查證。"""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    metadata = getattr(candidates[0], "grounding_metadata", None)
    if not metadata:
        return []
    queries = getattr(metadata, "web_search_queries", None) or []
    return [str(q) for q in queries if q]


FALLBACK_GEMINI_MODELS = ("gemini-2.5-flash-lite", "gemini-2.0-flash")


def _call_gemini_with_retry(
    client: genai.Client,
    model: str,
    contents: str,
    config: types.GenerateContentConfig,
    max_attempts: int = 4,
) -> Any:
    """呼叫 Gemini API，遇 503 尖峰自動退避重試，遇每日 429 額度上限則自動切換備援模型。"""
    candidate_models = [model] + [m for m in FALLBACK_GEMINI_MODELS if m != model]

    for current_model in candidate_models:
        delay = 5
        for attempt in range(1, max_attempts + 1):
            try:
                return client.models.generate_content(
                    model=current_model,
                    contents=contents,
                    config=config,
                )
            except genai_errors.APIError as exc:
                status_code = getattr(exc, "code", None)
                err_text = str(exc)
                # 若是當日免費額度耗盡（PerDay），直接跳下一個備援模型而不浪費時間空等
                if status_code == 429 and ("PerDay" in err_text or "per day" in err_text.lower()):
                    print(f"   ⚠️ 模型 {current_model} 今日免費額度已達上限，嘗試切換備援模型...")
                    break
                retryable = status_code in (429, 500, 502, 503, 504)
                if not retryable:
                    raise
                if attempt == max_attempts:
                    if current_model != candidate_models[-1]:
                        print(f"   ⚠️ 模型 {current_model} 連續忙碌，改由備援模型接手...")
                        break
                    raise
                print(
                    f"   ⏳ Gemini ({current_model}) 暫時忙碌（HTTP {status_code}），{delay} 秒後進行第 {attempt + 1}/{max_attempts} 次重試..."
                )
                time.sleep(delay)
                delay *= 2

    raise RuntimeError("所有 Gemini 模型均無法回應或今日額度已滿，請稍後再試。")


def generate_daily_report(config: dict[str, Any], now_tw: datetime) -> tuple[str, list[tuple[str, str]]]:
    """兩階段生成早報：
    1. 啟用 Google Search Grounding 蒐集並查證過去 24 小時真實市場與政經情報。
    2. 根據已查證情報，以幽默且具專業度的政經策略師口吻產出固定三段式報告。
    """
    client = genai.Client(
        api_key=config["gemini_api_key"],
        http_options=types.HttpOptions(api_version="v1beta"),
    )
    model = config["gemini_model"]

    # 第一階段：強制純事實 Google Search 查證
    print(f"🔍 [階段 1/2] 透過 Gemini ({model}) + Google Search 蒐集過去 24 小時真實政經與盤前數據...")
    research_prompt = build_research_prompt(now_tw, config["market_focus"])

    research_response = _call_gemini_with_retry(
        client=client,
        model=model,
        contents=research_prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.1,
        ),
    )

    verified_intel = (getattr(research_response, "text", None) or "").strip()
    sources = extract_grounding_sources(research_response)
    queries = extract_search_queries(research_response)

    if not verified_intel:
        raise RuntimeError("第一階段情報蒐集回傳空白內容，請稍後重試或檢查模型與金鑰權限。")

    print(f"   ↳ 已執行 {len(queries)} 次 Google 搜尋，取得 {len(sources)} 筆即時新聞來源。")
    if not sources:
        print("   ⚠️ 警告：本次回傳未附帶 grounding_chunks，請檢查搜尋額度或稍後重試。")

    # 第二階段：以已查證事實撰寫幽默專業早報
    print(f"✍️  [階段 2/2] 政經策略師撰寫幽默專業盤前早報...")
    system_instruction, strategist_prompt = build_strategist_prompts(
        now_tw,
        config["market_focus"],
        verified_intel,
    )

    report_response = _call_gemini_with_retry(
        client=client,
        model=model,
        contents=strategist_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.55,
        ),
    )

    report_md = (getattr(report_response, "text", None) or "").strip()
    if not report_md:
        raise RuntimeError("第二階段早報撰寫回傳空白內容，請稍後重試。")

    return report_md, sources


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """跨平台載入高質感無襯線字體（支援 Windows 本機與 GitHub Actions Ubuntu）。"""
    candidates = (
        [
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    )
    for font_path in candidates:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _load_cjk_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """跨平台載入繁體中文字體（供台股動態圖卡顯示中文股名）。"""
    candidates = (
        [
            "C:/Windows/Fonts/msjhbd.ttc",
            "C:/Windows/Fonts/msjh.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
        if bold
        else [
            "C:/Windows/Fonts/msjh.ttc",
            "C:/Windows/Fonts/msjhbd.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        ]
    )
    for font_path in candidates:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size=size)
            except Exception:
                continue
    return _load_font(size, bold=bold)


def _clean_md_cell(text: str) -> str:
    """清除 Markdown 儲存格內的粗體、反引號與多餘空白。"""
    cleaned = re.sub(r"[`*]", "", text).strip()
    return re.sub(r"\s+", " ", cleaned)


def extract_us_market_metrics(
    report_md: str,
    now_tw: datetime,
) -> tuple[list[dict[str, str]], str]:
    """從早報 Markdown 表格擷取費半(SOX)、輝達(NVDA)、台積電 ADR(TSM) 的真實收盤與漲跌。"""
    targets = [
        ("SOX", "PHLX SEMI", re.compile(r"(費半|SOX|費城半導體)", re.IGNORECASE)),
        ("NVDA", "NVIDIA", re.compile(r"(輝達|NVDA|NVIDIA)", re.IGNORECASE)),
        ("TSM", "TSMC ADR", re.compile(r"(台積電\s*ADR|TSM|TSMC)", re.IGNORECASE)),
    ]

    found: dict[str, dict[str, str]] = {}
    for line in report_md.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [_clean_md_cell(c) for c in stripped.strip("|").split("|")]
        if len(cells) < 3 or set(cells[0]) <= {"-", ":", " "}:
            continue
        label, price_raw, change_raw = cells[0], cells[1], cells[2]

        for symbol, subtitle, pattern in targets:
            if symbol in found or not pattern.search(label):
                continue

            # 整理價格字串（僅保留乾淨的數字與貨幣符號，避免中文字在英文字體下缺字）
            price_match = re.search(r"\$?\d[\d,]*(?:\.\d+)?", price_raw)
            if price_match:
                price_val = price_match.group(0)
                if symbol in ("NVDA", "TSM") and not price_val.startswith("$"):
                    price_val = f"${price_val}"
            else:
                price_val = "N/A"

            pct_match = re.search(r"([+\-]?\d+(?:\.\d+)?%)", change_raw)
            pct_text = pct_match.group(1) if pct_match else "--"

            if "▲" in change_raw or (pct_text.startswith("+") and pct_text != "+0.00%"):
                direction = "up"
                if pct_text != "--" and not pct_text.startswith(("+", "-")):
                    pct_text = f"+{pct_text}"
            elif "▼" in change_raw or pct_text.startswith("-"):
                direction = "down"
                if pct_text != "--" and not pct_text.startswith(("+", "-")):
                    pct_text = f"-{pct_text}"
            else:
                direction = "flat"

            found[symbol] = {
                "symbol": symbol,
                "subtitle": subtitle,
                "price": price_val,
                "change": pct_text,
                "direction": direction,
            }

    metrics = [found[sym] for sym, _, _ in targets if sym in found]

    # 嘗試擷取收盤日期（支援 2026/11/25、2026-11-25 或 2026 年 11 月 25 日），否則回退昨日日期
    date_match = re.search(r"(20\d{2})\s*[年/\-]\s*(\d{1,2})\s*[月/\-]\s*(\d{1,2})", report_md)
    if date_match:
        trade_date = f"{date_match.group(1)}/{int(date_match.group(2)):02d}/{int(date_match.group(3)):02d}"
    else:
        trade_date = (now_tw - timedelta(days=1)).strftime("%Y/%m/%d")

    return metrics, trade_date


def extract_tw_spotlight_stocks(report_md: str) -> list[dict[str, Any]]:
    """從「今日重點關注三支股票」表格擷取 3 檔焦點台股（代號、股名、收盤價、漲跌）。"""
    picks: list[dict[str, Any]] = []
    seen_codes: set[str] = set()

    sec_match = re.search(
        r"##\s*今日重點關注三支股票.*?(?=\n##\s|\Z)",
        report_md,
        flags=re.DOTALL,
    )
    section = sec_match.group(0) if sec_match else ""

    for line in section.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [_clean_md_cell(c) for c in stripped.strip("|").split("|")]
        if len(cells) < 3 or set(cells[0]) <= {"-", ":", " "}:
            continue
        if any(h in cells[0] for h in ("代號", "股票")):
            continue

        if len(cells) >= 4:
            code_raw, name_raw, price_raw, change_raw = cells[0], cells[1], cells[2], cells[3]
        else:
            # 容錯：若第一欄合併為「2330 台積電」
            parts = cells[0].split(maxsplit=1)
            code_raw = parts[0]
            name_raw = parts[1] if len(parts) > 1 else ""
            price_raw, change_raw = cells[1], cells[2]

        code_match = re.search(r"\b(\d{4,6}[A-Za-z]?)\b", code_raw)
        if not code_match:
            continue
        code = code_match.group(1)
        if code in seen_codes:
            continue
        seen_codes.add(code)

        name = name_raw.strip()[:6] or "台股焦點"
        price_match = re.search(r"\d[\d,]*(?:\.\d+)?", price_raw)
        price_val = f"NT${price_match.group(0)}" if price_match else "--"

        pct_match = re.search(r"([+\-]?\d+(?:\.\d+)?%)", change_raw)
        pct_text = pct_match.group(1) if pct_match else "WATCH"

        if "▲" in change_raw or (pct_text.startswith("+") and pct_text != "+0.00%"):
            direction = "up"
            if pct_text != "WATCH" and not pct_text.startswith(("+", "-")):
                pct_text = f"+{pct_text}"
        elif "▼" in change_raw or pct_text.startswith("-"):
            direction = "down"
            if pct_text != "WATCH" and not pct_text.startswith(("+", "-")):
                pct_text = f"-{pct_text}"
        else:
            direction = "flat"

        picks.append(
            {
                "symbol": code,
                "subtitle": name,
                "subtitle_cjk": True,
                "price": price_val,
                "change": pct_text,
                "direction": direction,
            }
        )
        if len(picks) >= 3:
            break

    # 保底機制：若表格未擷取滿 3 檔，從報告中補齊已提及的 4 碼台股代號與股名
    if len(picks) < 3:
        for m in re.finditer(r"`?(\d{4})\s+([\u4e00-\u9fffA-Za-z0-9\-]{2,6})`?", report_md):
            code, name = m.group(1), m.group(2)
            if code in seen_codes:
                continue
            seen_codes.add(code)
            picks.append(
                {
                    "symbol": code,
                    "subtitle": name,
                    "subtitle_cjk": True,
                    "price": "FOCUS",
                    "change": "WATCH",
                    "direction": "flat",
                }
            )
            if len(picks) >= 3:
                break

    return picks


def _draw_gold_diamond(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: tuple[int, int, int, int]) -> None:
    """繪製英倫復古燙金菱形裝飾。"""
    draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=color)


def _draw_metric_tile(
    metric: dict[str, str],
    width: int,
    height: int,
    revealed: bool = True,
    flash_alpha: float = 0.0,
) -> Image.Image:
    """繪製單張英倫孔雀深藍綠 × 香檳燙金風格美股數據卡。"""
    tile = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)

    gold = (200, 168, 107, 255)        # #C8A86B 復古香檳金
    gold_hi = (223, 195, 136, 255)     # #DFC388 亮香檳金
    gold_dim = (140, 117, 74, 255)     # #8C754A 低調古銅金
    cream = (246, 240, 226, 255)       # #F6F0E2 英倫羊皮紙米白

    direction = metric["direction"]
    if direction == "up":
        accent = (217, 70, 85, 255)    # 英倫酒紅（台股漲）
        pill_bg = (68, 25, 34, 255)
        pill_text = (255, 196, 196, 255)
        arrow_color = (242, 116, 116, 255)
    elif direction == "down":
        accent = (46, 155, 110, 255)   # 英倫墨綠（台股跌）
        pill_bg = (16, 56, 48, 255)
        pill_text = (182, 240, 212, 255)
        arrow_color = (107, 212, 162, 255)
    else:
        accent = gold
        pill_bg = (16, 58, 74, 255)
        pill_text = gold_hi
        arrow_color = gold_hi

    bg_color = (8, 40, 54, 255) if revealed else (7, 34, 46, 255)
    border_color = gold if revealed else gold_dim
    draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=12,
        fill=bg_color,
        outline=border_color,
        width=2,
    )
    # 內層英倫燙金極細雙框
    draw.rounded_rectangle(
        (5, 5, width - 6, height - 6),
        radius=9,
        outline=(118, 99, 64, 150),
        width=1,
    )

    is_cjk = bool(metric.get("subtitle_cjk"))
    sub_len = len(metric["subtitle"])
    if is_cjk:
        cjk_size = 24 if sub_len <= 3 else (21 if sub_len == 4 else 18)
        f_sub = _load_cjk_font(cjk_size, bold=True)
    else:
        f_sub = _load_font(13, bold=True)

    f_sym = _load_font(22, bold=True)
    f_price = _load_font(33 if len(metric["price"]) <= 9 else 27, bold=True)
    f_pct = _load_font(19, bold=True)

    if not revealed:
        # 翻牌前：復古英倫撲克牌背設計（燙金菱形徽章＋中線）
        mid_y = height // 2
        draw.line((22, mid_y, width - 22, mid_y), fill=gold_dim, width=1)
        _draw_gold_diamond(draw, width // 2, mid_y, 9, gold)
        sym_bbox = draw.textbbox((0, 0), metric["symbol"], font=f_sym)
        sym_w = sym_bbox[2] - sym_bbox[0]
        draw.text(((width - sym_w) // 2, 26), metric["symbol"], font=f_sym, fill=gold_hi)
        sub_label = metric["subtitle"] if is_cjk else "REVEALING"
        sub_bbox = draw.textbbox((0, 0), sub_label, font=f_sub)
        sub_w = sub_bbox[2] - sub_bbox[0]
        draw.text(
            ((width - sub_w) // 2, mid_y + (18 if is_cjk else 24)),
            sub_label,
            font=f_sub,
            fill=gold_hi if is_cjk else gold_dim,
        )
        return tile

    # 頂部細緻狀態色條
    draw.rounded_rectangle((18, 12, width - 18, 15), radius=2, fill=accent)

    # 第一列：香檳金 Ticker 與副標（台股放大至 21~24px 微軟正黑體並精準對齊）
    draw.text((18, 25), metric["symbol"], font=f_sym, fill=gold_hi)
    sub_bbox = draw.textbbox((0, 0), metric["subtitle"], font=f_sub)
    sub_w = sub_bbox[2] - sub_bbox[0]
    sub_h = sub_bbox[3] - sub_bbox[1]
    sym_bbox = draw.textbbox((0, 0), metric["symbol"], font=f_sym)
    sym_center_y = 25 + sym_bbox[1] + (sym_bbox[3] - sym_bbox[1]) // 2
    sub_y = sym_center_y - sub_h // 2 - sub_bbox[1]
    draw.text(
        (width - 18 - sub_w, sub_y),
        metric["subtitle"],
        font=f_sub,
        fill=gold_hi if is_cjk else gold_dim,
    )

    # 第二列：大型羊皮紙米白收盤價
    draw.text((18, 63), metric["price"], font=f_price, fill=cream)

    # 第三列：圓角膠囊 + 向量箭頭 + % 數
    pill_x1, pill_y1, pill_x2, pill_y2 = 18, height - 48, width - 18, height - 16
    draw.rounded_rectangle(
        (pill_x1, pill_y1, pill_x2, pill_y2),
        radius=8,
        fill=pill_bg,
        outline=(200, 168, 107, 90),
        width=1,
    )

    cy = (pill_y1 + pill_y2) // 2
    tri_x = pill_x1 + 14
    if direction == "up":
        draw.polygon(
            [(tri_x + 6, cy - 6), (tri_x, cy + 5), (tri_x + 12, cy + 5)],
            fill=arrow_color,
        )
    elif direction == "down":
        draw.polygon(
            [(tri_x, cy - 5), (tri_x + 12, cy - 5), (tri_x + 6, cy + 6)],
            fill=arrow_color,
        )
    else:
        draw.rectangle((tri_x, cy - 2, tri_x + 12, cy + 2), fill=arrow_color)

    pct_bbox = draw.textbbox((0, 0), metric["change"], font=f_pct)
    pct_h = pct_bbox[3] - pct_bbox[1]
    draw.text(
        (tri_x + 20, cy - pct_h // 2 - pct_bbox[1]),
        metric["change"],
        font=f_pct,
        fill=pill_text,
    )

    # 翻牌定格瞬間的香檳金微光邊框
    if flash_alpha > 0.01:
        a = int(min(255, 220 * flash_alpha))
        draw.rounded_rectangle(
            (1, 1, width - 2, height - 2),
            radius=12,
            outline=(239, 217, 166, a),
            width=2,
        )
    return tile


def generate_market_ticker_gif(
    metrics: list[dict[str, Any]],
    trade_date: str,
    header_title: str = "US MARKET SNAPSHOT",
    date_label: str | None = None,
) -> bytes | None:
    """生成英倫孔雀深藍綠 × 香檳燙金「動態翻牌數據卡」GIF，完整相容 Gmail。"""
    if not metrics:
        return None

    width, height = 900, 268
    bg_rgb = (11, 53, 71)  # #0B3547 經典英倫孔雀深藍綠
    gold = (200, 168, 107, 255)
    gold_hi = (223, 195, 136, 255)
    gold_dim = (158, 131, 79, 255)

    margin_x = 24
    gap = 16
    count = len(metrics)
    tile_w = (width - margin_x * 2 - gap * (count - 1)) // count
    tile_h = 166
    tile_y = 78

    f_hdr = _load_font(15, bold=True)
    f_date = _load_font(13, bold=False)

    back_tiles = [_draw_metric_tile(m, tile_w, tile_h, revealed=False) for m in metrics]
    front_tiles = [_draw_metric_tile(m, tile_w, tile_h, revealed=True) for m in metrics]
    flash_tiles = [_draw_metric_tile(m, tile_w, tile_h, revealed=True, flash_alpha=0.9) for m in metrics]

    frames: list[Image.Image] = []
    durations: list[int] = []
    total_frames = 22

    for idx in range(total_frames):
        canvas = Image.new("RGBA", (width, height), bg_rgb + (255,))
        draw = ImageDraw.Draw(canvas)

        # 外層香檳金雙線外框
        draw.rounded_rectangle(
            (2, 2, width - 3, height - 3),
            radius=16,
            fill=(11, 53, 71, 255),
            outline=gold,
            width=2,
        )
        draw.rounded_rectangle(
            (8, 8, width - 9, height - 9),
            radius=12,
            outline=(140, 117, 74, 140),
            width=1,
        )

        # 頂部香檳金脈衝光點與標題
        pulse = 0.5 + 0.5 * math.sin(idx * 0.48)
        dot_cx, dot_cy = 38, 33
        ring_r = int(6 + 4 * pulse)
        ring_alpha = int(125 * (1.0 - pulse * 0.6))
        draw.ellipse(
            (dot_cx - ring_r, dot_cy - ring_r, dot_cx + ring_r, dot_cy + ring_r),
            fill=(223, 195, 136, ring_alpha),
        )
        draw.ellipse(
            (dot_cx - 4, dot_cy - 4, dot_cx + 4, dot_cy + 4),
            fill=gold_hi,
        )
        draw.text((54, 24), header_title, font=f_hdr, fill=gold_hi)

        resolved_date_label = date_label or f"CLOSE  {trade_date}"
        d_bbox = draw.textbbox((0, 0), resolved_date_label, font=f_date)
        draw.text(
            (width - 28 - (d_bbox[2] - d_bbox[0]), 26),
            resolved_date_label,
            font=f_date,
            fill=gold,
        )

        # 可不可風格：兩側附菱形飾點的燙金水平分隔線 ◆────────◆
        line_y = 58
        draw.line((32, line_y, width - 32, line_y), fill=gold_dim, width=1)
        _draw_gold_diamond(draw, 30, line_y, 5, gold_hi)
        _draw_gold_diamond(draw, width - 30, line_y, 5, gold_hi)

        # 逐張繪製機械翻牌動畫
        for i in range(count):
            tx = margin_x + i * (tile_w + gap)
            start_f = 1 + i * 3
            local = idx - start_f

            if local < 0:
                tile_img = back_tiles[i]
                scale_y = 1.0
            elif local == 0:
                tile_img = back_tiles[i]
                scale_y = 0.72
            elif local == 1:
                tile_img = back_tiles[i]
                scale_y = 0.28
            elif local == 2:
                tile_img = flash_tiles[i]
                scale_y = 0.32
            elif local == 3:
                tile_img = flash_tiles[i]
                scale_y = 0.80
            elif local == 4:
                tile_img = flash_tiles[i]
                scale_y = 1.0
            else:
                tile_img = front_tiles[i]
                scale_y = 1.0

            if scale_y < 0.99:
                new_h = max(8, int(tile_h * scale_y))
                resized = tile_img.resize((tile_w, new_h), Image.Resampling.BILINEAR)
                y_offset = tile_y + (tile_h - new_h) // 2
                canvas.alpha_composite(resized, (tx, y_offset))
            else:
                canvas.alpha_composite(tile_img, (tx, tile_y))

        # 階段 2（影格 14~19）：柔和暖金色斜向流光掃過三張數據卡（精簡影格以降低信件體積）
        if 14 <= idx <= 19:
            progress = (idx - 14) / 5.0
            beam_x = int(-140 + progress * (width + 280))
            sheen = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            s_draw = ImageDraw.Draw(sheen)
            s_draw.polygon(
                [
                    (beam_x, tile_y),
                    (beam_x + 76, tile_y),
                    (beam_x + 24, tile_y + tile_h),
                    (beam_x - 52, tile_y + tile_h),
                ],
                fill=(239, 217, 166, 28),
            )
            canvas = Image.alpha_composite(canvas, sheen)

        rgb_frame = Image.new("RGB", (width, height), bg_rgb)
        rgb_frame.paste(canvas, mask=canvas.split()[3])
        quantized = rgb_frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
        frames.append(quantized)

        if idx < 14:
            durations.append(75)
        elif idx <= 19:
            durations.append(95)
        elif idx == 20:
            durations.append(1800)
        else:
            durations.append(550)

    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    return buf.getvalue()


def _inject_inline_styles(html_body: str) -> str:
    """為 HTML 元素套用英倫孔雀深藍綠 × 香檳燙金 inline style，確保各家信箱排版一致。"""
    tag_styles = {
        "h2": (
            "margin: 26px 0 12px 0; padding: 0 0 8px 0; "
            "border-bottom: 1px solid #C8A86B; "
            "font-family: Georgia, 'Noto Serif TC', 'PMingLiU', serif; "
            "font-size: 19px; line-height: 1.4; color: #DFC388; font-weight: 700; letter-spacing: 0.08em;"
        ),
        "h3": (
            "margin: 18px 0 8px 0; font-family: Georgia, 'Noto Serif TC', 'PMingLiU', serif; "
            "font-size: 16px; line-height: 1.45; color: #D6B87A; font-weight: 700; letter-spacing: 0.04em;"
        ),
        "p": "margin: 0 0 12px 0; font-size: 15px; line-height: 1.75; color: #F3EBD8;",
        "ul": "margin: 6px 0 14px 0; padding-left: 20px;",
        "ol": "margin: 6px 0 14px 0; padding-left: 20px;",
        "li": "margin: 0 0 8px 0; font-size: 15px; line-height: 1.7; color: #F3EBD8;",
        "strong": "color: #DFC388; font-weight: 700;",
        "blockquote": (
            "margin: 14px 0 22px 0; padding: 15px 18px; background-color: #082836; "
            "border: 1px solid #C8A86B; border-left: 4px solid #DFC388; "
            "border-radius: 6px; color: #F3EBD8;"
        ),
        "code": (
            "background-color: #07222E; color: #DFC388; border: 1px solid #8F7443; "
            "padding: 2px 7px; border-radius: 4px; "
            "font-family: 'SFMono-Regular', Consolas, monospace; font-size: 13px; font-weight: 600;"
        ),
        "hr": "border: none; border-top: 1px solid #8F7443; margin: 20px 0;",
        "table": (
            "width: 100%; border-collapse: collapse; margin: 12px 0 16px 0; "
            "font-size: 14px; line-height: 1.55; background-color: #082836;"
        ),
        "th": (
            "background-color: #07222E; color: #DFC388; text-align: left; "
            "padding: 8px 10px; border-top: 1px solid #C8A86B; border-bottom: 1px solid #C8A86B; "
            "font-family: Georgia, 'Noto Serif TC', serif; font-weight: 700; letter-spacing: 0.06em;"
        ),
        "td": (
            "padding: 8px 10px; border-bottom: 1px solid #2A5466; color: #F3EBD8;"
        ),
        "a": "color: #DFC388; text-decoration: underline;",
    }

    styled = html_body
    for tag, style in tag_styles.items():
        pattern = rf"<{tag}(\s[^>]*?)?(\s*/)?\s*>"

        def _repl(match: re.Match[str], t: str = tag, s: str = style) -> str:
            attrs = match.group(1) or ""
            closing = match.group(2) or ""
            if re.search(r'\bstyle\s*=\s*"', attrs, flags=re.IGNORECASE):
                attrs = re.sub(
                    r'\bstyle\s*=\s*"([^"]*)"',
                    lambda sm: f'style="{s} {sm.group(1)}"',
                    attrs,
                    count=1,
                    flags=re.IGNORECASE,
                )
                return f"<{t}{attrs}{closing}>"
            return f'<{t}{attrs} style="{s}"{closing}>'

        styled = re.sub(pattern, _repl, styled)

    # 將「今日必盯題材」升級為英倫香檳金復古標籤（並自動清除可能殘留的 🎯）
    focus_badge = (
        '<span style="display: inline-block; background-color: #C8A86B; color: #082836; '
        'font-size: 11px; font-weight: 800; letter-spacing: 0.14em; padding: 3px 9px; '
        'border-radius: 3px; vertical-align: middle; margin-right: 9px;">FOCUS</span>'
        '<strong style="font-family: Georgia, \'Noto Serif TC\', serif; font-size: 16px; '
        'color: #DFC388; letter-spacing: 0.08em; vertical-align: middle;">今日必盯題材</strong>'
    )
    styled = re.sub(
        r"(?:🎯\s*)?<strong[^>]*>\s*(?:🎯\s*)?今日必盯題材\s*</strong>",
        focus_badge,
        styled,
        count=1,
    )

    # 為每個 h2 章節前加上精緻金色菱形飾點 ◆
    styled = re.sub(
        r"(<h2[^>]*>)",
        r'\1<span style="color: #C8A86B; font-size: 13px; margin-right: 8px;">◆</span>',
        styled,
    )

    # 套用英倫深底高對比配色（台股紅漲綠跌）：▲ 亮酒紅、▼ 亮翠綠
    styled = re.sub(
        r"(▲(?:\s*[+\-]?\d[\d,\.]*\s*(?:%|億|萬口|點)?)?)",
        r'<span style="color: #FF8585; font-weight: 700;">\1</span>',
        styled,
    )
    styled = re.sub(
        r"(▼(?:\s*[+\-]?\d[\d,\.]*\s*(?:%|億|萬口|點)?)?)",
        r'<span style="color: #6EE7B7; font-weight: 700;">\1</span>',
        styled,
    )
    return styled


def render_email_html(
    report_md: str,
    sources: list[tuple[str, str]],
    now_tw: datetime,
    market_focus: str,
    ticker_img_src: str | None = None,
    tw_picks_img_src: str | None = None,
    interactive_url: str = "",
) -> str:
    """將 Markdown 報告轉換為英倫孔雀深藍綠 × 香檳燙金響應式 HTML 電子報。"""
    date_str = now_tw.strftime("%Y/%m/%d")
    weekday_str = f"週{WEEKDAY_ZH[now_tw.weekday()]}"
    generated_at = now_tw.strftime("%Y/%m/%d %H:%M")

    raw_html = markdown.markdown(
        report_md,
        extensions=["extra", "sane_lists", "nl2br"],
    )
    body_html = _inject_inline_styles(raw_html)

    # 將美股英倫動態翻牌圖卡 GIF（以及選填的互動翻牌按鈕）安插在「今日政經關鍵風向」標題下方
    if ticker_img_src:
        cta_html = ""
        if interactive_url:
            cta_html = f"""
            <div style="text-align: center; margin-top: 12px;">
              <a href="{html.escape(interactive_url, quote=True)}" target="_blank" rel="noopener noreferrer"
                 style="display: inline-block; background-color: #C8A86B; color: #082836; text-decoration: none;
                        font-family: Georgia, 'Noto Serif TC', serif; font-size: 13px; font-weight: 800;
                        letter-spacing: 0.1em; padding: 9px 20px; border-radius: 4px; border: 1px solid #DFC388;">
                ◆ 開啟今日 3D 互動翻牌網頁版 ◆
              </a>
            </div>
            """
        ticker_block = f"""
        <div style="margin: 14px 0 18px 0;">
          <img src="{ticker_img_src}" alt="US Market Snapshot (SOX / NVDA / TSM ADR)" width="100%" style="display: block; width: 100%; max-width: 608px; height: auto; border-radius: 10px;" />
          {cta_html}
        </div>
        """
        inserted, count = re.subn(
            r"(<h2[^>]*>.*?今日政經關鍵風向.*?</h2>)",
            rf"\1{ticker_block}",
            body_html,
            count=1,
            flags=re.DOTALL,
        )
        body_html = inserted if count else ticker_block + body_html

    # 將台股重點關注三支股票動態翻牌 GIF 安插在「潛在風險與避雷提示」與「SOURCES」之間
    if tw_picks_img_src:
        tw_picks_block = f"""
        <div style="margin: 14px 0 18px 0;">
          <img src="{tw_picks_img_src}" alt="TW Spotlight Picks (Top 3)" width="100%" style="display: block; width: 100%; max-width: 608px; height: auto; border-radius: 10px;" />
        </div>
        """
        inserted_tw, count_tw = re.subn(
            r"(<h2[^>]*>.*?今日重點關注三支股票.*?</h2>)",
            rf"\1{tw_picks_block}",
            body_html,
            count=1,
            flags=re.DOTALL,
        )
        if count_tw:
            body_html = inserted_tw
        else:
            body_html += (
                '<h2 style="margin: 26px 0 12px 0; padding: 0 0 8px 0; border-bottom: 1px solid #C8A86B; '
                "font-family: Georgia, 'Noto Serif TC', 'PMingLiU', serif; font-size: 19px; line-height: 1.4; "
                'color: #DFC388; font-weight: 700; letter-spacing: 0.08em;">'
                '<span style="color: #C8A86B; font-size: 13px; margin-right: 8px;">◆</span>今日重點關注三支股票</h2>'
                + tw_picks_block
            )

    sources_section = ""
    if sources:
        # 避免在信件內放入大量 Google 轉址連結而觸發 Gmail 垃圾郵件濾網，改以英倫燙金來源標籤呈現
        unique_titles = list(dict.fromkeys(title for title, _ in sources))
        tags_html = "\n".join(
            f'<span style="display: inline-block; margin: 0 6px 6px 0; padding: 3px 9px; '
            f'background-color: #0B3547; border: 1px solid #8F7443; border-radius: 4px; '
            f'font-size: 12px; color: #DFC388;">{html.escape(title)}</span>'
            for title in unique_titles
        )
        sources_section = f"""
        <div style="margin-top: 26px; padding: 14px 16px; background-color: #082836; border: 1px solid #8F7443; border-radius: 8px;">
          <div style="margin-bottom: 10px;">
            <span style="display: inline-block; background-color: #C8A86B; color: #082836; font-size: 10px; font-weight: 800; letter-spacing: 0.12em; padding: 2px 7px; border-radius: 3px; vertical-align: middle; margin-right: 8px;">SOURCES</span>
            <span style="font-family: Georgia, 'Noto Serif TC', serif; font-size: 13px; font-weight: 700; color: #DFC388; letter-spacing: 0.05em; vertical-align: middle;">即時新聞查證來源（Google Search Grounding）</span>
          </div>
          <div style="line-height: 1.6;">
            {tags_html}
          </div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>【盤前暖男台股財報】{date_str} 今日關鍵題材與觀察名單</title>
  <style>
    @media only screen and (max-width: 600px) {{
      .email-wrapper {{ padding: 10px 6px !important; }}
      .email-card {{ border-radius: 8px !important; }}
      .email-header {{ padding: 22px 16px !important; }}
      .email-body {{ padding: 18px 15px !important; }}
      .email-title {{ font-size: 21px !important; }}
    }}
  </style>
</head>
<body style="margin: 0; padding: 0; background-color: #06212C; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans TC', 'Microsoft JhengHei', sans-serif; -webkit-font-smoothing: antialiased;">
  <div class="email-wrapper" style="padding: 24px 12px; background-color: #06212C;">
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width: 660px; margin: 0 auto;">
      <tr>
        <td>
          <div class="email-card" style="background-color: #0B3547; border-radius: 10px; overflow: hidden; border: 2px solid #C8A86B; box-shadow: 0 10px 28px rgba(0, 0, 0, 0.35);">
            <!-- British Heritage Header Banner -->
            <div class="email-header" style="background-color: #082836; padding: 28px 26px 22px 26px; text-align: center;">
              <div style="font-family: Georgia, serif; font-size: 11px; letter-spacing: 0.22em; color: #C8A86B; font-weight: 700; margin-bottom: 10px;">
                PRE-MARKET INTELLIGENCE · {date_str} ({weekday_str})
              </div>
              <h1 class="email-title" style="margin: 0; font-family: Georgia, 'Noto Serif TC', 'PMingLiU', serif; font-size: 24px; line-height: 1.4; font-weight: 700; color: #DFC388; letter-spacing: 0.14em;">
                盤前暖男台股財報
              </h1>
              <!-- 可不可風格燙金菱形分隔線 ◆──────◆ -->
              <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="margin: 14px auto 12px auto;">
                <tr>
                  <td style="width: 14px; color: #DFC388; font-size: 11px; text-align: left; padding: 0; border: none;">◆</td>
                  <td style="padding: 0; border: none;"><div style="height: 1px; background-color: #C8A86B;"></div></td>
                  <td style="width: 14px; color: #DFC388; font-size: 11px; text-align: right; padding: 0; border: none;">◆</td>
                </tr>
              </table>
              <div style="font-size: 12px; color: #D8CEB8; letter-spacing: 0.1em;">
                焦點主軸：{html.escape(market_focus)}
              </div>
            </div>

            <!-- Main Content -->
            <div class="email-body" style="padding: 22px 26px 26px 26px; background-color: #0B3547;">
              {body_html}
              {sources_section}
            </div>

            <!-- Footer -->
            <div style="padding: 16px 24px; background-color: #082836; border-top: 1px solid #C8A86B; font-size: 12px; line-height: 1.65; color: #B8C4C9;">
              <p style="margin: 0 0 6px 0; color: #D8CEB8;">
                <strong style="color: #DFC388;">免責聲明：</strong>本電子報由 AI 結合即時網路搜尋整理而成，僅供市場政經資訊與題材研究參考，不構成任何買賣或投資建議。投資有賺有賠，下單前請務必審慎評估並獨立判斷。
              </p>
              <p style="margin: 0; color: #8CA6B2;">
                產生時間：{generated_at} (台灣時間) · 本信件以密件副本（Bcc）發送以保護所有訂閱者隱私。
              </p>
            </div>
          </div>
        </td>
      </tr>
    </table>
  </div>
</body>
</html>
"""


def _build_flip_cards_html(items: list[dict[str, Any]], start_idx: int = 0) -> str:
    """產生互動 3D 翻牌卡片 HTML 清單（支援美股與台股重點關注股）。"""
    parts: list[str] = []
    for offset, m in enumerate(items):
        idx = start_idx + offset
        direction = m["direction"]
        dir_class = "up" if direction == "up" else ("down" if direction == "down" else "flat")
        arrow_symbol = "▲" if direction == "up" else ("▼" if direction == "down" else "◆")
        parts.append(f"""
        <div class="flip-card" data-index="{idx}" role="button" tabindex="0" aria-pressed="false" aria-label="翻牌查看 {html.escape(str(m['symbol']))}">
          <div class="flip-card-inner">
            <div class="flip-face flip-back">
              <div class="back-symbol">{html.escape(str(m['symbol']))}</div>
              <div class="back-sub">{html.escape(str(m['subtitle']))}</div>
              <div class="back-divider"><span>◆</span></div>
              <div class="back-hint">點擊翻牌 · TAP TO FLIP</div>
            </div>
            <div class="flip-face flip-front {dir_class}">
              <div class="front-bar"></div>
              <div class="front-top">
                <span class="front-symbol">{html.escape(str(m['symbol']))}</span>
                <span class="front-sub">{html.escape(str(m['subtitle']))}</span>
              </div>
              <div class="front-price">{html.escape(str(m['price']))}</div>
              <div class="front-pill {dir_class}">
                <span class="pill-arrow">{arrow_symbol}</span>
                <span>{html.escape(str(m['change']))}</span>
              </div>
            </div>
          </div>
        </div>
        """)
    return "\n".join(parts)


def render_interactive_flip_page(
    report_md: str,
    metrics: list[dict[str, Any]],
    tw_picks: list[dict[str, Any]],
    trade_date: str,
    sources: list[tuple[str, str]],
    now_tw: datetime,
    market_focus: str,
) -> str:
    """產出真正可讓使用者「親手點擊 3D 翻牌」的英倫風互動網頁版（docs/index.html）。"""
    date_str = now_tw.strftime("%Y/%m/%d")
    weekday_str = f"週{WEEKDAY_ZH[now_tw.weekday()]}"
    generated_at = now_tw.strftime("%Y/%m/%d %H:%M")

    raw_html = markdown.markdown(
        report_md,
        extensions=["extra", "sane_lists", "nl2br"],
    )
    body_html = _inject_inline_styles(raw_html)

    cards_grid_html = _build_flip_cards_html(metrics, start_idx=0)
    if tw_picks:
        tw_cards_html = f"""
        <div class="cards-grid" style="margin: 14px 0 18px;">
          {_build_flip_cards_html(tw_picks, start_idx=len(metrics))}
        </div>
        """
        inserted_tw, count_tw = re.subn(
            r"(<h2[^>]*>.*?今日重點關注三支股票.*?</h2>)",
            rf"\1{tw_cards_html}",
            body_html,
            count=1,
            flags=re.DOTALL,
        )
        body_html = inserted_tw if count_tw else body_html + tw_cards_html

    sources_html = ""
    if sources:
        items = "\n".join(
            f'<li><a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(title)}</a></li>'
            for title, url in sources
        )
        sources_html = f"""
        <section class="sources-box">
          <div class="sources-title"><span class="badge">SOURCES</span>即時新聞參考來源（Google Search Grounding）</div>
          <ul>{items}</ul>
        </section>
        """

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>【英倫互動翻牌版】{date_str} 盤前暖男台股財報</title>
  <style>
    :root {{
      --bg-deep: #06212C;
      --bg-main: #0B3547;
      --bg-panel: #082836;
      --gold: #C8A86B;
      --gold-hi: #DFC388;
      --gold-dim: #8F7443;
      --cream: #F3EBD8;
      --up: #FF8585;
      --up-bar: #D94655;
      --down: #6EE7B7;
      --down-bar: #2E9B6E;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 20px 12px 48px;
      background: radial-gradient(circle at top, #0E4156 0%, var(--bg-deep) 65%);
      color: var(--cream);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans TC', 'Microsoft JhengHei', sans-serif;
    }}
    .wrap {{
      max-width: 720px;
      margin: 0 auto;
      background: var(--bg-main);
      border: 2px solid var(--gold);
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 18px 46px rgba(0, 0, 0, 0.45);
    }}
    header {{
      background: var(--bg-panel);
      padding: 28px 24px 22px;
      text-align: center;
      border-bottom: 1px solid rgba(200, 168, 107, 0.35);
    }}
    .kicker {{
      font-family: Georgia, serif;
      font-size: 11px;
      letter-spacing: 0.22em;
      color: var(--gold);
      font-weight: 700;
      margin-bottom: 8px;
    }}
    h1 {{
      margin: 0;
      font-family: Georgia, 'Noto Serif TC', 'PMingLiU', serif;
      font-size: 26px;
      letter-spacing: 0.15em;
      color: var(--gold-hi);
    }}
    .gold-rule {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 14px 0 10px;
      color: var(--gold-hi);
      font-size: 11px;
    }}
    .gold-rule::before, .gold-rule::after {{
      content: "";
      flex: 1;
      height: 1px;
      background: linear-gradient(90deg, transparent, var(--gold), transparent);
    }}
    .focus-sub {{
      font-size: 13px;
      color: #D8CEB8;
      letter-spacing: 0.08em;
    }}
    .interactive-zone {{
      padding: 22px 24px 8px;
      background: linear-gradient(180deg, #092D3C 0%, var(--bg-main) 100%);
    }}
    .zone-header {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 16px;
    }}
    .zone-title {{
      font-family: Georgia, 'Noto Serif TC', serif;
      font-size: 15px;
      font-weight: 700;
      letter-spacing: 0.08em;
      color: var(--gold-hi);
    }}
    .zone-title small {{
      display: block;
      font-family: -apple-system, BlinkMacSystemFont, 'Noto Sans TC', sans-serif;
      font-size: 12px;
      font-weight: 400;
      color: #B7C7CE;
      letter-spacing: 0.03em;
      margin-top: 2px;
    }}
    .btn-gold {{
      background: transparent;
      color: var(--gold-hi);
      border: 1px solid var(--gold);
      border-radius: 4px;
      padding: 7px 14px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.1em;
      cursor: pointer;
      transition: background 0.2s, color 0.2s, transform 0.15s;
    }}
    .btn-gold:hover {{
      background: var(--gold);
      color: var(--bg-panel);
    }}
    .btn-gold:active {{ transform: scale(0.97); }}
    .cards-grid {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 14px;
    }}
    @media (max-width: 600px) {{
      .cards-grid {{ grid-template-columns: 1fr; }}
    }}
    .flip-card {{
      perspective: 1100px;
      height: 168px;
      cursor: pointer;
      user-select: none;
      -webkit-tap-highlight-color: transparent;
    }}
    .flip-card-inner {{
      position: relative;
      width: 100%;
      height: 100%;
      transition: transform 0.68s cubic-bezier(0.22, 1, 0.36, 1);
      transform-style: preserve-3d;
    }}
    .flip-card.is-flipped .flip-card-inner {{
      transform: rotateY(180deg);
    }}
    .flip-face {{
      position: absolute;
      inset: 0;
      backface-visibility: hidden;
      -webkit-backface-visibility: hidden;
      border-radius: 10px;
      border: 1.5px solid var(--gold);
      background: var(--bg-panel);
      padding: 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      box-shadow: inset 0 0 0 4px rgba(11, 53, 71, 0.9), inset 0 0 0 5px rgba(200, 168, 107, 0.35);
    }}
    .flip-back {{
      align-items: center;
      justify-content: center;
      text-align: center;
      background: radial-gradient(circle at center, #0F4054 0%, #072330 100%);
      transition: border-color 0.25s, box-shadow 0.25s;
    }}
    .flip-card:hover .flip-back {{
      border-color: var(--gold-hi);
      box-shadow: 0 0 20px rgba(200, 168, 107, 0.28), inset 0 0 0 4px rgba(11, 53, 71, 0.9), inset 0 0 0 5px rgba(223, 195, 136, 0.55);
    }}
    .back-symbol {{
      font-family: Georgia, serif;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0.14em;
      color: var(--gold-hi);
    }}
    .back-sub {{
      font-size: 15px;
      font-weight: 700;
      letter-spacing: 0.12em;
      color: var(--gold);
      margin-top: 4px;
    }}
    .back-divider {{
      width: 70%;
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 12px 0;
      color: var(--gold);
      font-size: 10px;
    }}
    .back-divider::before, .back-divider::after {{
      content: "";
      flex: 1;
      height: 1px;
      background: var(--gold-dim);
    }}
    .back-hint {{
      font-size: 11px;
      letter-spacing: 0.12em;
      color: #D8CEB8;
      opacity: 0.9;
    }}
    .flip-front {{
      transform: rotateY(180deg);
    }}
    .front-bar {{
      height: 3px;
      border-radius: 2px;
      background: var(--gold);
      margin-bottom: 6px;
    }}
    .flip-front.up .front-bar {{ background: var(--up-bar); }}
    .flip-front.down .front-bar {{ background: var(--down-bar); }}
    .front-top {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
    }}
    .front-symbol {{
      font-family: Georgia, serif;
      font-size: 19px;
      font-weight: 700;
      color: var(--gold-hi);
      letter-spacing: 0.06em;
    }}
    .front-sub {{
      font-size: 15px;
      font-weight: 700;
      color: var(--gold-hi);
      letter-spacing: 0.06em;
    }}
    .front-price {{
      font-size: 28px;
      font-weight: 800;
      color: #FAF6EE;
      letter-spacing: 0.02em;
      margin: 6px 0;
    }}
    .front-pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 16px;
      font-weight: 700;
      border: 1px solid rgba(200, 168, 107, 0.35);
      background: #103A4A;
      color: var(--gold-hi);
    }}
    .front-pill.up {{ background: #441922; color: var(--up); }}
    .front-pill.down {{ background: #103830; color: var(--down); }}
    main {{
      padding: 16px 26px 28px;
    }}
    .sources-box {{
      margin-top: 26px;
      padding: 14px 16px;
      background: var(--bg-panel);
      border: 1px solid var(--gold-dim);
      border-radius: 8px;
    }}
    .sources-title {{
      font-family: Georgia, 'Noto Serif TC', serif;
      font-size: 13px;
      font-weight: 700;
      color: var(--gold-hi);
      margin-bottom: 10px;
    }}
    .badge {{
      display: inline-block;
      background: var(--gold);
      color: var(--bg-panel);
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      font-size: 10px;
      font-weight: 800;
      letter-spacing: 0.12em;
      padding: 2px 7px;
      border-radius: 3px;
      margin-right: 8px;
    }}
    .sources-box ul {{ margin: 0; padding-left: 18px; }}
    .sources-box li {{ margin-bottom: 6px; font-size: 13px; }}
    .sources-box a {{ color: var(--gold-hi); text-decoration: none; }}
    .sources-box a:hover {{ text-decoration: underline; }}
    footer {{
      padding: 16px 24px;
      background: var(--bg-panel);
      border-top: 1px solid var(--gold);
      font-size: 12px;
      line-height: 1.65;
      color: #9EB5BF;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="kicker">PRE-MARKET INTELLIGENCE · {date_str} ({weekday_str})</div>
      <h1>盤前暖男台股財報</h1>
      <div class="gold-rule"><span>◆</span></div>
      <div class="focus-sub">焦點主軸：{html.escape(market_focus)} · 收盤基準 {html.escape(trade_date)}</div>
    </header>

    <section class="interactive-zone">
      <div class="zone-header">
        <div class="zone-title">
          ◆ 美股收盤親手翻牌區
          <small>點擊單張燙金牌卡揭曉最新數據，或按右側一鍵全翻</small>
        </div>
        <button id="toggleAllBtn" class="btn-gold" type="button">一鍵全部翻牌</button>
      </div>
      <div class="cards-grid">
        {cards_grid_html}
      </div>
    </section>

    <main>
      {body_html}
      {sources_html}
    </main>

    <footer>
      <p style="margin: 0 0 6px; color: #D8CEB8;">
        <strong style="color: #DFC388;">免責聲明：</strong>本電子報由 AI 結合即時網路搜尋整理而成，僅供市場政經資訊與題材研究參考，不構成任何買賣或投資建議。
      </p>
      <p style="margin: 0;">產生時間：{generated_at} (台灣時間) · 英倫 3D 互動翻牌版</p>
    </footer>
  </div>

  <script>
    const cards = Array.from(document.querySelectorAll('.flip-card'));
    const toggleBtn = document.getElementById('toggleAllBtn');

    function syncButtonLabel() {{
      const allFlipped = cards.length > 0 && cards.every(c => c.classList.contains('is-flipped'));
      toggleBtn.textContent = allFlipped ? '全部蓋牌重抽' : '一鍵全部翻牌';
    }}

    function toggleCard(card) {{
      const flipped = card.classList.toggle('is-flipped');
      card.setAttribute('aria-pressed', String(flipped));
      syncButtonLabel();
    }}

    cards.forEach(card => {{
      card.addEventListener('click', () => toggleCard(card));
      card.addEventListener('keydown', (e) => {{
        if (e.key === 'Enter' || e.key === ' ') {{
          e.preventDefault();
          toggleCard(card);
        }}
      }});
    }});

    toggleBtn.addEventListener('click', () => {{
      const allFlipped = cards.every(c => c.classList.contains('is-flipped'));
      cards.forEach((card, i) => {{
        setTimeout(() => {{
          card.classList.toggle('is-flipped', !allFlipped);
          card.setAttribute('aria-pressed', String(!allFlipped));
          syncButtonLabel();
        }}, i * 110);
      }});
    }});
  </script>
</body>
</html>
"""


def build_plain_text(report_md: str, sources: list[tuple[str, str]]) -> str:
    """建立純文字備援版本，供不支援 HTML 的信箱顯示（避免塞入大量轉址網址觸發垃圾信濾網）。"""
    lines = [report_md]
    if sources:
        unique_titles = list(dict.fromkeys(title for title, _ in sources))
        lines.append("\n---\n即時新聞查證來源：" + " / ".join(unique_titles))
    lines.append(
        "\n---\n免責聲明：本早報由 AI 結合即時搜尋整理，僅供市場資訊參考，不構成任何投資建議。"
    )
    return "\n".join(lines)


def send_gmail_bcc(
    config: dict[str, Any],
    subject: str,
    plain_text: str,
    html_content: str,
    inline_gifs: dict[str, bytes] | None = None,
) -> None:
    """透過 Gmail SMTP SSL (Port 465) 以 Bcc 密件副本發送含內嵌動態圖卡的 HTML 電子報。"""
    sender_email = config["sender_email"]
    app_password = config["gmail_app_password"]
    recipients: list[str] = config["recipients"]

    msg = MIMEMultipart("related")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = formataddr((str(Header("快樂猛男台股機器人", "utf-8")), sender_email))
    # To 僅填寄件者自身，不將訂閱名單寫入 To/Cc/Bcc 標頭，確保彼此完全看不到對方信箱
    msg["To"] = formataddr((str(Header("早報訂閱貴賓", "utf-8")), sender_email))
    msg["Reply-To"] = sender_email
    # 補齊標準 RFC 5322 標頭（Date / Message-ID），大幅降低被判為垃圾郵件的機率
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="gmail.com")

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain_text, "plain", "utf-8"))
    alt.attach(MIMEText(html_content, "html", "utf-8"))
    msg.attach(alt)

    for cid, gif_bytes in (inline_gifs or {}).items():
        if not gif_bytes:
            continue
        filename = f"{cid}.gif"
        img_part = MIMEImage(gif_bytes, _subtype="gif", name=filename)
        img_part.add_header("Content-ID", f"<{cid}>")
        img_part.add_header("X-Attachment-Id", cid)
        img_part.add_header("Content-Disposition", "inline", filename=filename)
        msg.attach(img_part)

    context = ssl.create_default_context()
    print(f"📨 正在連線 {SMTP_HOST}:{SMTP_PORT} (SSL)，以 Bcc 發送給 {len(recipients)} 位訂閱者...")

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as server:
        server.login(sender_email, app_password)
        # 透過 SMTP envelope 傳入所有收件人，達到真正的 Bcc 密件副本效果
        server.sendmail(sender_email, recipients, msg.as_string())

    print(f"✅ 電子報已成功寄出！（標題：{subject}，訂閱者共 {len(recipients)} 位）")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每日盤前政經與題材選股 Gmail 電子報機器人")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="僅產生報告並存成 preview_report.html，不實際寄出 Gmail。",
    )
    parser.add_argument(
        "--test-self",
        action="store_true",
        help="僅寄送測試信給 SENDER_EMAIL（自己），不寄給 RECIPIENT_EMAILS 中的其他人。",
    )
    return parser.parse_args()


def main() -> None:
    configure_console_encoding()
    args = parse_args()
    now_tw = get_taipei_now()
    date_str = now_tw.strftime("%Y/%m/%d")
    subject = f"【盤前暖男台股財報】{date_str} 今日關鍵題材與觀察名單"

    config = load_config(dry_run=args.dry_run, test_self=args.test_self)
    if args.test_self and not args.dry_run:
        print(f"🧪 單人測試模式：僅寄送給寄件者本人（{config['sender_email']}），不會寄給其他訂閱者。")
    report_md, sources = generate_daily_report(config, now_tw)

    metrics, trade_date = extract_us_market_metrics(report_md, now_tw)
    ticker_gif_bytes = generate_market_ticker_gif(
        metrics,
        trade_date,
        header_title="US MARKET SNAPSHOT",
        date_label=f"CLOSE  {trade_date}",
    )
    if ticker_gif_bytes:
        print(
            f"🎞️  已生成英倫風美股動態翻牌圖卡 GIF（{len(metrics)} 檔指標，{len(ticker_gif_bytes) / 1024:.1f} KB）。"
        )

    tw_picks = extract_tw_spotlight_stocks(report_md)
    tw_picks_gif_bytes = generate_market_ticker_gif(
        tw_picks,
        date_str,
        header_title="TW SPOTLIGHT PICKS",
        date_label=f"TOP 3  {date_str}",
    )
    if tw_picks_gif_bytes:
        print(
            f"🎞️  已生成英倫風台股重點關注三支股票 GIF（{len(tw_picks)} 檔個股，{len(tw_picks_gif_bytes) / 1024:.1f} KB）。"
        )

    # 同步輸出「可讓使用者親手點擊 3D 翻牌」的英倫互動網頁版（docs/index.html，可直上 GitHub Pages）
    interactive_html = render_interactive_flip_page(
        report_md=report_md,
        metrics=metrics,
        tw_picks=tw_picks,
        trade_date=trade_date,
        sources=sources,
        now_tw=now_tw,
        market_focus=config["market_focus"],
    )
    docs_dir = Path(__file__).resolve().parent / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    interactive_path = docs_dir / "index.html"
    interactive_path.write_text(interactive_html, encoding="utf-8")
    print(f"🃏 已生成英倫 3D 互動翻牌網頁版：{interactive_path}")

    plain_text = build_plain_text(report_md, sources)

    if args.dry_run:
        ticker_data_uri = (
            "data:image/gif;base64," + base64.b64encode(ticker_gif_bytes).decode("ascii")
            if ticker_gif_bytes
            else None
        )
        tw_picks_data_uri = (
            "data:image/gif;base64," + base64.b64encode(tw_picks_gif_bytes).decode("ascii")
            if tw_picks_gif_bytes
            else None
        )
        html_content = render_email_html(
            report_md,
            sources,
            now_tw,
            config["market_focus"],
            ticker_img_src=ticker_data_uri,
            tw_picks_img_src=tw_picks_data_uri,
            interactive_url=config["interactive_url"],
        )
        preview_path = Path(__file__).resolve().parent / "preview_report.html"
        preview_path.write_text(html_content, encoding="utf-8")
        print(f"🧪 Dry-run 完成！標題：{subject}")
        print(f"📄 已將預覽 HTML 儲存至：{preview_path}")
        print("\n" + "=" * 50 + "\n" + report_md + "\n" + "=" * 50)
        return

    inline_gifs: dict[str, bytes] = {}
    ticker_cid_src = None
    if ticker_gif_bytes:
        inline_gifs["market_ticker_gif"] = ticker_gif_bytes
        ticker_cid_src = "cid:market_ticker_gif"

    tw_picks_cid_src = None
    if tw_picks_gif_bytes:
        inline_gifs["tw_picks_gif"] = tw_picks_gif_bytes
        tw_picks_cid_src = "cid:tw_picks_gif"

    html_content = render_email_html(
        report_md,
        sources,
        now_tw,
        config["market_focus"],
        ticker_img_src=ticker_cid_src,
        tw_picks_img_src=tw_picks_cid_src,
        interactive_url=config["interactive_url"],
    )
    send_gmail_bcc(
        config,
        subject,
        plain_text,
        html_content,
        inline_gifs=inline_gifs,
    )


if __name__ == "__main__":
    main()
