import os
import json
import base64
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, ImageMessage,
    TextSendMessage
)
from anthropic import Anthropic
from google.oauth2.service_account import Credentials
import gspread
from datetime import datetime

app = Flask(__name__)

# ─── ตั้งค่า API Keys (อ่านจาก Environment Variables) ───
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
ANTHROPIC_API_KEY         = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID           = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS_JSON   = os.environ["GOOGLE_CREDENTIALS_JSON"]  # JSON string

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
claude       = Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── Google Sheets ───
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"]
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID)

def append_to_sheet(data: dict):
    """เพิ่มแถวข้อมูลลง Google Sheets"""
    wb    = get_sheet()
    month = datetime.now().strftime("%Y-%m")

    # หา worksheet ของเดือนนี้ ถ้าไม่มีให้สร้างใหม่
    try:
        ws = wb.worksheet(month)
    except Exception:
        ws = wb.add_worksheet(title=month, rows=500, cols=7)
        ws.append_row(["วันที่", "ประเภท", "หมวดหมู่",
                        "รายละเอียด", "จำนวนเงิน (บาท)", "หมายเหตุ", "รูปภาพ"])

    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    ws.append_row([
        now,
        data.get("type", ""),
        data.get("category", ""),
        data.get("description", ""),
        data.get("amount", 0),
        data.get("note", ""),
        data.get("image_url", ""),
    ])

def get_monthly_summary() -> str:
    """ดึงสรุปรายเดือนจาก Google Sheets"""
    try:
        wb    = get_sheet()
        month = datetime.now().strftime("%Y-%m")
        ws    = wb.worksheet(month)
        rows  = ws.get_all_records()

        income  = sum(float(r["จำนวนเงิน (บาท)"]) for r in rows if r["ประเภท"] == "รายรับ")
        expense = sum(float(r["จำนวนเงิน (บาท)"]) for r in rows if r["ประเภท"] == "รายจ่าย")
        balance = income - expense

        # สรุปตามหมวดหมู่
        categories: dict = {}
        for r in rows:
            cat = r.get("หมวดหมู่", "อื่นๆ")
            categories[cat] = categories.get(cat, 0) + float(r["จำนวนเงิน (บาท)"])

        cat_lines = "\n".join(
            f"  • {k}: {v:,.0f} บาท" for k, v in sorted(categories.items(), key=lambda x: -x[1])
        )

        sign = "+" if balance >= 0 else ""
        return (
            f"📊 สรุปเดือน {month}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💚 รายรับรวม:  {income:>10,.0f} บาท\n"
            f"❤️  รายจ่ายรวม: {expense:>10,.0f} บาท\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{'✅' if balance >= 0 else '⚠️'} คงเหลือ:     {sign}{balance:>9,.0f} บาท\n\n"
            f"📂 แยกตามหมวดหมู่:\n{cat_lines}"
        )
    except Exception as e:
        return f"ยังไม่มีข้อมูลเดือนนี้ครับ ({e})"

# ─── Claude: แปลข้อความเป็น JSON รายรับ/รายจ่าย ───
SYSTEM_PROMPT = """คุณเป็นผู้ช่วยบันทึกรายรับรายจ่ายส่วนตัว
เมื่อผู้ใช้ส่งข้อความมา ให้แปลงเป็น JSON เท่านั้น ห้ามตอบคำอื่น
ยกเว้นคำสั่งพิเศษ: "สรุป", "ยอด", "summary" ให้ตอบว่า {"action": "summary"}
และ "ช่วยเหลือ", "help" ให้ตอบว่า {"action": "help"}

รูปแบบ JSON สำหรับรายการ:
{
  "type": "รายรับ" หรือ "รายจ่าย",
  "category": "หมวดหมู่ เช่น อาหาร, เดินทาง, เงินเดือน, ค่าน้ำไฟ, บันเทิง, สุขภาพ, อื่นๆ",
  "description": "รายละเอียดสั้นๆ",
  "amount": ตัวเลขเท่านั้น ไม่มีหน่วย,
  "note": "หมายเหตุถ้ามี"
}

ตัวอย่าง:
- "จ่ายค่าข้าว 80"  → {"type":"รายจ่าย","category":"อาหาร","description":"ค่าข้าว","amount":80,"note":""}
- "รับเงินเดือน 25000" → {"type":"รายรับ","category":"เงินเดือน","description":"เงินเดือน","amount":25000,"note":""}
- "ซื้อยา 350 บาท ที่ร้านขายยา" → {"type":"รายจ่าย","category":"สุขภาพ","description":"ซื้อยา","amount":350,"note":"ร้านขายยา"}
"""

IMAGE_PROMPT = """นี่คือรูปสลิปการโอนเงินหรือใบเสร็จ
กรุณาอ่านข้อมูลและตอบกลับเป็น JSON เท่านั้น รูปแบบเดียวกับที่กำหนด:
{
  "type": "รายจ่าย",
  "category": "หมวดหมู่ที่เหมาะสม",
  "description": "ชื่อร้าน/รายการที่จ่าย",
  "amount": ยอดเงิน (ตัวเลขเท่านั้น),
  "note": "ข้อมูลเพิ่มเติมจากสลิปถ้ามี"
}
"""

def parse_with_claude(text: str) -> dict:
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}]
    )
    raw = resp.content[0].text.strip()
    return json.loads(raw)

def parse_image_with_claude(image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text",  "text": IMAGE_PROMPT}
            ]
        }]
    )
    raw = resp.content[0].text.strip()
    return json.loads(raw)

def format_reply(data: dict) -> str:
    emoji = "💚" if data["type"] == "รายรับ" else "❤️"
    return (
        f"{emoji} บันทึกแล้วครับ!\n"
        f"━━━━━━━━━━━━\n"
        f"ประเภท: {data['type']}\n"
        f"หมวดหมู่: {data['category']}\n"
        f"รายละเอียด: {data['description']}\n"
        f"จำนวน: {float(data['amount']):,.0f} บาท"
        + (f"\nหมายเหตุ: {data['note']}" if data.get("note") else "")
    )

HELP_TEXT = """🤖 วิธีใช้งาน:

💬 พิมพ์รายการ เช่น:
• "จ่ายค่าข้าว 80"
• "ค่าน้ำมัน 500 บาท"
• "รับเงินเดือน 25000"
• "ซื้อกาแฟ 65 บาท"

🖼️ ส่งรูปสลิป/ใบเสร็จ
→ ระบบจะอ่านยอดให้อัตโนมัติ

📊 ดูสรุปเดือนนี้:
• พิมพ์ "สรุป" หรือ "ยอด"

ข้อมูลจะบันทึกลง Google Sheets ทันที ✅"""

# ─── Line Webhook ───
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body      = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_text = event.message.text.strip()
    try:
        data = parse_with_claude(user_text)

        if data.get("action") == "summary":
            reply = get_monthly_summary()
        elif data.get("action") == "help":
            reply = HELP_TEXT
        else:
            append_to_sheet(data)
            reply = format_reply(data)
    except Exception as e:
        reply = f"ขออภัยครับ ไม่เข้าใจรายการนี้\nลองพิมพ์ใหม่ เช่น 'จ่ายค่าข้าว 80 บาท'\n\n(error: {e})"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    try:
        # ดาวน์โหลดรูปจาก Line
        msg_content = line_bot_api.get_message_content(event.message.id)
        image_bytes = b"".join(msg_content.iter_content())

        # ให้ Claude อ่านสลิป
        data = parse_image_with_claude(image_bytes)
        data["image_url"] = f"line://message/{event.message.id}"

        append_to_sheet(data)
        reply = format_reply(data) + "\n\n📎 บันทึกจากรูปสลิปครับ"
    except Exception as e:
        reply = f"ขออภัยครับ อ่านรูปไม่ได้\nลองส่งรูปใหม่ หรือพิมพ์รายการแทนครับ\n\n(error: {e})"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@app.route("/", methods=["GET"])
def health():
    return "Line Expense Bot is running! 💰"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
