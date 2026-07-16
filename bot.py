import os
import logging
import asyncio
import threading
import urllib.parse
import datetime
from io import BytesIO
from flask import Flask
import httpx
import openpyxl
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# 1. ሎጊንግ (Logging)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# 2. የአካባቢ ተለዋዋጮች
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")

# 3. Flask Server (Keep-Alive)
app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

@app.route('/')
def home():
    return "OK", 200

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    logging.info(f"Starting Flask server on port {port}...")
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

# 4. ረዳት የዳታ ማጣሪያ ተግባራት (JSON & Field Parser)
def extract_field(item, keyword):
    if not isinstance(item, dict):
        return ""
    for k, v in item.items():
        if k.lower() == keyword.lower():
            if isinstance(v, dict):
                return extract_field(v, keyword)
            return str(v)
        elif isinstance(v, dict):
            res = extract_field(v, keyword)
            if res:
                return res
    return ""

def clean_json_string(text):
    """
    ከተዝረከረከ JSON ጽሑፍ ውስጥ ንጹህ ስሞችን ብቻ ያወጣል።
    ምሳሌ፦ {"name": "Awash"} -> Awash
    """
    if not text:
        return "N/A"
    text_str = str(text).strip()
    if text_str.startswith('{') and text_str.endswith('}'):
        try:
            import json
            data = json.loads(text_str.replace("'", '"'))
            for key in ['name', 'title', 'label', 'value', 'branch_name', 'bank_name']:
                if key in data:
                    return str(data[key])
        except Exception:
            pass
    return text_str

def is_case_closed(item):
    """
    ኬዙ በትክክል መዘጋቱን/ማለቁን ያረጋግጣል።
    ከሚከተሉት አንዱ True ከሆነ ኬዙ አልቋል (Pending አይደለም) ተብሎ ይታለፋል፦
    """
    # 1. 'is_closed' ቁልፍን መፈተሽ
    is_closed_val = extract_field(item, "is_closed")
    if is_closed_val.lower() in ["true", "1", "yes"]:
        return True

    # 2. 'status' ወይም 'progress' ወይም 'step' መፈተሽ
    for key in ["status", "progress", "step", "state"]:
        val = extract_field(item, key).lower()
        if any(closed_word in val for closed_word in ["close", "resolve", "complete", "done", "finish"]):
            return True
            
    return False

# 5. API Scraper & Token Manager
class APIScraper:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.token = None
        self.token_expiry = None

    async def get_token(self):
        if self.token and self.token_expiry and datetime.datetime.now() < self.token_expiry:
            return self.token

        login_url = "https://api.bcm.superapp.et/api/auth/login"
        payload = {"email": self.email, "password": self.password}
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.post(login_url, json=payload)
                if response.status_code == 200:
                    data = response.json()
                    self.token = data.get("token") or data.get("data", {}).get("token")
                    self.token_expiry = datetime.datetime.now() + datetime.timedelta(hours=2)
                    logging.info("Successfully retrieved API token.")
                    return self.token
                else:
                    logging.error(f"Failed to get token: Status {response.status_code}")
            except Exception as e:
                logging.error(f"Token request exception: {e}")
        return None

    async def fetch_cases(self):
        token = await self.get_token()
        if not token:
            return []
        
        url = "https://api.bcm.superapp.et/api/cases"
        headers = {"Authorization": f"Bearer {token}"}
        
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list):
                        return data
                    elif isinstance(data, dict):
                        return data.get("data", []) or data.get("cases", [])
                else:
                    logging.error(f"Fetch cases failed: Status {response.status_code}")
            except Exception as e:
                logging.error(f"Fetch cases exception: {e}")
        return []

scraper = APIScraper(EMAIL, PASSWORD)
sent_cases = set()  # የተላኩ ኬዞችን ID መያዣ

# 6. የ 10 ደቂቃ Auto-Monitor 🔄
async def auto_monitor(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    logging.info("Auto-monitoring check started...")
    
    cases = await scraper.fetch_cases()
    if not cases:
        return

    new_pending_found = False

    for item in cases:
        # ኬዙ ያለቀበት ከሆነ ፈጽሞ Pending ውስጥ አናስገባውም
        if is_case_closed(item):
            continue

        case_id = extract_field(item, "id") or extract_field(item, "_id")
        if not case_id or case_id in sent_cases:
            continue

        # ንጹህ መረጃዎችን መፈልቀቅ
        bank = clean_json_string(extract_field(item, "bank"))
        branch = clean_json_string(extract_field(item, "branch"))
        terminal_id = clean_json_string(extract_field(item, "terminal_id") or extract_field(item, "terminal"))
        issue = extract_field(item, "issue") or extract_field(item, "description") or "Not Specified"
        technician = extract_field(item, "technician") or "Not Assigned"
        phone = extract_field(item, "phone") or extract_field(item, "telephone") or "N/A"
        reported_time = extract_field(item, "reported_time") or extract_field(item, "created_at") or "N/A"

        # ማራኪ የቴሌግራም UI ፎርማት
        message = (
            f"🔔 *New Pending Case Detected!*\n\n"
            f"🏛 *Bank:* {bank}\n"
            f"📍 *Branch:* {branch}\n"
            f"🖥 *Terminal ID:* {terminal_id}\n"
            f"⚠️ *Issue:* {issue}\n"
            f"👤 *Technician:* {technician}\n"
            f"📞 *Phone:* {phone}\n"
            f"🕒 *Reported Time:* {reported_time}\n"
        )
        
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="Markdown"
            )
            sent_cases.add(case_id)
            new_pending_found = True
            await asyncio.sleep(1) # አይፒአይ እንዳይጨናነቅ
        except Exception as e:
            logging.error(f"Error sending message for case {case_id}: {e}")

    if not new_pending_found:
        logging.info("No new active pending cases found.")

# 7. የቴሌግራም ቦት ትዕዛዞች (Commands)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # የቆዩ የሞኒተር ስራዎች ካሉ ማጽዳት
    current_jobs = context.job_queue.get_jobs_by_name(f"monitor_{chat_id}")
    for j in current_jobs:
        j.schedule_removal()
        
    # አዲስ በየ 10 ደቂቃው የሚሰራ ስራ መጀመር (600 ሰከንድ)
    context.job_queue.run_repeating(
        auto_monitor,
        interval=600,
        first=1,
        chat_id=chat_id,
        name=f"monitor_{chat_id}"
    )

    welcome_text = (
        "👋 *Welcome to the BCM Monitoring Bot!*\n\n"
        "🔄 *Auto-monitor is now ACTIVE.*\n"
        "The bot will check the dashboard every *10 minutes* and alert you ONLY when a new pending case arrives.\n\n"
        "💡 *Use the Menu below or commands to navigate!*"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("📊 Summary Report", callback_data="btn_report"),
            InlineKeyboardButton("📅 Monthly Report", callback_data="btn_monthly")
        ],
        [
            InlineKeyboardButton("📥 Export Active Pending", callback_data="btn_export")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=reply_markup)

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generate_summary_report(update, context)

async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generate_monthly_report(update, context)

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generate_excel_export(update, context)

# 8. የሪፖርት ማመንጫ ሎጂኮች
async def generate_summary_report(update_or_query, context):
    is_cb = isinstance(update_or_query, CallbackQueryHandler) or hasattr(update_or_query, "data")
    chat_id = update_or_query.message.chat_id if is_cb else update_or_query.effective_chat.id
    
    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔄 Fetching data & generating report...")
    
    cases = await scraper.fetch_cases()
    if not cases:
        await status_msg.edit_text("❌ No data retrieved from the server.")
        return

    # ማጠቃለያዎችን መስራት (እውነተኛ Pending ብቻ)
    pending_count = 0
    bank_summary = {}

    for item in cases:
        if is_case_closed(item):
            continue
        
        pending_count += 1
        bank_name = clean_json_string(extract_field(item, "bank"))
        bank_summary[bank_name] = bank_summary.get(bank_name, 0) + 1

    report_text = f"📊 *BCM Summary Report*\n"
    report_text += f"📅 *Generated on:* {datetime.date.today().strftime('%B %d, %Y')}\n"
    report_text += f"━━━━━━━━━━━━━━━━━━━━━\n\n"
    report_text += f"🔴 *Total Active Pending Cases:* `{pending_count}`\n\n"
    report_text += f"🏛 *Pending Cases by Bank:*\n"
    
    if bank_summary:
        for bank, count in bank_summary.items():
            report_text += f"• *{bank}:* `{count}`\n"
    else:
        report_text += "_No active pending cases._\n"
        
    report_text += f"\n━━━━━━━━━━━━━━━━━━━━━\n"
    
    await status_msg.edit_text(text=report_text, parse_mode="Markdown")

async def generate_monthly_report(update_or_query, context):
    is_cb = isinstance(update_or_query, CallbackQueryHandler) or hasattr(update_or_query, "data")
    chat_id = update_or_query.message.chat_id if is_cb else update_or_query.effective_chat.id
    
    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔄 Preparing monthly report...")
    
    cases = await scraper.fetch_cases()
    if not cases:
        await status_msg.edit_text("❌ Failed to fetch data.")
        return

    now = datetime.datetime.now()
    current_month_cases = 0
    resolved_this_month = 0
    pending_this_month = 0

    for item in cases:
        rep_time_str = extract_field(item, "reported_time") or extract_field(item, "created_at")
        if not rep_time_str:
            continue
        try:
            # የዓመቱንና ወሩን መፈተሽ
            rep_date = datetime.datetime.strptime(rep_time_str.split("T")[0], "%Y-%m-%d")
            if rep_date.year == now.year and rep_date.month == now.month:
                current_month_cases += 1
                if is_case_closed(item):
                    resolved_this_month += 1
                else:
                    pending_this_month += 1
        except Exception:
            # ፎርማቱ የተለየ ከሆነ ወደ ቀጣዩ ማለፍ
            continue

    monthly_text = (
        f"📅 *Monthly Performance Report*\n"
        f"📆 *Month:* {now.strftime('%B %Y')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📥 *Total Received:* `{current_month_cases}`\n"
        f"✅ *Resolved/Closed:* `{resolved_this_month}`\n"
        f"⏳ *Active Pending:* `{pending_this_month}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
    )
    await status_msg.edit_text(text=monthly_text, parse_mode="Markdown")

async def generate_excel_export(update_or_query, context):
    is_cb = isinstance(update_or_query, CallbackQueryHandler) or hasattr(update_or_query, "data")
    chat_id = update_or_query.message.chat_id if is_cb else update_or_query.effective_chat.id
    
    status_msg = await context.bot.send_message(chat_id=chat_id, text="📥 Generating Excel spreadsheet...")
    
    cases = await scraper.fetch_cases()
    if not cases:
        await status_msg.edit_text("❌ No data to export.")
        return

    # Excel ፋይል አዘገጃጀት
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Active Pending Cases"
    
    # ራስጌዎች (Headers)
    headers = ["Bank", "Branch", "Terminal ID", "Issue Description", "Technician", "Phone Number", "Reported Time"]
    ws.append(headers)

    # ውሂብ መሙላት
    row_count = 0
    for item in cases:
        if is_case_closed(item):
            continue
        
        bank = clean_json_string(extract_field(item, "bank"))
        branch = clean_json_string(extract_field(item, "branch"))
        terminal_id = clean_json_string(extract_field(item, "terminal_id") or extract_field(item, "terminal"))
        issue = extract_field(item, "issue") or extract_field(item, "description") or "N/A"
        technician = extract_field(item, "technician") or "N/A"
        phone = extract_field(item, "phone") or extract_field(item, "telephone") or "N/A"
        reported_time = extract_field(item, "reported_time") or extract_field(item, "created_at") or "N/A"

        ws.append([bank, branch, terminal_id, issue, technician, phone, reported_time])
        row_count += 1

    if row_count == 0:
        await status_msg.edit_text("ℹ️ No active pending cases to export at this moment.")
        return

    # ፎርማቱን ማስተካከያ (Auto-fit columns)
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    # ፋይሉን ወደ ቴሌግራም መላክ
    file_stream = BytesIO()
    wb.save(file_stream)
    file_stream.seek(0)
    
    try:
        await context.bot.send_document(
            chat_id=chat_id,
            document=file_stream,
            filename=f"Active_Pending_Cases_{datetime.date.today()}.xlsx",
            caption=f"📊 Here is the active pending cases list. Total: {row_count} cases."
        )
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ Error sending Excel file: {e}")

# 9. የትዕዛዝ ቁልፎች (Callback Query Handler)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "btn_report":
        await generate_summary_report(query, context)
    elif query.data == "btn_monthly":
        await generate_monthly_report(query, context)
    elif query.data == "btn_export":
        await generate_excel_export(query, context)

# 10. ቦቱ ሲነሳ ሜኑዎችን በእንግሊዝኛ ብቻ መጫን (No Terminate Menu)
async def post_init(application: Application) -> None:
    # የድሮ ሜኑዎችን በሙሉ ማጽዳት
    await application.bot.delete_my_commands()
    
    # 6ቱ ዋና ዋና ሜኑዎች ብቻ በእንግሊዝኛ መጫን (Terminate የለበትም)
    commands = [
        BotCommand("start", "Start monitoring & show menu"),
        BotCommand("report", "Get daily/weekly summary report"),
        BotCommand("monthly", "Get monthly performance report"),
        BotCommand("export", "Export pending cases to Excel"),
    ]
    await application.bot.set_my_commands(commands)
    logging.info("Bot commands successfully updated to English only (Terminate removed).")

# 11. ዋናው ማስነሻ (Main Run)
def main():
    # Flask Health server በሌላ Thread ላይ ማስጀመር
    threading.Thread(target=run_health_server, daemon=True).start()

    if not BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN environment variable is missing!")
        return

    # የቴሌግራም አፕሊኬሽን መገንባት
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # ትዕዛዞችን ማገናኘት
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("monthly", monthly_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CallbackQueryHandler(button_handler))

    logging.info("Telegram Bot starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
