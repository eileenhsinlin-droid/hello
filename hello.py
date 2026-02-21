from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    ImageMessage,
    AudioMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent
)

import random
import os, requests, csv, traceback, time
from io import StringIO
import tempfile
from mutagen import File as MutagenFile
from collections import OrderedDict

app = Flask(__name__)

# ===========================
#  SDK v3 設定 (改動區)
# ===========================
access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
channel_secret = os.getenv("LINE_CHANNEL_SECRET")

configuration = Configuration(access_token=access_token)
handler = WebhookHandler(channel_secret)

# ===========================
#  Google Sheet 與 快取設定 (維持不變)
# ===========================
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/1IPm1wGgYKgU5iB0nRUrzYji8ZH_HKsvl5r031gNWnac/export?format=csv"

SHEET_CACHE = []
SHEET_LAST_FETCH = 0
SHEET_TTL = 30 

AUDIO_DURATION_CACHE = {}

user_cache = OrderedDict()
MAX_USERS = 800


def get_sheet_rows():
    """5 分鐘抓一次 Google Sheet"""
    global SHEET_CACHE, SHEET_LAST_FETCH
    now = time.time()

    if SHEET_CACHE and now - SHEET_LAST_FETCH < SHEET_TTL:
        return SHEET_CACHE

    try:
        res = requests.get(SHEET_CSV_URL, timeout=10)
        res.raise_for_status()

        decoded_content = res.content.decode("utf-8-sig")
        f = StringIO(decoded_content)
        reader = csv.DictReader(f)

        SHEET_CACHE = list(reader)
        SHEET_LAST_FETCH = now
        return SHEET_CACHE
    except Exception:
        traceback.print_exc()
        return []


def get_audio_duration_ms(url):
    """音檔長度快取（同一首只算一次）"""
    if url in AUDIO_DURATION_CACHE:
        return AUDIO_DURATION_CACHE[url]

    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=True) as tmp:
            tmp.write(res.content)
            tmp.flush()
            audio = MutagenFile(tmp.name)
            if audio and audio.info:
                duration = int(audio.info.length * 1000)
                AUDIO_DURATION_CACHE[url] = duration
                return duration
    except Exception as e:
        print("Audio duration error:", e)

    AUDIO_DURATION_CACHE[url] = 3000
    return 3000


def get_images(keyword):
    """搜尋 Google Sheet"""
    try:
        rows = get_sheet_rows()
        results = []

        keyword_clean = keyword.replace(" ", "").lower()
        if not keyword_clean:
            return []

        use_artist = keyword_clean.startswith(("/", "／", "∕"))
        random_pick = keyword_clean.startswith("🎲")

        if use_artist:
            keyword_clean = keyword_clean[1:]

        if random_pick and rows:
            picked = random.choice(rows)
            return [{
                "no": picked["編號"],
                "keyword": picked["說話的內容"],
                "url": picked["圖片網址"],
                "episode": picked["影片資訊"],
                "audio": picked.get("音檔", "").strip(),
                "artist": picked.get("藝人", "")
            }]

        for row in rows:
            kw = row.get("藝人" if use_artist else "說話的內容", "").strip().lower()
            if all(ch in kw for ch in keyword_clean):
                results.append({
                    "no": row["編號"],
                    "keyword": row["說話的內容"],
                    "url": row["圖片網址"],
                    "episode": row["影片資訊"],
                    "audio": row.get("音檔", "").strip(),
                    "artist": row.get("藝人", "")
                })

        return results
    except Exception:
        traceback.print_exc()
        return []


# ===========================
#  Flask 路由 (改動區)
# ===========================

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

#與Cronitor連動喚醒
@app.route("/ping", methods=["GET"])
def ping():
    return "OK", 200

# 輔助函式：統一回覆訊息用 (v3 寫法)
def reply(reply_token, messages):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=messages
            )
        )

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    user_id = event.source.user_id
    user_input = event.message.text.strip()
    reply_token = event.reply_token

    last_results = user_cache.get(user_id, [])

    # ===== 數字選擇圖片 =====
    if user_input.isdigit():
        if last_results:
            selected = [r for r in last_results if r["no"] == user_input]
            if selected:
                data = selected[0]
                
                # 建構 v3 訊息模型
                msgs = [
                    ImageMessage(
                        original_content_url=data["url"],
                        preview_image_url=data["url"]
                    ),
                    TextMessage(text=f"集數資訊：{data['episode']}")
                ]
                
                if data.get("audio"):
                    duration = get_audio_duration_ms(data["audio"])
                    msgs.append(AudioMessage(
                        original_content_url=data["audio"],
                        duration=duration
                    ))
                
                reply(reply_token, msgs)
                return

        reply(reply_token, [TextMessage(text="沒有這張圖片餒！")])
        return

    # ===== 關鍵字搜尋 =====
    results = get_images(user_input)

    user_cache[user_id] = results
    user_cache.move_to_end(user_id)
    if len(user_cache) > MAX_USERS:
        user_cache.popitem(last=False)

    if results:
        if len(results) == 1:
            data = results[0]
            msgs = [
                ImageMessage(
                    original_content_url=data["url"],
                    preview_image_url=data["url"]
                ),
                TextMessage(text=f"集數資訊：{data['episode']}")
            ]
            if data.get("audio"):
                duration = get_audio_duration_ms(data["audio"])
                msgs.append(AudioMessage(
                    original_content_url=data["audio"],
                    duration=duration
                ))
            reply(reply_token, msgs)
            return

        # 情況 B: 多筆結果 -> 列表顯示
        reply_messages = []
        current_text = "請輸入圖片編號以查看圖片：\n"
        MAX_CHARS = 4000 

        for data in results:
            line = f"{data['no']}. {data['keyword']}\n"
            
            if len(current_text) + len(line) > MAX_CHARS:
                reply_messages.append(TextMessage(text=current_text.strip()))
                current_text = ""
                
                if len(reply_messages) >= 5:
                    current_text = "結果太多，僅顯示前 5 頁內容..."
                    break

            current_text += line

        if current_text and len(reply_messages) < 5:
            reply_messages.append(TextMessage(text=current_text.strip()))

        reply(reply_token, reply_messages)
    else:
        reply(reply_token, [TextMessage(text="沒有這張圖片餒！")])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
