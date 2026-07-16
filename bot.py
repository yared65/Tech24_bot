import os
import logging
import asyncio
import threading
import urllib.parse
import ast
import json
import datetime
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
logger = logging.getLogger(__name__)

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
logging.getLogger('werkzeug').setLevel(logging.ERROR)

@app.route('/')
def home():
    return "OK", 200

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Flask server on port {port}...")
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

# ==========================================
# 4. UTILITY DATA PARSERS (ዳታ እንዳይዘበራረቅ ማድረጊያ)
# ==========================================
def extract_field(item, keyword):
    """የተዘበራረቁ stringified dictionaries-ን በትክክል ፈልቅቆ ያወጣል"""
    if not isinstance(item, dict): 
        return ""
    
    val = None
    for k, v in item.items():
        if k.lower() == keyword.lower():
            val = v
            break
    if val is None:
        for k, v in item.items():
            if keyword.lower() in k.lower():
                val = v
                break

    if val is None:
        return ""

    # በፅሁፍ የመጣን ዲክሽነሪ ወደ እውነተኛ ዲክሽነሪ መቀየር
    if isinstance(val, str) and (val.startswith('{') or val.startswith('[')):
        try:
            val = ast.literal_eval(val)
        except Exception:
            try:
                val = json.loads(val)
            except Exception:
                pass

    if isinstance(val, dict):
        return val.get('name', val.get('title', val.get('bank_name', val.get('branch_name', val.get('atmterminalname', str(val))))))
    
    return str(val)

# ==========================================
# 5. DASHBOARD API SCRAPING & MUTATION (419 Fixed)
# ==========================================
async def scrape_website_cases():
    if not EMAIL or not PASSWORD:
        return [], "Configuration Error: EMAIL or PASSWORD missing!"

    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    api_url = 'https://api.tech24et.com/api/callentries?limit=200'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://tech24et.com',
        'Referer': 'https://tech24et.com/'
    }

    # በ httpx.AsyncClient አማካኝነት ሴሽኑን እና ኩኪውን አጥብቆ እንዲይዝ እናደርጋለን
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0, verify=False) as client:
        try:
            # 1. CSRF Cookie መውሰድ
            csrf_res = await client.get(csrf_url)
            
            cookies_dict = {}
            for header_name, header_val in csrf_res.headers.multi_items():
                if header_name.lower() == 'set-cookie':
                    parts = header_val.split(';')[0].split('=', 1)
                    if len(parts) == 2:
                        cookies_dict[parts[0].strip()] = parts[1].strip()

            xsrf_token = cookies_dict.get("XSRF-TOKEN") or client.cookies.get("XSRF-TOKEN")
            if not xsrf_token:
                return [], "CSRF handshake failed (Token missing)."

            decoded_token = urllib.parse.unquote(xsrf_token)
            client.headers.update({
                'X-XSRF-TOKEN': decoded_token,
                'X-CSRF-TOKEN': decoded_token,
            })
            
            cookie_header_val = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
            client.headers.update({'Cookie': cookie_header_val})

            # 2. ሎጊን መግባት
            login_payload = {'email': EMAIL.strip(), 'password': PASSWORD.strip()}
            login_res = await client.post(login_url, json=login_payload)
            
            if login_res.status_code not in [200, 201, 204]: 
                return [], f"Login failed with status {login_res.status_code}"

            # ከሎጊን በኋላ የመጣውን አዲስ ኩኪ ማደስ (419 እንዳይመጣ ዋናው ሚስጥር)
            for header_name, header_val in login_res.headers.multi_items():
                if header_name.lower() == 'set-cookie':
                    parts = header_val.split(';')[0].split('=', 1)
                    if len(parts) == 2:
                        cookies_dict[parts[0].strip()] = parts[1].strip()

            if cookies_dict.get("XSRF-TOKEN"):
                new_decoded = urllib.parse.unquote(cookies_dict["XSRF-TOKEN"])
                client.headers.update({
                    'X-XSRF-TOKEN': new_decoded,
                    'X-CSRF-TOKEN': new_decoded,
                })
            
            cookie_header_val = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
            client.headers.update({'Cookie': cookie_header_val})

            # 3. መረጃውን መሳብ
            response = await client.get(api_url)
            if response.status_code != 200: 
                return [], f"Failed to fetch data. Status: {response.status_code}"

            data = response.json()
            cases_list = data.get('data', []) if isinstance(data, dict) else data

            scraped_cases = []
            for item in cases_list:
                if not item or not isinstance(item, dict): 
                    continue
                
                # ለአዳማ ብቻ ማጣሪያ (strict filter for Adama)
                item_str = str(item).lower()
                if "adama" in item_str and "adama district" not in item_str:
                    status = extract_field(item, 'status') or extract_field(item, 'progress') or "Pending"
                    status_text = "Completed" if status.lower() in ["complete", "completed", "1", "done", "closed"] else "Pending"
                    
                    # ንፁህ እና የተስተካከለ ዳታ ማዋቀር
                    scraped_cases.append({
                        'case_id': str(item.get('callentry_id', item.get('id', 'N/A'))),
                        'terminal': extract_field(item, 'terminal') or extract_field(item, 'atm_id') or "N/A",
                        'bank': extract_field(item, 'bank') or "Awash",
                        'branch': extract_field(item, 'branch') or "Adama Branch",
                        'issue': extract_field(item, 'description') or extract_field(item, 'issue') or "No Description",
                        'status': status_text,
                        'district': "Adama",
                        'atm_name': extract_field(item, 'atm_name') or "N/A",
                        'comment': extract_field(item, 'comment') or extract_field(item, 'note') or "None",
                        'technician': extract_field(item, 'technician') or extract_field(item, 'assigned_to') or "Unassigned",
                        'tech_phone': extract_field(item, 'phone') or "N/A",
                        'date': str(item.get('created_at', ''))[:19].replace('T', ' ')
                    })
            return scraped_cases, "OK"
        except Exception as e:
            return [], f"Error: {str(e)}"

async def terminate_case_on_dashboard(case_id):
    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    terminate_url = f'https://api.tech24et.com/api/callentries/{case_id}/close'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://tech24et.com',
        'Referer': 'https://tech24et.com/'
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20.0, verify=False) as client:
        try:
            csrf_res = await client.get(csrf_url)
            cookies_dict = {}
            for header_name, header_val in csrf_res.headers.multi_items():
                if header_name.lower() == 'set-cookie':
                    parts = header_val.split(';')[0].split('=', 1)
                    if len(parts) == 2:
                        cookies_dict[parts[0].strip()] = parts[1].strip()

            xsrf = cookies_dict.get("XSRF-TOKEN") or client.cookies.get("XSRF-TOKEN")
            if xsrf: 
                client.headers.update({'X-XSRF-TOKEN': urllib.parse.unquote(xsrf)})
                
            cookie_header_val = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
            client.headers.update({'Cookie': cookie_header_val})
                
            login_res = await client.post(login_url, json={'email': EMAIL.strip(), 'password': PASSWORD.strip()})
            if login_res.status_code not in [200, 201, 204]:
                return False
            
            res = await client.post(terminate_url, json={'status': 'completed'})
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
# 7. TELEGRAM COMMAND HANDLERS
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
            except:
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

    logger.info("Bot starting up...")
    application.run_polling()

if __name__ == '__main__':
    main()
