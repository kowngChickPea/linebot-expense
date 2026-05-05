import os
import json
import base64
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage
from anthropic import Anthropic
from google.oauth2.service_account import Credentials
import gspread
from datetime import datetime

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
ANTHROPIC_API_KEY         = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID           = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS_JSON   = os.environ["GOOGLE_CREDENTIALS_JSON"]

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
claude       = Anthropic(api_key=ANTHROPIC_API_KEY)

def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"]
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID)

def get_or_create_worksheet(wb, title):
    try:
        return wb.worksheet(title)
    except Exception:
        ws = wb.add_worksheet(title=title, rows=500, cols=9)
        ws.append_row([
            "วันที่", "ประเภท", "รายการ/ลูกค้า",
            "จำนวน (แผง/กก.)", "หน่วย",
            "ราคาต่อหน่วย (บาท)", "รวมเงิน (บาท)",
            "โอนแล้ว", "หมายเหตุ"
        ])
        ws.format("A1:I1", {
            "backgroundColor": {"red": 0.27, "green": 0.51, "blue": 0.27},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "horizontalAlignment": "CENTER"
        })
        return ws

def append_sale(data: dict):
    wb    = get_sheet()
    month = datetime.now().strftime("%Y-%m")
    ws    = get_or_create_worksheet(wb, month)
    now   = datetime.now().strftime("%d/%m/%Y %H:%M")
    ws.append_row([
        now,
        data.get("type", ""),
        data.get("customer", ""),
        data.get("quantity", 0),
        data.get("unit", "แผง"),
        data.get("price_per_unit", 0),
        data.get("total", 0),
        "☐",
        data.get("note", ""),
    ])

def get_monthly_summary() -> str:
    try:
        wb    = get_sheet()
        month = datetime.now().strftime("%Y-%m")
        ws    = get_or_create_worksheet(wb, month)
        rows  = ws.get_all_records()

        egg_total    = sum(float(r["รวมเงิน (บาท)"]) for r in rows if r["ประเภท"] == "ขายไข่")
        manure_total = sum(float(r["รวมเงิน (บาท)"]) for r in rows if r["ประเภท"] == "ขายมูลไก่")
        expense_total= sum(float(r["รวมเงิน (บาท)"]) for r in rows if r["ประเภท"] == "รายจ่าย")
        egg_trays    = sum(float(r["จำนวน (แผง/กก.)"]) for r in rows if r["ประเภท"] == "ขายไข่")
        income_total = egg_total + manure_total
        profit       = income_total - expense_total

        customers: dict = {}
        for r in rows:
            if r["ประเภท"] == "ขายไข่":
                c = r.get("รายการ/ลูกค้า", "ไม่ระบุ")
                customers[c] = customers.get(c, 0) + float(r["รวมเงิน (บาท)"])

        cust_lines = "\n".join(
            f"  • {k}: {v:,.0f} บาท"
            for k, v in sorted(customers.items(), key=lambda x: -x[1])
        )
        sign = "+" if profit >= 0 else ""
        return (
            f"🐔 สรุปฟาร์มไก่ไข่ เดือน {month}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🥚 ขายไข่:     {egg_trays:>6.0f} แผง = {egg_total:>9,.0f} บาท\n"
            f"💩 ขายมูลไก่:              {manure_total:>9,.0f} บาท\n"
            f"💚 รายรับรวม:              {income_total:>9,.0f} บาท\n"
            f"❤️  รายจ่ายรวม:             {expense_total:>9,.0f} บาท\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"{'✅' if profit >= 0 else '⚠️'} กำไร/ขาดทุน: {sign}{profit:>8,.0f} บาท\n\n"
            f"👥 ลูกค้าแต่ละราย:\n{cust_lines}"
        )
    except Exception as e:
        return f"ยังไม่มีข้อมูลเดือนนี้ครับ ({e})"

SYSTEM_PROMPT = """คุณเป็นผู้ช่วยบันทึกรายรับรายจ่ายสำหรับ "ฟาร์มไก่ไข่"
เมื่อผู้ใช้ส่งข้อความมา ให้แปลงเป็น JSON เท่านั้น ห้ามตอบคำอื่น

คำสั่งพิเศษ:
- "สรุป", "ยอด", "summary" → {"action": "summary"}
- "help", "ช่วยเหลือ" → {"action": "help"}

รูปแบบ JSON สำหรับ ขายไข่:
{"type":"ขายไข่","customer":"ชื่อลูกค้า","quantity":จำนวนแผง,"unit":"แผง","price_per_unit":ราคาต่อแผง,"total":ยอดรวม,"note":""}

รูปแบบ JSON สำหรับ ขายมูลไก่:
{"type":"ขายมูลไก่","customer":"ชื่อลูกค้า","quantity":จำนวน,"unit":"กก.","price_per_unit":ราคา,"total":ยอดรวม,"note":""}

รูปแบบ JSON สำหรับ รายจ่าย (อาหารไก่ ค่าน้ำ ค่าไฟ วัคซีน ฯลฯ):
{"type":"รายจ่าย","customer":"รายการ","quantity":จำนวน,"unit":"หน่วย","price_per_unit":ราคา,"total":ยอดรวม,"note":""}

ตัวอย่าง:
- "ขายไข่น้องแจง 3 แผง 100" → {"type":"ขายไข่","customer":"น้องแจง","quantity":3,"unit":"แผง","price_per_unit":100,"total":300,"note":""}
- "พี่มา 2 แผง 115" → {"type":"ขายไข่","customer":"พี่มา","quantity":2,"unit":"แผง","price_per_unit":115,"total":230,"note":""}
- "ขายมูล 200 กก. กก.ละ 2" → {"type":"ขายมูลไก่","customer":"ทั่วไป","quantity":200,"unit":"กก.","price_per_unit":2,"total":400,"note":""}
- "ซื้ออาหารไก่ 5 ถุง ถุงละ 400" → {"type":"รายจ่าย","customer":"ค่าอาหารไก่","quantity":5,"unit":"ถุง","price_per_unit":400,"total":2000,"note":""}

คำนวณ total = quantity * price_per_unit ถ้าไม่ได้บอกตรงๆ
"""

IMAGE_PROMPT = """นี่คือรูปสลิปหรือใบเสร็จของฟาร์มไก่ไข่
อ่านข้อมูลและตอบกลับเป็น JSON เท่านั้น:
{"type":"ขายไข่/ขายมูลไก่/รายจ่าย","customer":"ชื่อ","quantity":จำนวน,"unit":"แผง/กก.","price_per_unit":ราคา,"total":ยอดรวม,"note":""}
"""

def parse_with_claude(text: str) -> dict:
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}]
    )
    return json.loads(resp.content[0].text.strip())

def parse_image_with_claude(image_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text",  "text": IMAGE_PROMPT}
        ]}]
    )
    return json.loads(resp.content[0].text.strip())

def format_reply(data: dict) -> str:
    emoji = {"ขายไข่": "🥚", "ขายมูลไก่": "💩", "รายจ่าย": "❤️"}.get(data["type"], "📝")
    ppu   = data.get("price_per_unit", 0)
    return (
        f"{emoji} บันทึกแล้วครับ!\n"
        f"━━━━━━━━━━━━━━\n"
        f"ประเภท:       {data['type']}\n"
        f"ลูกค้า/รายการ: {data['customer']}\n"
        f"จำนวน:        {data.get('quantity',0)} {data.get('unit','')}"
        + (f" × {ppu:,.0f} บาท" if ppu else "")
        + f"\nรวม:          {float(data.get('total',0)):,.0f} บาท"
        + (f"\nหมายเหตุ:     {data['note']}" if data.get("note") else "")
    )

HELP_TEXT = """🐔 ฟาร์มไก่ไข่ — วิธีใช้ Bot

🥚 ขายไข่:
• "ขายไข่น้องแจง 3 แผง 100"
• "พี่มา 2 แผง 115"
• "ผอ.เพื่อนแม่ 4 แผง 400"

💩 ขายมูลไก่:
• "ขายมูล 200 กก. กก.ละ 2"
• "มูลไก่ 5 กระสอบ 150"

💸 รายจ่าย:
• "ซื้ออาหารไก่ 5 ถุง ถุงละ 400"
• "ค่าไฟ 800 บาท"
• "วัคซีน 1200 บาท"

🖼️ ส่งรูปสลิป/ใบเสร็จ
→ Bot อ่านยอดให้อัตโนมัติ

📊 สรุปเดือนนี้:
• พิมพ์ "สรุป" หรือ "ยอด"

✅ ข้อมูลบันทึกลง Google Sheets ทันที"""

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
            append_sale(data)
            reply = format_reply(data)
    except Exception as e:
        reply = (
            "ขออภัยครับ ไม่เข้าใจรายการนี้\n"
            "ลองพิมพ์ใหม่ เช่น:\n"
            "• 'ขายไข่น้องแจง 3 แผง 100'\n"
            "• 'ขายมูล 200 กก. กก.ละ 2'\n\n"
            f"(error: {e})"
        )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    try:
        msg_content = line_bot_api.get_message_content(event.message.id)
        image_bytes = b"".join(msg_content.iter_content())
        data = parse_image_with_claude(image_bytes)
        append_sale(data)
        reply = format_reply(data) + "\n\n📎 บันทึกจากรูปภาพครับ"
    except Exception as e:
        reply = f"ขออภัยครับ อ่านรูปไม่ได้\nลองพิมพ์รายการแทนครับ\n\n(error: {e})"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@app.route("/", methods=["GET"])
def health():
    return "🐔 Egg Farm Bot is running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
