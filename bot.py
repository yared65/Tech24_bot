import os
import logging
import asyncio
import threading
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from io import BytesIO
from flask import Flask

import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ==========================================
# 1. LOGGING & CONFIGURATION
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")

# 🚨 MAINTENANCE SWITCH
MAINTENANCE_MODE = False

def get_eat_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)

raw_chat_id = os.environ.get("NOTIFICATION_CHAT_ID", "")
if raw_chat_id.startswith("-") or raw_chat_id.isdigit():
    try:
        NOTIFICATION_CHAT_ID = int(raw_chat_id)
    except ValueError:
        NOTIFICATION_CHAT_ID = raw_chat_id
else:
    NOTIFICATION_CHAT_ID = raw_chat_id

SENT_CASES_TRACKER = set()
SENT_REMINDERS_TRACKER = {}
ACTIVE_USERS_TRACKER = set()

# ==========================================
# 2. FLASK SERVER FOR KEEPALIVE (RENDER)
# ==========================================
app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    return "Bot is Running and Alive!", 200

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Keep-Alive Flask server on port {port}...")
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

# ==========================================
# 3. ROBUST JSON FIELD EXTRACTORS
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

def clean_extracted_value(data, key_hierarchy):
    if not data:
        return ""
    parsed_data = safe_parse_json(data) if isinstance(data, str) else data
    if not isinstance(parsed_data, dict):
        return str(parsed_data)
    for key in key_hierarchy:
        if key in parsed_data and parsed_data[key] is not None:
            val = parsed_data[key]
            if isinstance(val, dict):
                return clean_extracted_value(val, key_hierarchy)
            return str(val)
    for k, v in parsed_data.items():
        if isinstance(v, (dict, str)):
            res = clean_extracted_value(v, key_hierarchy)
            if res:
                return res
    return ""

def get_relative_time(date_obj):
    now = get_eat_now()
    diff = now - date_obj
    seconds = diff.total_seconds()
    if seconds < 0:
        minutes = abs(int(seconds // 60))
        if minutes < 2:
            return "Just now", "Just now"
        return f"{minutes}min", f"{minutes}min"
    minutes = int(seconds // 60)
    hours = int(minutes // 60)
    days = int(hours // 24)
    if days > 0:
        time_str = f"{days}d"
    elif hours > 0:
        time_str = f"{hours}h"
    else:
        time_str = f"{minutes}min"
    return time_str, time_str

# ==========================================
# 4. API SCRAPER & TRANSACTION ENGINES
# ==========================================
async def scrape_website_cases():
    if not EMAIL or not PASSWORD:
        return [], "Error: EMAIL or PASSWORD environment variables missing!"

    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    api_url = 'https://api.tech24et.com/api/callentries?limit=250'

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
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
                session.headers.update({
                    'X-XSRF-TOKEN': urllib.parse.unquote(xsrf_token)
                })

            payload = {'email': EMAIL.strip(), 'password': PASSWORD.strip()}
            login_res = await session.post(login_url, json=payload)
            if login_res.status_code not in [200, 201, 204]:
                return [], f"Login failed! Code: {login_res.status_code}"

            response = await session.get(api_url)
            if response.status_code != 200:
                return [], f"API GET error: {response.status_code}"

            data = response.json()
            raw_list = data.get('data', []) if isinstance(data, dict) else data
            if not isinstance(raw_list, list):
                return [], "Error: Data response format isn't parsed into list."

            scraped_cases = []
            for entry in raw_list:
                if not entry or not isinstance(entry, dict):
                    continue
                
                raw_string_dump = str(entry).lower()
                if "adama" in raw_string_dump:
                    case_id = str(entry.get('callentry_id', 'N/A'))
                    
                    terminal_data = entry.get('atmterminal') or {}
                    terminal_no = clean_extracted_value(terminal_data, ['atmterminal_no', 'terminal_no']) if isinstance(terminal_data, dict) else 'N/A'
                    if not terminal_no or terminal_no == "None": terminal_no = "N/A"
                    
                    terminal_name = clean_extracted_value(terminal_data, ['atmterminal_name', 'terminal_name']) if isinstance(terminal_data, dict) else 'N/A'
                    if not terminal_name or terminal_name == "None": terminal_name = "N/A"

                    bank_data = entry.get('bank') or {}
                    bank = clean_extracted_value(bank_data, ['bank_name', 'bankname']) if isinstance(bank_data, dict) else 'Awash'
                    if not bank or bank == "None": bank = "Awash"

                    issue_data = entry.get('issuesubcategory') or entry.get('issuecategory') or {}
                    issue = clean_extracted_value(issue_data, ['issuesubcat_name', 'issuecatname', 'name']) if isinstance(issue_data, dict) else 'ATM Issue'
                    if not issue or issue == "None": issue = "ATM Issue"

                    branch_data = entry.get('branch') or {}
                    branch = clean_extracted_value(branch_data, ['branch_name', 'branchname']) if isinstance(branch_data, dict) else 'Adama Branch'
                    if not branch or branch == "None": branch = "Adama Branch"

                    district_data = entry.get('district') or {}
                    district = clean_extracted_value(district_data, ['dist_name', 'district_name']) if isinstance(district_data, dict) else 'Adama'
                    if not district or district == "None": district = "Adama"

                    comment = entry.get('callentry_description') or "-"
                    if not comment or comment.strip() == "":
                        comment = "-"

                    technician = entry.get('assigned_eng', 'Not Assigned')
                    if not technician or str(technician).strip() == "" or str(technician).lower() == "none":
                        tech_obj = entry.get('Technician') or entry.get('technician') or {}
                        if isinstance(tech_obj, dict):
                            technician = tech_obj.get('assigned_eng', 'Not Assigned')

                    if not technician or str(technician).strip() == "" or str(technician).lower() == "none":
                        technician = "Not Assigned"
                    
                    tech_phone = entry.get('assigned_phone', '-')
                    if not tech_phone:
                        tech_phone = "-"

                    created_at = entry.get('created_at') or entry.get('Reported At') or entry.get('updated_at')
                    if not created_at:
                        tech_folder = entry.get('Technician') or entry.get('technician') or {}
                        if isinstance(tech_folder, dict):
                            created_at = tech_folder.get('created_at') or tech_folder.get('Reported At')

                    date_obj = None
                    date_str = "N/A"
                    
                    if created_at:
                        date_str = str(created_at).strip()
                        formats_to_try = (
                            "%d/%m/%Y %H:%M:%S",
                            "%d/%m/%Y %I:%M %p",
                            "%d/%m/%Y %H:%M",
                            "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%dT%H:%M:%S",
                            "%d/%m/%Y",
                            "%Y-%m-%d"
                        )
                        
                        clean_time_str = date_str.split(".")[0]
                        for fmt in formats_to_try:
                            try:
                                date_obj = datetime.strptime(clean_time_str, fmt).replace(tzinfo=None)
                                break
                            except ValueError:
                                continue
                                
                    if not date_obj:
                        date_obj = get_eat_now()
                        date_str = date_obj.strftime("%d/%m/%Y %H:%M:%S")
                    else:
                        date_str = date_obj.strftime("%d/%m/%Y %H:%M:%S")

                    status_raw = str(entry.get('callentry_status', '')).lower()
                    if not status_raw:
                        tech_folder = entry.get('Technician') or entry.get('technician') or {}
                        if isinstance(tech_folder, dict):
                            status_raw = str(tech_folder.get('callentry_status', '')).lower()

                    if status_raw in ["complete", "completed", "done", "1"]:
                        status_text = "Completed"
                    else:
                        status_text = "On going"

                    scraped_cases.append({
                        'case_id': case_id,
                        'bank': bank,
                        'district': district,
                        'branch': branch,
                        'terminal': terminal_no,
                        'atm_name': terminal_name,
                        'issue': issue,
                        'status': status_text,
                        'comment': comment,
                        'technician': technician,
                        'tech_phone': tech_phone,
                        'date_raw': date_str,
                        'date_obj': date_obj
                    })

            return scraped_cases, "OK"

        except Exception as e:
            return [], f"Scraper Exception: {str(e)}"

async def terminate_case_on_dashboard(case_id):
    terminate_url = f'https://api.tech24et.com/api/callentries/{case_id}/close'
    login_url = "https://api.tech24et.com/api/login"
    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Origin': 'https://tech24et.com',
        'Referer': 'https://tech24et.com/'
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=25.0, verify=False) as client:
        try:
            await client.get(csrf_url)
            xsrf_token = client.cookies.get("XSRF-TOKEN")
            if xsrf_token:
                client.headers.update({'X-XSRF-TOKEN': urllib.parse.unquote(xsrf_token)})

            payload = {"email": EMAIL.strip(), "password": PASSWORD.strip()}
            login_res = await client.post(login_url, json=payload)
            if login_res.status_code not in [200, 201, 204]:
                return False, f"Auth Error status: {login_res.status_code}"

            res = await client.post(terminate_url, json={})
            if res.status_code in [200, 204]:
                return True, "Successfully Closed"
            return False, f"API Rejected: Code {res.status_code}"
            
        except Exception as e:
            return False, str(e)

# ==========================================
# 5. AUTOMATIC ALARM & OVERDUE LOOP
# ==========================================
async def start_independent_alarm_loop(bot):
    logger.info("Background Alarm Engine successfully launched inside Application Loop.")
    while True:
        try:
            if MAINTENANCE_MODE:
                await asyncio.sleep(30)
                continue

            cases, status = await scrape_website_cases()
            if status != "OK":
                logger.error(f"Background Scan Scraper error: {status}")
                await asyncio.sleep(30)
                continue

            pending_statuses = ["on going", "pending", "open", "0"]
            pending_cases = [c for c in cases if str(c['status']).lower() in pending_statuses or c['status'] == "On going"]
            
            now = get_eat_now()

            for case in pending_cases:
                case_id = case['case_id']
                case_time = case['date_obj']

                if case_id not in SENT_CASES_TRACKER:
                    SENT_CASES_TRACKER.add(case_id)
                    
                    time_diff = now - case_time
                    hours_ago = int(time_diff.total_seconds() // 3600)
                    mins_ago = int((time_diff.total_seconds() % 3600) // 60)
                    age_str = f"{hours_ago}h {mins_ago}m ago" if hours_ago > 0 else f"{mins_ago}min ago"

                    notif_text = (
                        f"🚨 *ATM Incident Alert* 🚨\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📄 *ID:* `{case_id}`\n"
                        f"🏦 *Bank:* {case['bank']}\n"
                        f"🏢 *Branch:* {case['branch']}\n"
                        f"⚠️ *Issue:* {case['issue']}\n"
                        f"📍 *District:* {case['district']}\n"
                        f"💬 *Comment:* {case['comment']}\n"
                        f"🕒 *Reported at:* {case['date_raw']} ({age_str})\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 _Status: Pending Action / Unresolved_"
                    )
                    
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open Dashboard", url="https://tech24et.com/")]])
                    
                    if NOTIFICATION_CHAT_ID:
                        try: await bot.send_message(chat_id=NOTIFICATION_CHAT_ID, text=notif_text, reply_markup=kb, parse_mode="Markdown")
                        except Exception: pass

                    for user_id in list(ACTIVE_USERS_TRACKER):
                        try: await bot.send_message(chat_id=user_id, text=notif_text, reply_markup=kb, parse_mode="Markdown")
                        except Exception: pass
                    continue

                time_elapsed = now - case_time
                if time_elapsed >= timedelta(hours=5):
                    last_reminder = SENT_REMINDERS_TRACKER.get(case_id)
                    
                    if last_reminder is None or (now - last_reminder) >= timedelta(hours=5):
                        SENT_REMINDERS_TRACKER[case_id] = now
                        hours_passed = int(time_elapsed.total_seconds() // 3600)
                        
                        reminder_text = (
                            f"⚠️ *OVERDUE INCIDENT REMINDER (>{hours_passed} Hours)* ⚠️\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"ይህ ኬስ ከተመዘገበ {hours_passed} ሰዓታት አልፈውታል። ፍተሻ ያድርጉ።\n\n"
                            f"📄 *ID:* `{case_id}`\n"
                            f"🏦 *Bank:* {case['bank']} ({case['branch']})\n"
                            f"⚠️ *Issue:* {case['issue']}\n"
                            f"👤 *Technician:* {case['technician']}\n"
                            f"🕒 *Reported at:* {case['date_raw']}\n\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏳ _Duration: Still Pending!_"
                        )
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open Dashboard", url="https://tech24et.com/")]])
                        
                        if NOTIFICATION_CHAT_ID:
                            try: await bot.send_message(chat_id=NOTIFICATION_CHAT_ID, text=reminder_text, reply_markup=kb, parse_mode="Markdown")
                            except Exception: pass
                        
                        for user_id in list(ACTIVE_USERS_TRACKER):
                            try: await bot.send_message(chat_id=user_id, text=reminder_text, reply_markup=kb, parse_mode="Markdown")
                            except Exception: pass

        except Exception as e:
            logger.error(f"Error inside independent background loop: {str(e)}")
        
        await asyncio.sleep(30)

# ==========================================
# 6. DYNAMIC UI BUILDERS & OTHER HELPERS (Simplified)
# ==========================================
def get_maintenance_message():
    return "🚨 *SYSTEM MAINTENANCE* 🚨\nBot is currently undergoing maintenance."

def build_case_detail_ui(case):
    relative_long, _ = get_relative_time(case['date_obj'])
    text = (
        f"Case ID: {case['case_id']}\n"
        f"Bank: {case['bank']}\n"
        f"Branch: {case['branch']}\n"
        f"Issue: {case['issue']}\n"
        f"Status: {case['status']}\n"
        f"Technician: {case['technician']}\n"
    )
    keyboard = [
        [InlineKeyboardButton("⛔ Terminate", callback_data=f"askterm_{case['case_id']}")],
        [InlineKeyboardButton("🌀 Refresh", callback_data=f"refresh_{case['case_id']}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

def format_technician_weekly_report(cases, selected_tech):
    now = get_eat_now()
    start_of_week = now - timedelta(days=now.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    filtered_cases = [c for c in cases if c['date_obj'] >= start_of_week and c['technician'].lower() == selected_tech.lower()]
    if not filtered_cases:
        return f"📭 No cases for *{selected_tech}* this week."
    report_lines = [f"📋 *Report - {selected_tech}* 📋"]
    for c in filtered_cases:
        report_lines.append(f"{c['case_id']} | {c['bank']} | {c['status']}")
    return "\n".join(report_lines)

def format_weekly_summary_matrix(cases):
    return "Summary report generation..."

def generate_excel_bytes(cases):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Case ID", "Bank", "Branch", "Status", "Technician"])
    for c in cases:
        ws.append([c['case_id'], c['bank'], c['branch'], c['status'], c['technician']])
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output

# ==========================================
# 7. TELEGRAM COMMAND HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_USERS_TRACKER.add(chat_id)
    await update.message.reply_text("Welcome to Adama District Bot!")

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching pending cases...")

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE:
        return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")

    processing = await update.message.reply_text("⏳ Processing weekly active configurations, please wait...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Error:* {status}", parse_mode="Markdown")

    # የተጠየቁት አራት ቴክኒሻኖች ብቻ እንዲታዩ የተደረገ ማስተካከያ
    allowed_technicians = ["Yared Girma", "Girmaye Kelil", "Yohanis Getiye", "Feab Worku"]
    
    text = "Select an Adama District Technician to view their weekly cases report (Monday - Saturday):"
    keyboard = []
    for tech in allowed_technicians:
        keyboard.append([InlineKeyboardButton(tech, callback_data=f"wrep_{tech}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Generating summary...")

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Exporting excel...")

async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("wrep_"):
        tech_name = query.data.split("_")[1]
        cases, _ = await scrape_website_cases()
        await query.edit_message_text(format_technician_weekly_report(cases, tech_name), parse_mode="Markdown")

# ==========================================
# 8. STARTUP & MAIN
# ==========================================
async def post_init(application: Application) -> None:
    asyncio.create_task(start_independent_alarm_loop(application.bot))

def main():
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("summary", summary_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CallbackQueryHandler(button_click_handler))
    application.run_polling()

if __name__ == '__main__':
    main()
