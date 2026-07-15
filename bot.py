
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# 3. Flask Server
app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

@app.route('/')
def home():
    # UptimeRobot ጥሪ ሲያደርግ ይህንን መልስ ያገኛል
    return "OK", 200

def run_health_server():
    # Render የሚሰጠውን ፖርት በትክክል መያዙን እናረጋግጣለን
    port = int(os.environ.get("PORT", 10000))
    logging.info(f"Starting Flask server on port {port}...")
    # use_reloader=False እና threaded=True መደረጉ በ thread ውስጥ እንዳይጋጭ ያደርገዋል
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

def extract_field(item, keyword):
    if not isinstance(item, dict): return ""
    for k, v in item.items():
        if k.lower() == keyword.lower():
            return v.get('name', v.get('title', str(v))) if isinstance(v, dict) else str(v)
    for k, v in item.items():
        if keyword.lower() in k.lower():
            return v.get('name', v.get('title', str(v))) if isinstance(v, dict) else str(v)
    return ""

# 4. መረጃ ከዌብሳይት መሳቢያ
async def scrape_website_cases():
    if not EMAIL or not PASSWORD:
        return [], "Error: EMAIL or PASSWORD environment variables missing!"

    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    api_url = 'https://api.tech24et.com/api/callentries?limit=200'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://tech24et.com',
        'Referer': 'https://tech24et.com/'
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0, verify=False) as session:
        try:
            await session.get(csrf_url)
            xsrf_token = session.cookies.get("XSRF-TOKEN")
            if xsrf_token: session.headers.update({'X-XSRF-TOKEN': urllib.parse.unquote(xsrf_token)})

            login_res = await session.post(login_url, json={'email': EMAIL.strip(), 'password': PASSWORD.strip()})
            if login_res.status_code not in [200, 201, 204]: return [], "Login failed!"

            response = await session.get(api_url)
            if response.status_code != 200: return [], "Failed to fetch data!"

            data = response.json()
            cases_list = data.get('data', []) if isinstance(data, dict) else data

            scraped_cases = []
            for item in cases_list:
                if not item or not isinstance(item, dict): continue
                if "adama" in str(item).lower():
                    status = extract_field(item, 'status') or extract_field(item, 'progress') or "Pending"
                    status_text = "Completed" if status.lower() in ["complete", "completed", "1", "done", "closed"] else "Pending"
                    
                    scraped_cases.append({
                        'case_id': str(item.get('callentry_id', item.get('id', 'N/A'))),
                        'terminal': extract_field(item, 'terminal') or extract_field(item, 'atm_id') or "N/A",
                        'bank': extract_field(item, 'bank') or "Awash",
                        'branch': extract_field(item, 'branch') or "Adama Branch",
                        'issue': extract_field(item, 'description') or extract_field(item, 'issue') or "No Description",
                        'status': status_text,
                        'district': "Adama",
                        'atm_name': extract_field(item, 'atm_name') or extract_field(item, 'terminal') or "N/A",
                        'comment': extract_field(item, 'comment') or extract_field(item, 'note') or "None",
                        'technician': extract_field(item, 'technician') or extract_field(item, 'assigned_to') or "Unassigned",
                        'tech_phone': extract_field(item, 'phone') or "N/A",
                        'date': str(item.get('created_at', ''))[:19].replace('T', ' ')
                    })
            return scraped_cases, "OK"
        except Exception as e:
            return [], f"Error: {str(e)}"

# 5. ዳሽቦርድ ላይ ኬዝን Terminate (መዝጊያ) ተግባር
async def terminate_case_on_dashboard(case_id):
    terminate_url = f'https://api.tech24et.com/api/callentries/{case_id}/close'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20.0, verify=False) as session:
        try:
            await session.get('https://api.tech24et.com/sanctum/csrf-cookie')
            xsrf = session.cookies.get("XSRF-TOKEN")
            if xsrf: session.headers.update({'X-XSRF-TOKEN': urllib.parse.unquote(xsrf)})
            await session.post('https://api.tech24et.com/api/login', json={'email': EMAIL.strip(), 'password': PASSWORD.strip()})
            res = await session.post(terminate_url, json={'status': 'completed'})
            return res.status_code in [200, 201, 204]
        except:
            return False

# 6. UI Builder
def build_case_detail_ui(case):
    text = (
        f"Case ID: {case['case_id']}\n"
        f"Terminal: {case['terminal']}\n"
        f"Bank: {case['bank']}\n"
        f"Branch: {case['branch']}\n"
        f"Issue: {case['issue']}\n"
        f"Status: {case['status']}\n"
        f"District: {case['district']}\n"
        f"ATM Name: {case['atm_name']}\n"
        f"Comment: {case['comment']}\n"
        f"Technician: {case['technician']}\n"
        f"Technician Phone: Phone: {case['tech_phone']}\n"
        f"Reported At: {case['date']} (East Africa Time)\n"
    )
    keyboard = [
        [InlineKeyboardButton("🛑 Terminate", callback_data=f"terminate_{case['case_id']}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{case['case_id']}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

# 7. ቦት ኮማንዶች
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋👋👋Welcome to Tech24 Adama District Bot👋👋👋\n The Bot is Active Know")

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cases, status_msg = await scrape_website_cases()
    if status_msg != "OK": return await update.message.reply_text("❌ Error connecting to dashboard.")

    pending_cases = [c for c in cases if c['status'] == "Pending"]

    if not pending_cases:
        keyboard = [[InlineKeyboardButton("🗂 Check in dashboard ↗️", url="https://tech24et.com")]]
        await update.message.reply_text(
            "no pending or ongoing cases in the **Adama** district. You can check the dashboard for confirmation or further details.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if len(pending_cases) == 1:
        text, markup = build_case_detail_ui(pending_cases[0])
        await update.message.reply_text(text, reply_markup=markup)
    else:
        keyboard = []
        for case in pending_cases:
            btn_text = f"{case['case_id']} | {case['bank']} | {case['branch']} | ⏳"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"show_{case['case_id']}")])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
        await update.message.reply_text(
            "The following ATM cases have been reported and are currently pending action. "
            "select a case from the list below to view details and proceed with resolution.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# 8. Excel Export
async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing_msg = await update.message.reply_text("⏳ Generating Excel Report...")
    cases, status = await scrape_website_cases()
    
    if not cases:
        return await processing_msg.edit_text("❌ No data available to export.")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ATM Cases Report"
    
    headers = ["Case ID", "Bank", "Branch", "Terminal", "Issue", "Status", "Technician", "Reported At", "Comment"]
    ws.append(headers)
    
    for c in cases:
        ws.append([c['case_id'], c['bank'], c['branch'], c['terminal'], c['issue'], c['status'], c['technician'], c['date'], c['comment']])

    excel_file = BytesIO()
    wb.save(excel_file)
    excel_file.seek(0)
    
    current_month = datetime.datetime.now().strftime("%B %Y")
    filename = f"case-report-2026-{datetime.datetime.now().strftime('%B').lower()}.xlsx"
    
    caption = (
        f"📊 ATM Cases Report – {current_month}\n\n"
        f"This report contains all ATM cases reported for {current_month}. "
        f"Use this data to monitor ATM downtime and track technician performance."
    )

    await processing_msg.delete()
    await update.message.reply_document(document=excel_file, filename=filename, caption=caption)

# 9. Callback Buttons
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel_action":
        await query.message.delete()
        return

    if data.startswith("show_"):
        case_id = data.split("_")[1]
        cases, _ = await scrape_website_cases()
        selected = next((c for c in cases if c['case_id'] == case_id), None)
        if selected:
            text, markup = build_case_detail_ui(selected)
            await query.edit_message_text(text, reply_markup=markup)
        else:
            await query.edit_message_text("❌ Case not found.")

    elif data.startswith("refresh_"):
        case_id = data.split("_")[1]
        cases, _ = await scrape_website_cases()
        selected = next((c for c in cases if c['case_id'] == case_id), None)
        if selected:
            text, markup = build_case_detail_ui(selected)
            try:
                await query.edit_message_text(text, reply_markup=markup)
                await query.answer("🔄 Data Refreshed!")
            except:
                pass

    elif data.startswith("terminate_"):
        case_id = data.split("_")[1]
        await query.answer("⏳ Terminating Case...", show_alert=True)
        
        success = await terminate_case_on_dashboard(case_id)
        if success:
            await query.edit_message_text(f"✅ Case `{case_id}` has been successfully TERMINATED on the dashboard.", parse_mode="Markdown")
        else:
            await query.edit_message_text(
                f"⚠️ Attempted to terminate Case `{case_id}`, but couldn't verify success on the dashboard. Please check manually.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗂 Open Dashboard", url="https://tech24et.com")]])
            )

def main():
    # Flaskን በ Thread ማስነሳት
    threading.Thread(target=run_health_server, daemon=True).start()
    
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("monthly", export_command))
    application.add_handler(CallbackQueryHandler(button_click_handler))

    logging.info("Bot is starting...")
    application.run_polling()

if __name__ == '__main__':
    main()
