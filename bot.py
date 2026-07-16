import os
import logging
import asyncio
import threading
import json
from datetime import datetime, timedelta
from io import BytesIO
from flask import Flask
import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ==========================================
# 1. LOGGING & SYSTEM CONFIGURATION
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")
NOTIFICATION_CHAT_ID = os.environ.get("NOTIFICATION_CHAT_ID")

SENT_CASES_TRACKER = set()

# ==========================================
# 2. FLASK SERVER - FIXES 404 UPTIME ERRORS
# ==========================================
app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Catch-all route: UptimeRobot ወይም ሌላ ሞኒተር ሊንኩን በማንኛውም መልኩ ቢጠራው 404 እንዳይሰጥ 200 OK ይመልሳል
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def home(path):
    return "Tech24 Bot is Live and Healthy!", 200

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Flask keep-alive server on port {port}...")
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

# ==========================================
# 3. JSON PARSING HELPERS (CLEAN UI TEXTS)
# ==========================================
def safe_parse_json(val):
    if not val:
        return {}
    if isinstance(val, dict):
        return val
    try:
        if isinstance(val, str):
            cleaned = val.replace("'", '"').replace("None", "null").replace("True", "true").replace("False", "false")
            return json.loads(cleaned)
    except Exception:
        pass
    return {}

def extract_field(item, keyword):
    if not item:
        return ""
    if isinstance(item, dict):
        for k, v in item.items():
            if k.lower() == keyword.lower() or keyword.lower() in k.lower():
                return v.get('name', v.get('title', str(v))) if isinstance(v, dict) else str(v)
    
    parsed = safe_parse_json(item)
    if parsed:
        for k, v in parsed.items():
            if k.lower() == keyword.lower() or keyword.lower() in k.lower():
                return v.get('name', v.get('title', str(v))) if isinstance(v, dict) else str(v)
                
    if isinstance(item, str):
        return item
    return ""

# ==========================================
# 4. API SCRAPER & PRODUCTION ACTIONS
# ==========================================
async def scrape_website_cases():
    if not EMAIL or not PASSWORD:
        return [], "Configuration Error: Missing login credentials."

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
            login_payload = {'email': EMAIL.strip(), 'password': PASSWORD.strip()}
            login_res = await session.post(login_url, json=login_payload)
            
            if login_res.status_code not in [200, 201, 204]: 
                return [], f"Login failed with status {login_res.status_code}"

            response = await session.get(api_url)
            if response.status_code != 200: 
                return [], f"API data fetch failed. Status: {response.status_code}"

            data = response.json()
            cases_list = data.get('data', []) if isinstance(data, dict) else data

            scraped_cases = []
            for item in cases_list:
                if not item or not isinstance(item, dict): 
                    continue
                
                if "adama" in str(item).lower():
                    status_raw = extract_field(item, 'status') or extract_field(item, 'progress') or "Pending"
                    status_text = "Completed" if status_raw.lower() in ["complete", "completed", "1", "done", "closed"] else "Pending"
                    
                    date_str = str(item.get('created_at', ''))[:19].replace('T', ' ')
                    date_obj = datetime.now()
                    try:
                        date_obj = datetime.strptime(date_str.split(" ")[0], "%Y-%m-%d")
                    except:
                        pass

                    scraped_cases.append({
                        'case_id': str(item.get('callentry_id', item.get('id', 'N/A'))),
                        'terminal': extract_field(item, 'terminal') or "N/A",
                        'bank': extract_field(item, 'bank') or "Awash",
                        'branch': extract_field(item, 'branch') or "Adama Branch",
                        'issue': extract_field(item, 'description') or extract_field(item, 'issue') or "No Description",
                        'status': status_text,
                        'district': "Adama",
                        'atm_name': extract_field(item, 'atm_name') or "N/A",
                        'comment': extract_field(item, 'comment') or extract_field(item, 'note') or "None",
                        'technician': extract_field(item, 'technician') or "Unassigned",
                        'tech_phone': extract_field(item, 'phone') or "N/A",
                        'date': date_str,
                        'date_obj': date_obj
                    })
            return scraped_cases, "OK"
        except Exception as e:
            logger.error(f"Scraper error: {str(e)}")
            return [], str(e)

async def terminate_case_on_dashboard(case_id):
    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    terminate_url = f'https://api.tech24et.com/api/callentries/{case_id}/close'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Origin': 'https://tech24et.com',
        'Referer': 'https://tech24et.com/'
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20.0, verify=False) as session:
        try:
            await session.get(csrf_url)
            login_res = await session.post(login_url, json={'email': EMAIL.strip(), 'password': PASSWORD.strip()})
            if login_res.status_code not in [200, 201, 204]:
                return False
            
            res = await session.post(terminate_url, json={'status': 'completed'})
            return res.status_code in [200, 201, 204]
        except Exception as e:
            logger.error(f"Failed to close case {case_id}: {e}")
            return False

# ==========================================
# 5. AUTOMATIC 10-MINUTE MONITOR ENGINE
# ==========================================
async def auto_monitor_dashboard(context: ContextTypes.DEFAULT_TYPE):
    if not NOTIFICATION_CHAT_ID:
        return

    cases, status = await scrape_website_cases()
    if status != "OK":
        return

    pending_cases = [c for c in cases if c['status'] == "Pending"]
    
    for case in pending_cases:
        if case['case_id'] not in SENT_CASES_TRACKER:
            text = (
                f"📋 **Case ID Details:** {case['case_id']}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🏦 **Bank:** {case['bank']}\n"
                f"🏛 **Branch:** {case['branch']}\n"
                f"📟 **Terminal:** {case['terminal']}\n"
                f"🚨 **Issue:** {case['issue']}\n"
                f"👤 **Technician Assigned:** {case['technician']}\n"
            )
            keyboard = [[InlineKeyboardButton("🛑 Terminate Case", callback_data=f"terminate_{case['case_id']}")]]
            
            try:
                await context.bot.send_message(
                    chat_id=NOTIFICATION_CHAT_ID,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                SENT_CASES_TRACKER.add(case['case_id'])
            except Exception as e:
                logger.error(f"Error sending auto notification: {e}")

# ==========================================
# 6. UI BUILDER LOGIC FOR PENDING COMMANDS
# ==========================================
def build_case_detail_ui(case):
    text = (
        f"📋 **Case ID Details:** {case['case_id']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🏦 **Bank:** {case['bank']}\n"
        f"🏛 **Branch:** {case['branch']}\n"
        f"📟 **Terminal:** {case['terminal']} ({case['atm_name']})\n"
        f"🚨 **Issue:** {case['issue']}\n"
        f"🛡 **Status:** {case['status']}\n"
        f"💬 **Comment:** {case['comment']}\n"
        f"👤 **Technician:** {case['technician']} ({case['tech_phone']})\n"
        f"⏰ **Time:** {case['date']}\n"
    )
    keyboard = [
        [InlineKeyboardButton("🛑 Terminate Case", callback_data=f"terminate_{case['case_id']}")],
        [InlineKeyboardButton("🔄 Refresh Details", callback_data=f"refresh_{case['case_id']}")],
        [InlineKeyboardButton("❌ Close Card", callback_data="cancel_action")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

async def send_pending_cases_ui(chat_id, context: ContextTypes.DEFAULT_TYPE):
    cases, status = await scrape_website_cases()
    if status != "OK":
        return await context.bot.send_message(chat_id=chat_id, text=f"❌ Error: {status}")

    pending = [c for c in cases if c['status'] == "Pending"]
    if not pending:
        return await context.bot.send_message(chat_id=chat_id, text="✅ **All clear!** No pending cases in Adama.")

    if len(pending) == 1:
        text, markup = build_case_detail_ui(pending[0])
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode="Markdown")
    else:
        keyboard = []
        for c in pending:
            btn_text = f"⏳ {c['case_id']} | {c['bank']} - {c['branch']}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"show_{c['case_id']}")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
        
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ **Pending ATM Cases List:**\nSelect an incident entry below to view logs:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

# ==========================================
# 7. FORMATTED REPORT ENGINE (WEEKLY & MONTHLY)
# ==========================================
def format_summary_report(cases, title, days_limit=7):
    now = datetime.now()
    cutoff_date = now - timedelta(days=days_limit)
    filtered = [c for c in cases if c['date_obj'] >= cutoff_date]

    if not filtered:
        return f"⚠️ No registered cases matching the {title.lower()} period filter."

    tech_name = "Adama Tech Team"
    for c in filtered:
        if c['technician'] != "Unassigned" and c['technician'] != "":
            tech_name = c['technician']
            break

    lines = [f"🏧 {title}  report /{tech_name}/"]
    
    stats = {}
    for c in filtered:
        display_date = c['date'].split(" ")[0]
        try:
            dt = datetime.strptime(display_date, "%Y-%m-%d")
            display_date = dt.strftime("%d/%m/%Y")
        except:
            pass
            
        line_item = f"®️{display_date} Registered | {c['branch']} | {c['bank']} | ({c['issue']}) | {c['status']}"
        lines.append(line_item)

        bank = c['bank'].strip()
        if bank not in stats:
            stats[bank] = {"registered": 0, "completed": 0}
        stats[bank]["registered"] += 1
        if c['status'] == "Completed":
            stats[bank]["completed"] += 1

    lines.append("\n       Generally ")
    for bank_name, data in stats.items():
        lines.append(f" 🏛 {bank_name} Registered ")
        lines.append(f"           Completed - {data['completed']}")
        pending_count = data['registered'] - data['completed']
        if pending_count > 0:
            lines.append(f"           Pending - {pending_count}")

    return "\n".join(lines)

# ==========================================
# 8. TELEGRAM COMMAND HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 Welcome to Tech24 Adama District Bot \n\n"
        "💻 Available Commands: \n"
        "• /pending - View all current open/unresolved cases\n"
        "• /terminate - Access list of cases to quickly terminate/complete\n"
        "• /report - View structured Weekly summary report\n"
        "• /monthly - View structured Monthly summary report\n"
        "• /export - Generate and download spreadsheet raw database dump"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_pending_cases_ui(update.effective_chat.id, context)

async def terminate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cases, status = await scrape_website_cases()
    if status != "OK":
        return await update.message.reply_text("❌ Error fetching active cases.")
        
    pending = [c for c in cases if c['status'] == "Pending"]
    if not pending:
        return await update.message.reply_text("✨ No open cases require termination right now.")

    keyboard = []
    for c in pending:
        keyboard.append([InlineKeyboardButton(f"🛑 Complete Case {c['case_id']} ({c['bank']})", callback_data=f"terminate_{c['case_id']}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
    
    await update.message.reply_text(
        "🛠 **Termination Action Center:**\nChoose an operational live case entry to close on the remote dashboard:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cases, status = await scrape_website_cases()
    if status != "OK":
        return await update.message.reply_text(f"❌ Error compiling report: {status}")
    report_out = format_summary_report(cases, "Weekly", 7)
    await update.message.reply_text(report_out)

async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cases, status = await scrape_website_cases()
    if status != "OK":
        return await update.message.reply_text(f"❌ Error compiling report: {status}")
    report_out = format_summary_report(cases, "Monthly", 30)
    await update.message.reply_text(report_out)

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    proc = await update.message.reply_text("⏳ Compiling and cleaning spreadsheet logs...")
    cases, status = await scrape_website_cases()
    if not cases:
        return await proc.edit_text("❌ Data dump returned an empty grid framework.")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ATM Incident Log"
    ws.views.sheetView[0].showGridLines = True
    
    headers = ["Case ID", "Bank", "Branch", "Terminal ID", "Issue Description", "Resolution Status", "Assigned Technician", "Creation Timestamp", "Notes/Comments"]
    ws.append(headers)
    
    for c in cases:
        ws.append([c['case_id'], c['bank'], c['branch'], c['terminal'], c['issue'], c['status'], c['technician'], c['date'], c['comment']])

    excel_file = BytesIO()
    wb.save(excel_file)
    excel_file.seek(0)
    
    await proc.delete()
    await update.message.reply_document(
        document=excel_file,
        filename=f"atm-report-{datetime.now().strftime('%Y-%m-%d')}.xlsx",
        caption="📊 **ATM Operational Log Export**\nAutomated worksheet database dump updated successfully."
    )

# ==========================================
# 9. INLINE BUTTON CALLBACK ROUTER
# ==========================================
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
            await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")

    elif data.startswith("refresh_"):
        case_id = data.split("_")[1]
        cases, _ = await scrape_website_cases()
        selected = next((c for c in cases if c['case_id'] == case_id), None)
        if selected:
            text, markup = build_case_detail_ui(selected)
            try:
                await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
            except:
                pass

    elif data.startswith("terminate_"):
        case_id = data.split("_")[1]
        success = await terminate_case_on_dashboard(case_id)
        if success:
            await query.edit_message_text(f"✅ **Success!** Case `{case_id}` has been safely marked closed on the remote database.", parse_mode="Markdown")
        else:
            await query.edit_message_text(f"⚠️ **Update Validation Failed:** Tried to close Case `{case_id}` but server did not authorize execution.", parse_mode="Markdown")

# ==========================================
# 10. BOT MENU INITIALIZER
# ==========================================
async def post_init(application: Application) -> None:
    logger.info("Setting bot commands menu during system startup sequence...")
    commands = [
        BotCommand("start", "Initialize your session"),
        BotCommand("pending", "View open/unresolved cases"),
        BotCommand("terminate", "Access list of cases to terminate"),
        BotCommand("report", "View structured Weekly summary report"),
        BotCommand("monthly", "View structured Monthly summary report"),
        BotCommand("export", "Generate and download database spreadsheet")
    ]
    await application.bot.set_my_commands(commands)

# ==========================================
# 11. ENGINE POLLING LOOP INITIALIZATION
# ==========================================
def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("terminate", terminate_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("monthly", monthly_command))
    application.add_handler(CommandHandler("export", export_command))
    
    application.add_handler(CallbackQueryHandler(button_click_handler))

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(auto_monitor_dashboard, interval=600, first=10)

    logger.info("Production engine successfully deployed. Initiating polling loop hooks...")
    application.run_polling()

if __name__ == '__main__':
    main()
