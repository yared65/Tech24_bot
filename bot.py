import os
import logging
import asyncio
import threading
import urllib.parse
import ast
import json
import re
from datetime import datetime, timedelta
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
# 2. ENVIRONMENT VARIABLES & GLOBALS
# ==========================================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")

ACTIVE_CHATS = set()
NOTIFIED_CASES = set()

# ==========================================
# 3. FLASK HEALTH-CHECK SERVER (For Uptime)
# ==========================================
app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

@app.route('/')
def home():
    return "Bot is alive and running!", 200

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Flask keep-alive server on port {port}...")
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

# ==========================================
# 4. UTILITY DATA PARSERS
# ==========================================
def extract_field(item, keyword):
    """Safely extracts nested fields and cleans up raw string-dict structures."""
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

    if isinstance(val, str) and (val.startswith('{') or val.startswith('[')):
        try:
            val = ast.literal_eval(val)
        except Exception:
            try:
                val = json.loads(val)
            except Exception:
                pass

    if isinstance(val, dict):
        return val.get('name', val.get('title', val.get('bank_name', val.get('branch_name', str(val)))))
    
    return str(val)

# ==========================================
# 5. DASHBOARD API SCRAPING & MUTATION (STRICT SESSION FIX)
# ==========================================
async def scrape_website_cases():
    """Authenticates using stateful manual cookie parsing to prevent 419 CSRF errors."""
    if not EMAIL or not PASSWORD:
        return [], "Configuration Error: Missing login credentials in Environment Variables."

    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    api_url = 'https://api.tech24et.com/api/callentries?limit=200'
    
    # Strict web-browser simulation headers
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest', # CRITICAL: Tells Laravel this is an AJAX/API request
        'Origin': 'https://tech24et.com',
        'Referer': 'https://tech24et.com/',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0, verify=False) as session:
        try:
            # Step 1: Fetch the CSRF Cookie
            csrf_res = await session.get(csrf_url)
            
            # Manually extract cookies from response headers to bypass client-side domain limitations
            cookies_dict = {}
            for header, val in csrf_res.headers.multi_items():
                if header.lower() == 'set-cookie':
                    cookie_parts = val.split(';')[0].split('=', 1)
                    if len(cookie_parts) == 2:
                        cookies_dict[cookie_parts[0].strip()] = cookie_parts[1].strip()

            xsrf_token = cookies_dict.get("XSRF-TOKEN")
            if not xsrf_token:
                # Fallback to standard cookie jar if manual extract yielded nothing
                xsrf_token = session.cookies.get("XSRF-TOKEN")

            if not xsrf_token:
                return [], "CSRF Token could not be initialized from the platform."

            # Step 2: Set XSRF Headers and include gathered cookies
            decoded_token = urllib.parse.unquote(xsrf_token)
            session.headers.update({
                'X-XSRF-TOKEN': decoded_token,
                'X-CSRF-TOKEN': decoded_token,
                'Cookie': "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
            })

            # Step 3: Perform Login
            login_payload = {'email': EMAIL.strip(), 'password': PASSWORD.strip()}
            login_res = await session.post(login_url, json=login_payload)
            
            if login_res.status_code not in [200, 201, 204]: 
                return [], f"Login failed with status {login_res.status_code}"

            # Step 4: Refresh cookies list with the authenticated Session token
            for header, val in login_res.headers.multi_items():
                if header.lower() == 'set-cookie':
                    cookie_parts = val.split(';')[0].split('=', 1)
                    if len(cookie_parts) == 2:
                        cookies_dict[cookie_parts[0].strip()] = cookie_parts[1].strip()

            # Apply refreshed cookies & potential updated tokens
            if cookies_dict.get("XSRF-TOKEN"):
                decoded_updated = urllib.parse.unquote(cookies_dict["XSRF-TOKEN"])
                session.headers.update({
                    'X-XSRF-TOKEN': decoded_updated,
                    'X-CSRF-TOKEN': decoded_updated
                })
            
            session.headers.update({
                'Cookie': "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
            })

            # Step 5: Query Case Entries
            response = await session.get(api_url)
            if response.status_code != 200: 
                return [], f"Failed to fetch data. Status: {response.status_code}"

            data = response.json()
            cases_list = data.get('data', []) if isinstance(data, dict) else data

            scraped_cases = []
            for item in cases_list:
                if not item or not isinstance(item, dict): 
                    continue
                
                # Strict check for 'adama' while bypassing 'adama district'
                item_str = str(item).lower()
                if "adama" in item_str and "adama district" not in item_str:
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
            logger.error(f"Scraper error: {str(e)}")
            return [], f"Error: {str(e)}"

async def terminate_case_on_dashboard(case_id):
    """Sends patch/close request to complete a specific case on the remote dashboard with rigid CSRF parsing."""
    if not EMAIL or not PASSWORD:
        return False

    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    terminate_url = f'https://api.tech24et.com/api/callentries/{case_id}/close'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://tech24et.com',
        'Referer': 'https://tech24et.com/'
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20.0, verify=False) as session:
        try:
            csrf_res = await session.get(csrf_url)
            cookies_dict = {}
            for header, val in csrf_res.headers.multi_items():
                if header.lower() == 'set-cookie':
                    cookie_parts = val.split(';')[0].split('=', 1)
                    if len(cookie_parts) == 2:
                        cookies_dict[cookie_parts[0].strip()] = cookie_parts[1].strip()

            xsrf = cookies_dict.get("XSRF-TOKEN") or session.cookies.get("XSRF-TOKEN")
            if xsrf: 
                decoded_xsrf = urllib.parse.unquote(xsrf)
                session.headers.update({
                    'X-XSRF-TOKEN': decoded_xsrf,
                    'X-CSRF-TOKEN': decoded_xsrf,
                    'Cookie': "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
                })
                
            login_res = await session.post(login_url, json={'email': EMAIL.strip(), 'password': PASSWORD.strip()})
            if login_res.status_code not in [200, 201, 204]:
                return False
            
            for header, val in login_res.headers.multi_items():
                if header.lower() == 'set-cookie':
                    cookie_parts = val.split(';')[0].split('=', 1)
                    if len(cookie_parts) == 2:
                        cookies_dict[cookie_parts[0].strip()] = cookie_parts[1].strip()

            if cookies_dict.get("XSRF-TOKEN"):
                decoded_updated = urllib.parse.unquote(cookies_dict["XSRF-TOKEN"])
                session.headers.update({
                    'X-XSRF-TOKEN': decoded_updated,
                    'X-CSRF-TOKEN': decoded_updated
                })
                
            session.headers.update({
                'Cookie': "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
            })
                
            res = await session.post(terminate_url, json={'status': 'completed'})
            return res.status_code in [200, 201, 204]
        except Exception as e:
            logger.error(f"Failed to terminate Case {case_id}: {e}")
            return False

# ==========================================
# 6. REPORT GENERATOR ENGINE
# ==========================================
async def build_formatted_report(title: str, days_filter: int = None) -> str:
    cases, status_msg = await scrape_website_cases()
    if status_msg != "OK":
        return f"❌ **Error generating report**: {status_msg}"
    if not cases:
        return "⚠️ **No data records found on the dashboard for Adama.**"

    now = datetime.now()
    filtered_cases = []
    
    for c in cases:
        if days_filter:
            try:
                case_date = datetime.strptime(c['date'].split(" ")[0], "%Y-%m-%d")
                if (now - case_date).days > days_filter:
                    continue
            except Exception as e:
                logger.warning(f"Could not parse date {c['date']}: {e}")
                pass
        filtered_cases.append(c)

    if not filtered_cases:
        return f"⚠️ **No registered cases matching the {title.lower()} timeline filter.**"

    technician_name = "Adama Tech Team"
    for c in filtered_cases:
        if c['technician'] != "Unassigned" and c['technician'] != "":
            technician_name = c['technician']
            break

    lines = [f"🏧 {title} report /{technician_name}/\n"]
    stats = {}
    
    for c in filtered_cases:
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

    lines.append("\n       Generally \n")
    for bank_name, data in stats.items():
        lines.append(f" 🏛 {bank_name} Registered ")
        lines.append(f"           Completed - {data['completed']}")
        pending = data['registered'] - data['completed']
        if pending > 0:
            lines.append(f"           Pending - {pending}")

    return "\n".join(lines)

# ==========================================
# 7. TELEGRAM COMPONENT & INTERACTION UI
# ==========================================
def build_pending_ui(case):
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
        [InlineKeyboardButton("🗂 Open Dashboard Portal ↗️", url="https://tech24et.com")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

def build_terminate_ui(case):
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
        [InlineKeyboardButton("🗂 Open Dashboard Portal ↗️", url="https://tech24et.com")],
        [InlineKeyboardButton("❌ Close Card", callback_data="cancel_action")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

# ==========================================
# 8. COMMAND HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ACTIVE_CHATS.add(update.effective_chat.id)
    welcome_text = (
        "👋 **Welcome to Tech24 Adama Bot**\n\n"
        "💻 **Available Commands:**\n"
        "• /pending   - View all current open/unresolved cases\n"
        "• /terminate - Access list of cases to quickly terminate/complete\n"
        "• /report    - View structured Weekly summary report\n"
        "• /monthly   - View structured Monthly summary report\n"
        "• /export    - Generate and download spreadsheet raw database dump"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ACTIVE_CHATS.add(update.effective_chat.id)
    cases, status_msg = await scrape_website_cases()
    if status_msg != "OK": 
        return await update.message.reply_text(f"❌ **Connection Error**: {status_msg}", parse_mode="Markdown")

    pending_cases = [c for c in cases if c['status'] == "Pending"]

    if not pending_cases:
        keyboard = [[InlineKeyboardButton("🗂 Access Live Dashboard ↗️", url="https://tech24et.com")]]
        await update.message.reply_text(
            "✅ **All clear!** No pending or ongoing cases found in **Adama**.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if len(pending_cases) == 1:
        text, markup = build_pending_ui(pending_cases[0])
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        keyboard = []
        for case in pending_cases:
            btn_text = f"⏳ {case['case_id']} | {case['bank']} - {case['branch']}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"showpending_{case['case_id']}")])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
        await update.message.reply_text(
            "⏳ **Pending ATM Cases:**\nSelect any open case entry below to view logs on the server:",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )

async def terminate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ACTIVE_CHATS.add(update.effective_chat.id)
    cases, status_msg = await scrape_website_cases()
    if status_msg != "OK": 
        return await update.message.reply_text(f"❌ **API Error**: Could not retrieve entries.", parse_mode="Markdown")
        
    pending_cases = [c for c in cases if c['status'] == "Pending"]
    if not pending_cases:
        return await update.message.reply_text("✨ **No actions needed.** All logged issues are completed.", parse_mode="Markdown")

    keyboard = []
    for case in pending_cases:
        btn_text = f"🛑 Complete Case {case['case_id']} ({case['bank']})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"showterm_{case['case_id']}")])
    
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
    await update.message.reply_text(
        "🛠 **Termination Action Center:**\nChoose a case entry below to mark as completed directly on the Tech24 dashboard:",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    proc = await update.message.reply_text("⏳ Processing Weekly Case Summary...")
    report_text = await build_formatted_report("Weekly", days_filter=7)
    await proc.delete()
    await update.message.reply_text(report_text)

async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    proc = await update.message.reply_text("⏳ Processing Monthly Case Summary...")
    report_text = await build_formatted_report("Monthly", days_filter=30)
    await proc.delete()
    await update.message.reply_text(report_text)

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    proc = await update.message.reply_text("⏳ Compiling data logs & creating Excel workbook...")
    cases, status = await scrape_website_cases()
    
    if not cases:
        return await proc.edit_text("❌ Data compilation returned empty database.", parse_mode="Markdown")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ATM Incident Log"
    
    headers = ["Case ID", "Bank", "Branch", "Terminal ID", "Issue Description", "Resolution Status", "Assigned Technician", "Creation Timestamp", "Notes/Comments"]
    ws.append(headers)
    
    for c in cases:
        ws.append([c['case_id'], c['bank'], c['branch'], c['terminal'], c['issue'], c['status'], c['technician'], c['date'], c['comment']])

    excel_file = BytesIO()
    wb.save(excel_file)
    excel_file.seek(0)
    
    now_label = datetime.now().strftime("%B %Y")
    filename = f"atm-case-report-{datetime.now().strftime('%Y-%b').lower()}.xlsx"
    
    caption = (
        f"📊 **ATM Case Log Export**\n"
        f"📅 **Reporting Period:** {now_label}\n\n"
        "Automated administrative worksheet showing full issue records, down-times, and engineering logs."
    )

    await proc.delete()
    await update.message.reply_document(document=excel_file, filename=filename, caption=caption, parse_mode="Markdown")

# ==========================================
# 9. INTERACTIVE ACTION ROUTING (Callbacks)
# ==========================================
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel_action":
        await query.message.delete()
        return

    if data.startswith("showpending_"):
        case_id = data.split("_")[1]
        cases, _ = await scrape_website_cases()
        selected = next((c for c in cases if c['case_id'] == case_id), None)
        if selected:
            text, markup = build_pending_ui(selected)
            await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Case file could not be fetched or has been removed.", parse_mode="Markdown")

    elif data.startswith("showterm_"):
        case_id = data.split("_")[1]
        cases, _ = await scrape_website_cases()
        selected = next((c for c in cases if c['case_id'] == case_id), None)
        if selected:
            text, markup = build_terminate_ui(selected)
            await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Case file could not be fetched or has been removed.", parse_mode="Markdown")

    elif data.startswith("terminate_"):
        case_id = data.split("_")[1]
        await query.answer("⏳ Dispatching request to mark case completed...", show_alert=False)
        
        success = await terminate_case_on_dashboard(case_id)
        if success:
            await query.edit_message_text(f"✅ **Success!** Case `{case_id}` has been closed and updated on the remote platform.", parse_mode="Markdown")
        else:
            await query.edit_message_text(
                f"⚠️ **Server Update Failed:** Tried to complete Case `{case_id}` but could not confirm backend validation. Please check on-dashboard directly.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗂 Open Dashboard Portal", url="https://tech24et.com")]])
            )

# ==========================================
# 10. BACKGROUND TIMER ALERTS (10-Minute Windows)
# ==========================================
async def check_new_cases_loop(application: Application):
    while True:
        try:
            cases, status = await scrape_website_cases()
            if status == "OK" and cases:
                now = datetime.now()
                for c in cases:
                    if c['status'] == "Pending" and c['case_id'] not in NOTIFIED_CASES:
                        try:
                            case_time = datetime.strptime(c['date'], "%Y-%m-%d %H:%M:%S")
                            if now - case_time <= timedelta(minutes=10):
                                notification_text = (
                                    f"🚨 **ATM Incident Notification**\n\n"
                                    f"📝 **ID:** {c['case_id']},\n"
                                    f"🏦 **Bank:** {c['bank']},\n"
                                    f"⚠️ **Issue:** {c['issue']},\n"
                                    f"🏢 **Branch:** {c['branch']},\n"
                                    f"📍 **District:** {c['district']},\n"
                                    f"💬 **Comment:** {c['comment']},\n"
                                    f"🕒 **Reported at:** {c['date']}"
                                )
                                markup = InlineKeyboardMarkup([
                                    [InlineKeyboardButton("Open in tech24et.com dashboard", url="https://tech24et.com")]
                                ])
                                
                                for chat_id in list(ACTIVE_CHATS):
                                    try:
                                        await application.bot.send_message(
                                            chat_id=chat_id,
                                            text=notification_text,
                                            reply_markup=markup,
                                            parse_mode="Markdown"
                                        )
                                    except Exception as send_err:
                                        logger.warning(f"Could not broadcast to chat {chat_id}: {send_err}")
                                
                                NOTIFIED_CASES.add(c['case_id'])
                        except Exception as parse_err:
                            logger.error(f"Error parsing date for case {c['case_id']}: {parse_err}")
        except Exception as err:
            logger.error(f"Background alert loop encountered an error: {err}")
        
        await asyncio.sleep(60)

def start_background_loop(application):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(check_new_cases_loop(application))

# ==========================================
# 11. MAIN BOT ENTRY POINT
# ==========================================
def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("terminate", terminate_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("monthly", monthly_command))
    application.add_handler(CommandHandler("export", export_command))
    
    application.add_handler(CallbackQueryHandler(button_click_handler))

    threading.Thread(start_background_loop, args=(application,), daemon=True).start()

    logger.info("Bot starting up online polling loops...")
    application.run_polling()

if __name__ == '__main__':
    main()
