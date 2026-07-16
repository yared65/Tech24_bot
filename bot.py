import os
import logging
import asyncio
import threading
import urllib.parse
import datetime
import ast
import json
from io import BytesIO
from flask import Flask
import httpx
import openpyxl
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ==========================================
# 1. LOGGING SETUP
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ==========================================
# 2. ENVIRONMENT VARIABLES
# ==========================================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")

# ==========================================
# 3. FLASK HEALTH-CHECK SERVER (For Uptime)
# ==========================================
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

# ==========================================
# 4. UTILITY DATA PARSERS (ዳታ እንዳይዘበራረቅ ፈልቅቆ ማውጫ)
# ==========================================
def clean_and_parse_value(val):
    """የመጣውን ማንኛውንም አይነት የተዘበራረቀ ጽሑፍ ወደ ዲክሽነሪ ይቀይራል"""
    if not val:
        return ""
    
    # እውነተኛ ዲክሽነሪ ከሆነ በቀጥታ እንመልሰው
    if isinstance(val, dict):
        return val
        
    # በጽሑፍ የተጻፈ ዲክሽነሪ ከሆነ ፈልቅቀን እንውሰደው
    if isinstance(val, str) and (val.strip().startswith('{') or val.strip().startswith('[')):
        try:
            return ast.literal_eval(val.strip())
        except Exception:
            try:
                return json.loads(val.strip())
            except Exception:
                pass
    return val

def extract_field(item, field_name):
    """ከዳታው ውስጥ ባንክ፣ ቅርንጫፍ፣ ተርሚናልን ለይቶ ንፁህ ጽሑፍ ያወጣል"""
    val = item.get(field_name)
    
    # መጀመሪያ እሴቱን አጽድተን ዲክሽነሪ መሆኑን እንፈትሻለን
    parsed_val = clean_and_parse_value(val)
    
    if isinstance(parsed_val, dict):
        # በሁለተኛው ፎቶ መሰረት ያሉትን ቁልፎች በሙሉ እንፈትሻለን
        for key in ['bankname', 'branchname', 'atmterminalname', 'issuecatname', 'name', 'title']:
            if key in parsed_val:
                return str(parsed_val[key])
        # ምንም ቁልፍ ካልተገኘ ሙሉውን ዲክሽነሪ ወደ ጽሑፍ ቀይረን ከመላክ እሴቱን እንፈልጋለን
        return str(list(parsed_val.values())[1]) if len(parsed_val) > 1 else str(parsed_val)
        
    return str(parsed_val) if parsed_val is not None else ""

# ==========================================
# 5. DASHBOARD API SCRAPING (የሁለተኛው ኮድ የሎጊን መንገድ)
# ==========================================
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
            if xsrf_token: 
                session.headers.update({'X-XSRF-TOKEN': urllib.parse.unquote(xsrf_token)})

            login_res = await session.post(login_url, json={'email': EMAIL.strip(), 'password': PASSWORD.strip()})
            if login_res.status_code not in [200, 201, 204]: 
                return [], f"Login failed with status {login_res.status_code}!"

            response = await session.get(api_url)
            if response.status_code != 200: 
                return [], "Failed to fetch data!"

            data = response.json()
            cases_list = data.get('data', []) if isinstance(data, dict) else data

            scraped_cases = []
            for item in cases_list:
                if not item or not isinstance(item, dict): 
                    continue
                
                item_str = str(item).lower()
                # ለአዳማ ብቻ ማጣሪያ (strict filter for Adama)
                if "adama" in item_str and "adama district" not in item_str:
                    status_raw = clean_and_parse_value(item.get('status') or item.get('progress')) or "Pending"
                    status_text = "Completed" if str(status_raw).lower() in ["complete", "completed", "1", "done", "closed"] else "Pending"
                    
                    scraped_cases.append({
                        'case_id': str(item.get('callentry_id', item.get('id', 'N/A'))),
                        'terminal': extract_field(item, 'terminal') or "N/A",
                        'bank': extract_field(item, 'bank') or "Awash",
                        'branch': extract_field(item, 'branch') or "Adama Branch",
                        'issue': extract_field(item, 'issuecat') or extract_field(item, 'description') or "No Description",
                        'status': status_text,
                        'district': "Adama",
                        'atm_name': extract_field(item, 'atm_name') or "N/A",
                        'comment': extract_field(item, 'comment') or "None",
                        'technician': extract_field(item, 'technician') or "Unassigned",
                        'tech_phone': extract_field(item, 'phone') or "N/A",
                        'date': str(item.get('created_at', ''))[:19].replace('T', ' ')
                    })
            return scraped_cases, "OK"
        except Exception as e:
            return [], f"Error: {str(e)}"

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
            if xsrf: 
                session.headers.update({'X-XSRF-TOKEN': urllib.parse.unquote(xsrf)})
            await session.post('https://api.tech24et.com/api/login', json={'email': EMAIL.strip(), 'password': PASSWORD.strip()})
            res = await session.post(terminate_url, json={'status': 'completed'})
            return res.status_code in [200, 201, 204]
        except:
            return False

# ==========================================
# 6. UI BUILDER (ንፁህ ውፅዓት ማሳያ)
# ==========================================
def build_case_detail_ui(case):
    text = (
        f"📋 **Case ID Details:** {case['case_id']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🏦 **Bank:** {case['bank']}\n"
        f"🏛 **Branch:** {case['branch']}\n"
        f"📟 **Terminal:** {case['terminal']} (Name: {case['atm_name']})\n"
        f"🚨 **Issue Details:** {case['issue']}\n"
        f"⚙️ **District:** {case['district']}\n"
        f"🛡 **Status:** {case['status']}\n"
        f"💬 **Comments:** {case['comment']}\n"
        f"👤 **Tech Assigned:** {case['technician']} ({case['tech_phone']})\n"
        f"⏰ **Logged Time:** {case['date']} (EAT)\n"
    )
    keyboard = [
        [InlineKeyboardButton("🛑 Terminate Case", callback_data=f"terminate_{case['case_id']}")],
        [InlineKeyboardButton("🔄 Refresh Details", callback_data=f"refresh_{case['case_id']}")],
        [InlineKeyboardButton("❌ Close Card", callback_data="cancel_action")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

# ==========================================
# 7. COMMAND HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Welcome to Tech24 Adama Bot**\n\n"
        "💻 **Available Commands:**\n"
        "• /pending   - View all current open/unresolved cases\n"
        "• /export    - Generate spreadsheet database dump\n"
        "• /monthly   - Generate monthly case report"
    )

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cases, status_msg = await scrape_website_cases()
    if status_msg != "OK": 
        return await update.message.reply_text(f"❌ **Connection Error**: {status_msg}")

    pending_cases = [c for c in cases if c['status'] == "Pending"]

    if not pending_cases:
        keyboard = [[InlineKeyboardButton("🗂 Check Dashboard ↗️", url="https://tech24et.com")]]
        await update.message.reply_text(
            "✅ **All clear!** No pending or ongoing cases found in **Adama**.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if len(pending_cases) == 1:
        text, markup = build_case_detail_ui(pending_cases[0])
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        keyboard = []
        for case in pending_cases:
            btn_text = f"⏳ {case['case_id']} | {case['bank']} - {case['branch']}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"show_{case['case_id']}")])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
        await update.message.reply_text(
            "⏳ **Pending ATM Cases:**\nSelect any case entry below to view logs:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

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
    filename = f"case-report-{datetime.datetime.now().strftime('%Y-%b').lower()}.xlsx"
    
    caption = f"📊 **ATM Cases Report – {current_month}**"

    await processing_msg.delete()
    await update.message.reply_document(document=excel_file, filename=filename, caption=caption)

# ==========================================
# 8. CALLBACK BUTTON HANDLER
# ==========================================
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel_action":
        await query.message.delete()
        return

    if data.startswith("show_") or data.startswith("refresh_"):
        case_id = data.split("_")[1]
        cases, _ = await scrape_website_cases()
        selected = next((c for c in cases if c['case_id'] == case_id), None)
        if selected:
            text, markup = build_case_detail_ui(selected)
            try:
                await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
            except Exception as e:
                pass
        else:
            await query.edit_message_text("❌ Case file could not be found.")

    elif data.startswith("terminate_"):
        case_id = data.split("_")[1]
        await query.answer("⏳ Dispatched request...", show_alert=False)
        
        success = await terminate_case_on_dashboard(case_id)
        if success:
            await query.edit_message_text(f"✅ **Success!** Case `{case_id}` has been closed on the dashboard.", parse_mode="Markdown")
        else:
            await query.edit_message_text(
                f"⚠️ **Update Failed:** Tried to complete Case `{case_id}` but could not confirm backend validation.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗂 Open Dashboard", url="https://tech24et.com")]])
            )

# ==========================================
# 9. MAIN RUNNER
# ==========================================
def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("monthly", export_command))
    application.add_handler(CallbackQueryHandler(button_click_handler))

    logging.info("Bot starting up...")
    application.run_polling()

if __name__ == '__main__':
    main()
