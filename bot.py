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
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

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

# 🎯 ALLOWED TECHNICIANS
ALLOWED_TECHNICIANS = [
     "Girmaye Kelil","Isael Aklilu",
    "Yared Girma","Yohanis Getiye",
]

# የጉግል ፎርም ማስገቢያ ሊንክ
FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSfJAWo1l6gNT2hFwnGZcf-ibX-8drfZLR_ww6JMx_yFZCEcGQ/formResponse"

def get_eat_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)

raw_chat_id = os.environ.get("NOTIFICATION_CHAT_ID", "")
if raw_chat_id.startswith("-") or raw_chat_id.isdigit():
    try: NOTIFICATION_CHAT_ID = int(raw_chat_id)
    except ValueError: NOTIFICATION_CHAT_ID = raw_chat_id
else:
    NOTIFICATION_CHAT_ID = raw_chat_id

SENT_CASES_TRACKER = set()
SENT_REMINDERS_TRACKER = {}
ACTIVE_USERS_TRACKER = set()

# 📝 የባለብዙ-ደረጃ ፎርም ስቴት መቆጣጠሪያ
USER_FORM_STATES = {}

# 🌐 GLOBAL HTTP CLIENT (ቦቱ እንዳይቆም/እንዳይዝረከረክ በጋራ የሚሰራ)
HTTP_CLIENT = httpx.AsyncClient(
    headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://tech24et.com',
        'Referer': 'https://tech24et.com/'
    },
    follow_redirects=True,
    timeout=10.0, # ታይምአውት ወደ 10 ሰከንድ ዝቅ ተደርጓል
    verify=False
)

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
    if not val: return {}
    if isinstance(val, dict): return val
    try:
        if isinstance(val, str):
            cleaned = val.replace("'", '"').replace("None", "null").replace("True", "true").replace("False", "false")
            return json.loads(cleaned)
    except Exception: pass
    return {}

def clean_extracted_value(data, key_hierarchy):
    if not data: return ""
    parsed_data = safe_parse_json(data) if isinstance(data, str) else data
    if not isinstance(parsed_data, dict): return str(parsed_data)
    
    # መጀመሪያ ዋናዎቹን የቁልፍ ቅደም ተከተሎች መፈለግ
    for key in key_hierarchy:
        if key in parsed_data and parsed_data[key] is not None:
            val = parsed_data[key]
            if isinstance(val, dict): 
                return clean_extracted_value(val, key_hierarchy)
            return str(val)
            
    # ማለቂያ የሌለው ሉፕ (Infinite Loop) እንዳይፈጠር ተስተካክሏል
    for key in key_hierarchy:
        for k, v in parsed_data.items():
            if isinstance(v, dict) and key in v:
                return str(v[key])
    return ""

def get_relative_time(date_obj):
    now = get_eat_now()
    diff = now - date_obj
    seconds = diff.total_seconds()
    if seconds < 0:
        minutes = abs(int(seconds // 60))
        if minutes < 2: return "Just now", "Just now"
        return f"{minutes}min", f"{minutes}min"
    minutes = int(seconds // 60)
    hours = int(minutes // 60)
    days = int(hours // 24)
    if days > 0: time_str = f"{days}d"
    elif hours > 0: time_str = f"{hours}h"
    else: time_str = f"{minutes}min"
    return time_str, time_str

def find_matching_technician(dashboard_tech_name):
    if not dashboard_tech_name or str(dashboard_tech_name).strip().lower() in ["none", "not assigned", "-"]:
        return None
    dash_clean = " ".join(str(dashboard_tech_name).strip().split()).lower()
    for tech in ALLOWED_TECHNICIANS:
        tech_clean = " ".join(str(tech).strip().split()).lower()
        if tech_clean == dash_clean: return tech
    return None

# ==========================================
# 4. API SCRAPER
# ==========================================
async def scrape_website_cases():
    if not EMAIL or not PASSWORD:
        return [], "Error: EMAIL or PASSWORD environment variables missing!"

    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    api_url = 'https://api.tech24et.com/api/callentries?limit=250'

    try:
        await HTTP_CLIENT.get(csrf_url)
        xsrf_token = HTTP_CLIENT.cookies.get("XSRF-TOKEN")
        if xsrf_token:
            HTTP_CLIENT.headers.update({'X-XSRF-TOKEN': urllib.parse.unquote(xsrf_token)})

        payload = {'email': EMAIL.strip(), 'password': PASSWORD.strip()}
        login_res = await HTTP_CLIENT.post(login_url, json=payload)
        if login_res.status_code not in [200, 201, 204]:
            return [], f"Login failed! Code: {login_res.status_code}"

        response = await HTTP_CLIENT.get(api_url)
        if response.status_code != 200:
            return [], f"API GET error: {response.status_code}"

        data = response.json()
        raw_list = data.get('data', []) if isinstance(data, dict) else data
        if not isinstance(raw_list, list):
            return [], "Error: Data response format isn't parsed into list."

        scraped_cases = []
        for entry in raw_list:
            if not entry or not isinstance(entry, dict): continue
            
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
                if not comment or comment.strip() == "": comment = "-"

                technician = entry.get('assigned_eng', 'Not Assigned')
                if not technician or str(technician).strip() == "" or str(technician).lower() == "none":
                    tech_obj = entry.get('Technician') or entry.get('technician') or {}
                    if isinstance(tech_obj, dict):
                        technician = tech_obj.get('assigned_eng', 'Not Assigned')

                if not technician or str(technician).strip() == "" or str(technician).lower() == "none":
                    technician = "Not Assigned"
                
                tech_phone = entry.get('assigned_phone', '-')
                if not tech_phone: tech_phone = "-"

                created_at = entry.get('created_at') or entry.get('Reported At') or entry.get('updated_at')
                closed_at_raw = entry.get('closed_at') or entry.get('updated_at') or ""

                date_obj = None
                if created_at:
                    date_str = str(created_at).strip()
                    formats_to_try = (
                        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M",
                        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%Y-%m-%d"
                    )
                    clean_time_str = date_str.split(".")[0]
                    for fmt in formats_to_try:
                        try:
                            date_obj = datetime.strptime(clean_time_str, fmt).replace(tzinfo=None)
                            break
                        except ValueError: continue
                            
                if not date_obj:
                    date_obj = get_eat_now()
                    date_str = date_obj.strftime("%d/%m/%Y %H:%M:%S")
                else:
                    date_str = date_obj.strftime("%d/%m/%Y %H:%M:%S")

                reg_date, reg_time = ("-", "-")
                if date_str and " " in date_str:
                    reg_date, reg_time = date_str.split(" ")[0], date_str.split(" ")[1][:5]

                closed_date, closed_time = ("-", "-")
                if closed_at_raw and " " in str(closed_at_raw):
                    c_str = str(closed_at_raw).split(".")[0]
                    closed_date, closed_time = c_str.split(" ")[0], c_str.split(" ")[1][:5]

                status_raw = str(entry.get('callentry_status', '')).lower()
                if status_raw in ["complete", "completed", "done", "1"]: status_text = "Completed"
                else: status_text = "On going"

                scraped_cases.append({
                    'case_id': case_id, 'bank': bank, 'district': district, 'branch': branch,
                    'terminal': terminal_no, 'atm_name': terminal_name, 'issue': issue,
                    'status': status_text, 'comment': comment, 'technician': technician,
                    'tech_phone': tech_phone, 'date_raw': date_str, 'date_obj': date_obj,
                    'reg_date': reg_date, 'reg_time': reg_time,
                    'closed_date': closed_date, 'closed_time': closed_time
                })
        return scraped_cases, "OK"
    except Exception as e:
        logger.error(f"Scraper Exception: {str(e)}")
        return [], f"Scraper Exception: {str(e)}"

async def terminate_case_on_dashboard(case_id):
    terminate_url = f'https://api.tech24et.com/api/callentries/{case_id}/close'
    login_url = "https://api.tech24et.com/api/login"
    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'

    try:
        await HTTP_CLIENT.get(csrf_url)
        xsrf_token = HTTP_CLIENT.cookies.get("XSRF-TOKEN")
        if xsrf_token:
            HTTP_CLIENT.headers.update({'X-XSRF-TOKEN': urllib.parse.unquote(xsrf_token)})

        payload = {"email": EMAIL.strip(), "password": PASSWORD.strip()}
        login_res = await HTTP_CLIENT.post(login_url, json=payload)
        if login_res.status_code not in [200, 201, 204]:
            return False, f"Auth Error status: {login_res.status_code}"

        res = await HTTP_CLIENT.post(terminate_url, json={})
        if res.status_code in [200, 204]: return True, "Successfully Closed"
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
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Check in dashboard", url="https://tech24et.com/login")]])
                    
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
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Check in dashboard", url="https://tech24et.com/login")]])
                        
                        if NOTIFICATION_CHAT_ID:
                            try: await bot.send_message(chat_id=NOTIFICATION_CHAT_ID, text=reminder_text, reply_markup=kb, parse_mode="Markdown")
                            except Exception: pass
                        for user_id in list(ACTIVE_USERS_TRACKER):
                            try: await bot.send_message(chat_id=user_id, text=reminder_text, reply_markup=kb, parse_mode="Markdown")
                            except Exception: pass
        except Exception as e:
            logger.error(f"Error inside independent background loop: {str(e)}")
        await asyncio.sleep(30)

def get_maintenance_message():
    return (
        "🚨 *SYSTEM NOTICE / MAINTENANCE ALERT* 🚨\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ *For All bot user !!!*\n"
        "The bot was under maintenance and we working on to getback to work please be patient 🙏 🙏🙏 Thank you for understanding us \n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛠️ _Status: Upgrading systems & optimization ongoing_"
    )

def build_case_detail_ui(case):
    relative_long, _ = get_relative_time(case['date_obj'])
    text = (
        f"Case ID: {case['case_id']}\n"
        f"Terminal: {case['terminal']}\n"
        f"Bank: {case['bank']}\n"
        f"Branch: {case['branch']}\n"
        f"Issue: {case['issue']}\n"
        f"Status: {case['status']}\n"
        f"District: {case['district']}\n"
        f"Comment: {case['comment']}\n"
        f"Technician: {case['technician']}\n"
        f"Reported At: {case['date_obj'].strftime('%d/%m/%Y %H:%M:%S')} (EAT)\n"
        f"Relative Time: {relative_long}"
    )
    keyboard = [
        [InlineKeyboardButton("⛔ Terminate", callback_data=f"askterm_{case['case_id']}")],
        [InlineKeyboardButton("🌀 Refresh", callback_data=f"refresh_{case['case_id']}")],
        [InlineKeyboardButton("⛔ Cancel", callback_data="cancel_action")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

# ==========================================
# 7. EXCEL & SPECIFIC REPORT FORMATTERS
# ==========================================
def format_technician_daily_report(cases, selected_tech, report_type):
    now = get_eat_now()
    today_str = now.strftime("%d/%m/%Y")
    filtered_cases = []
    for c in cases:
        if c['date_obj'].strftime("%d/%m/%Y") == today_str:
            matched_tech = find_matching_technician(c['technician'])
            if matched_tech and matched_tech.lower() == selected_tech.lower():
                filtered_cases.append(c)

    title_type = "Telegram Registered Cases" if report_type == "case" else "PM Report" if report_type == "pm" else "Dashboard Cases"
    if not filtered_cases:
        return (
            f"📋 *Adama District Daily Report ({title_type}) - {selected_tech}* 📋\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📭 *Currently, there are no recorded cases for this technician today ({today_str}).*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    report_lines = [f"📋 *Adama District Daily Report ({title_type}) - {selected_tech}* 📋\n", f"📅 Date: {today_str}\n━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for idx, c in enumerate(filtered_cases, start=1):
        status_emoji = "✅ Completed" if c['status'] == "Completed" else "⏳ On going"
        line = f"{idx}. ID: {c['case_id']}\n🏦 Bank: {c['bank']} ({c['branch']} branch)\n⚠️ Issue: {c['issue']}\n📌 Status: {status_emoji}\n💬 Comment: {c['comment']}\n----------------------------------------"
        report_lines.append(line)
    return "\n".join(report_lines)

def format_technician_weekly_report(cases, selected_tech):
    now = get_eat_now()
    start_of_week = now - timedelta(days=now.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = start_of_week + timedelta(days=6, hours=23, minutes=59, seconds=59)

    filtered_cases = []
    for c in cases:
        if start_of_week <= c['date_obj'] <= end_of_week:
            matched_tech = find_matching_technician(c['technician'])
            if matched_tech and matched_tech.lower() == selected_tech.lower(): filtered_cases.append(c)

    if not filtered_cases:
        return f"📋 *Adama District Weekly Cases Report - {selected_tech}* 📋\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n📭 *Currently, there are no recorded cases assigned to this technician for this week.*\n\n🌟 Keep up the great work!\n━━━━━━━━━━━━━━━━━━━━━━━━━━"
    report_lines = [f"📋 *Adama District Weekly Cases Report - {selected_tech}* 📋\n"]
    for idx, c in enumerate(filtered_cases, start=1):
        date_formatted = c['date_obj'].strftime("%d/%m/%Y")
        status_emoji = "✅ Completed" if c['status'] == "Completed" else "⏳ On going"
        line = f"{idx}. ID: {c['case_id']}\n🏦 Bank: {c['bank']} ({c['branch']} branch)\n⚠️ Issue: {c['issue']}\n📅 Date: {date_formatted}\n📌 Status: {status_emoji}\n----------------------------------------"
        report_lines.append(line)

    report_lines.append("\n        *Generally*")
    bank_analytics = {}
    for case in filtered_cases:
        b_name = case['bank']
        if b_name not in bank_analytics: bank_analytics[b_name] = {"completed": 0, "ongoing": 0}
        if case['status'] == "Completed": bank_analytics[b_name]["completed"] += 1
        else: bank_analytics[b_name]["ongoing"] += 1

    for bank_name, stats in bank_analytics.items():
        report_lines.append(f"*{bank_name} bank*\n    Completed-{stats['completed']}\n    On going-{stats['ongoing']}")
    return "\n".join(report_lines)

def format_weekly_summary_matrix(cases):
    now = get_eat_now()
    start_of_week = now - timedelta(days=now.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = start_of_week + timedelta(days=6, hours=23, minutes=59, seconds=59)

    filtered_cases = [c for c in cases if start_of_week <= c['date_obj'] <= end_of_week]
    report_lines = ["📋 *Weekly Summary Report of Matrix* 📋\n"]

    tech_stats = {tech: {"completed": 0, "ongoing": 0} for tech in ALLOWED_TECHNICIANS}
    total_completed, total_ongoing, other_district_or_unassigned = 0, 0, 0

    for case in filtered_cases:
        matched_tech = find_matching_technician(case['technician'])
        if matched_tech:
            if case['status'] == "Completed":
                tech_stats[matched_tech]["completed"] += 1
                total_completed += 1
            else:
                tech_stats[matched_tech]["ongoing"] += 1
                total_ongoing += 1
        else: other_district_or_unassigned += 1

    for tech in ALLOWED_TECHNICIANS:
        stats = tech_stats[tech]
        report_lines.append(f" 👤 Technician *{tech}* {stats['completed']} case completed {stats['ongoing']} on going.\n")

    report_lines.append(f"\n 🟧 Totally in *Adama District* {total_completed} completed {total_ongoing} on going cases.")
    if other_district_or_unassigned > 0: report_lines.append(f" 🔍 Unassigned / Other District Cases: *{other_district_or_unassigned}*")
    total_cases = total_completed + total_ongoing
    if total_cases > 0: report_lines.append(f"🎯 Completion Rate: *{(total_completed / total_cases) * 100:.1f}%*")
    num_techs = len(ALLOWED_TECHNICIANS)
    if num_techs > 0: report_lines.append(f"📊 Average Completed cases per Tech: *{total_completed / num_techs:.1f}*")
    return "\n".join(report_lines)

def generate_excel_bytes(cases):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Incident Log Database"
    ws.views.sheetView[0].showGridLines = True

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    data_font = Font(name="Calibri", size=10, bold=False, color="000000")
    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    thin_border_side = Side(border_style="thin", color="D9D9D9")
    grid_border = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)

    headers = ["Case ID", "Terminal", "Bank", "Branch", "Issue Description", "Status", "District", "Comment", "Technician", "Tech Phone", "Date EAT"]
    ws.append(headers)

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row_idx, case in enumerate(cases, start=2):
        row_data = [case['case_id'], case['terminal'], case['bank'], case['branch'], case['issue'], case['status'], case['district'], case['comment'], case['technician'], case['tech_phone'], case['date_raw']]
        ws.append(row_data)
        for col_num in range(1, len(headers) + 1):
            cell = ws.cell(row=row_idx, column=col_num)
            cell.font = data_font
            cell.border = grid_border
            cell.alignment = Alignment(horizontal="center") if col_num in [1, 2, 6, 10, 11] else Alignment(horizontal="left")

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = max(max_len + 3, 12)

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output

# ==========================================
# 8. TELEGRAM COMMAND HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE: return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")
    chat_id = update.effective_chat.id
    ACTIVE_USERS_TRACKER.add(chat_id)

    welcome_text = (
        "👋 *Welcome to Tech24 Adama District Bot*\n\n"
        "💻 *Available Commands Menu:*\n"
        "• /pending - View currently open / unresolved cases\n"
        "• /daily - View daily report by technician selection\n"
        "• /report - View weekly performance metrics by technician\n"
        "• /summary - View overall weekly matrix summary\n"
        "• /export - Download structured incident Excel spreadsheets"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE: return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")
    processing = await update.message.reply_text("⏳ Searching dashboard portal for Adama logs, please wait...")
    cases, status = await scrape_website_cases()
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK": return await update.message.reply_text(f"❌ *Connection Failure:*\n{status}", parse_mode="Markdown")
    pending_cases = [c for c in cases if c['status'] == "On going"]
    if not pending_cases:
        return await update.message.reply_text("✅ All Adama cases are completed! No pending cases found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Check in dashboard", url="https://tech24et.com/login")]]))

    if len(pending_cases) == 1:
        text, kb = build_case_detail_ui(pending_cases[0])
        await update.message.reply_text(text, reply_markup=kb)
    else:
        text = "The following ATM cases have been reported and are currently pending action. Select a case from the list below to view details."
        keyboard = [[InlineKeyboardButton(f"{get_relative_time(c['date_obj'])[1]} | {c['case_id']} | {c['bank']} | {c['branch']}", callback_data=f"view_{c['case_id']}")] for c in pending_cases]
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_action")])
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE: return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")
    keyboard = [[InlineKeyboardButton(tech, callback_data=f"dtech_{tech}")] for tech in sorted(ALLOWED_TECHNICIANS)]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
    await update.message.reply_text("Select an Adama District Technician to view their Daily report:", reply_markup=InlineKeyboardMarkup(keyboard))

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE: return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")
    keyboard = [[InlineKeyboardButton(tech, callback_data=f"wrep_{tech}")] for tech in sorted(ALLOWED_TECHNICIANS)]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
    await update.message.reply_text("Select an Adama District Technician to view their weekly cases report (Monday - Sunday):", reply_markup=InlineKeyboardMarkup(keyboard))

async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE: return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")
    processing = await update.message.reply_text("⏳ Searching dashboard portal for Adama logs, please wait...")
    cases, status = await scrape_website_cases()
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)
    if status != "OK": return await update.message.reply_text(f"❌ *Error:* {status}", parse_mode="Markdown")
    await update.message.reply_text(format_weekly_summary_matrix(cases), parse_mode="Markdown")

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE: return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")
    processing = await update.message.reply_text("⏳ Writing and formatting Excel spreadsheet...")
    cases, status = await scrape_website_cases()
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK": return await update.message.reply_text(f"❌ *Export Blocked:* {status}", parse_mode="Markdown")
    if not cases: return await update.message.reply_text("❌ *Export Cancelled:* No cases matched query scope.", parse_mode="Markdown")

    excel_file = generate_excel_bytes(cases)
    excel_file.name = f"case-report-{get_eat_now().strftime('%Y-%m')}.xlsx"
    await context.bot.send_document(chat_id=update.effective_chat.id, document=excel_file, caption=f"📊 *ATM Cases Report – {get_eat_now().strftime('%B %Y')}*\n\nThis report contains all ATM cases.", parse_mode="Markdown")

# ==========================================
# 9. INLINE BUTTON CALLBACK HANDLER
# ==========================================
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if MAINTENANCE_MODE:
        await context.bot.send_message(chat_id=chat_id, text=get_maintenance_message(), parse_mode="Markdown")
        return

    data = query.data
    if data == "cancel_action":
        USER_FORM_STATES.pop(chat_id, None)
        await query.message.delete()
        return

    if data.startswith("dtech_"):
        tech_name = data.split("_")[1]
        confirm_text = f"   🔥 *Daily Report*\n\nℹ️ For Dashboard case select Dashboard button \n\n  ℹ️ For Telegram case and PM select Telegram & PM button"\n\n
        confirm_keyboard = [
            [InlineKeyboardButton("Dashboard", callback_data=f"ddash_{tech_name}"),
             InlineKeyboardButton("Telegram & PM", callback_data=f"dtgpm_menu_{tech_name}")],
            [InlineKeyboardButton("🔙 Back to Technicians", callback_data="back_to_daily_techs")]
        ]
        await query.edit_message_text(text=confirm_text, reply_markup=InlineKeyboardMarkup(confirm_keyboard), parse_mode="Markdown")
        return

    if data.startswith("dtgpm_menu_"):
        tech_name = data.split("_")[2]
        tgpm_text = f" Telegram and PM report \n\n ℹ️ For Telegram registered case clicked  *CASE* button\n \n ℹ️ For PM report Clicked *PM* button"\n\n
        tgpm_keyboard = [
            [InlineKeyboardButton("CASE", callback_data=f"drpt_case_{tech_name}"),
             InlineKeyboardButton("PM", callback_data=f"drpt_pm_{tech_name}")],
            [InlineKeyboardButton("🔙 Back", callback_data=f"dtech_{tech_name}")]
        ]
        await query.edit_message_text(text=tgpm_text, reply_markup=InlineKeyboardMarkup(tgpm_keyboard), parse_mode="Markdown")
        return

    # 4. 🎯 DASHBOARD BUTTON CLICKED -> LIST CASES
    if data.startswith("ddash_"):
        tech_name = data.split("_")[1]
        await query.edit_message_text("⏳ Syncing daily logs from dashboard portal...")
        cases, status = await scrape_website_cases()
        if status != "OK": 
            return await query.edit_message_text(f"❌ API Sync Fail: {status}")

        today_str = get_eat_now().strftime("%d/%m/%Y")
        filtered_cases = [c for c in cases if c['date_obj'].strftime("%d/%m/%Y") == today_str and find_matching_technician(c['technician']) and find_matching_technician(c['technician']).lower() == tech_name.lower()]

        if not filtered_cases:
            return await query.edit_message_text(text=f"📭 *No dashboard cases found for {tech_name} today ({today_str}).*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"dtech_{tech_name}")]], parse_mode="Markdown"))

        text = f"📋 *Today's Dashboard Cases for {tech_name}:*\nSelect a case to initiate reporting."
        keyboard = [[InlineKeyboardButton(f"ID: {c['case_id']} | {c['branch']}", callback_data=f"fcase_{c['case_id']}")] for c in filtered_cases]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"dtech_{tech_name}")])
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # 5. 🛠️ CASE SELECTED FROM DASHBOARD
    if data.startswith("fcase_"):
        case_id = data.split("_")[1]
        await query.edit_message_text("⏳ Extraction data for Google form mapping...")
        cases, status = await scrape_website_cases()
        if status != "OK": return await query.edit_message_text(f"❌ Sync Fail: {status}")
        
        target_case = next((c for c in cases if c['case_id'] == case_id), None)
        if not target_case: return await query.edit_message_text("❌ Selected record could not be found.")

        USER_FORM_STATES[chat_id] = {
            'step': 'ASK_PM_TYPE',
            'tech_name': target_case['technician'],
            'extracted_payload': {
                'entry.283120155': target_case['case_id'],        
                'entry.1541091566': target_case['terminal'],       
                'entry.2128913998': target_case['bank'],           
                'entry.1983056024': target_case['branch'],         
                'entry.1741675200': target_case['issue'],          
                'entry.1994644026': target_case['status'],         
                'entry.38555627': target_case['comment'],          
                'entry.regdate': target_case['reg_date'],          
                'entry.regtime': target_case['reg_time'],          
                'entry.clsdate': target_case['closed_date'],       
                'entry.clstime': target_case['closed_time']        
            }
        }
        
        pm_kb = [
            [InlineKeyboardButton("PM Done", callback_data="fpm_Done"),
             InlineKeyboardButton("PM Not Done", callback_data="fpm_Not_Done")],
            [InlineKeyboardButton("❌ Cancel Process", callback_data="cancel_action")]
        ]
        await context.bot.send_message(chat_id=chat_id, text=f"📊 *Form Configurator Loaded for Case {case_id}*\n\nየዳሽቦርድ መረጃዎች ተነበዋል። እባክዎ የቀሩትን መረጃዎች ይሙሉ፦\n\n*1. PM ተደርጓል?*", reply_markup=InlineKeyboardMarkup(pm_kb), parse_mode="Markdown")
        await query.message.delete()
        return

    # 6. HANDLING PM SELECTION
    if data.startswith("fpm_"):
        pm_value = data.split("_")[1] if len(data.split("_")) == 2 else f"{data.split('_')[1]} {data.split('_')[2]}"
        if chat_id not in USER_FORM_STATES:
            return await context.bot.send_message(chat_id=chat_id, text="❌ የፎርም መሙላት ሂደቱ ጊዜ አልፎበታል፣ እባክዎ እንደገና ይጀምሩ።")
        
        USER_FORM_STATES[chat_id]['extracted_payload']['entry.1011663080'] = pm_value.replace("_", " ") 
        USER_FORM_STATES[chat_id]['step'] = 'WAITING_FOR_RESOLUTION'
        
        kb = [[InlineKeyboardButton("❌ Abort", callback_data="cancel_action")]]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        # አስተማማኝ ፅሁፍ ማሳያ
        await context.bot.send_message(chat_id=chat_id, text="🔧 *2. የተወሰደው መፍትሄ (Resolution Description):*\n\nእባክዎ የተከናወነውን የቴክኒክ ስራ በፅሁፍ መልዕክት እዚህ ላይ ይላኩት።", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return

    # 7. PRE-SUBMIT PREVIEW
    if data == "f_trigger_preview":
        if chat_id not in USER_FORM_STATES: return
        payload = USER_FORM_STATES[chat_id]['extracted_payload']
        
        preview_msg = (
            f"📋 *Google Form Data Preview* 📋\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Case ID: {payload.get('entry.283120155')}\n"
            f"🏧 Terminal No: {payload.get('entry.1541091566')}\n"
            f"🏦 Bank: {payload.get('entry.2128913998')}\n"
            f"🏢 Branch: {payload.get('entry.1983056024')}\n"
            f"⚠️ Issue: {payload.get('entry.1741675200')}\n"
            f"📌 Status: {payload.get('entry.1994644026')}\n"
            f"🕒 Registered: {payload.get('entry.regdate', '-')} - {payload.get('entry.regtime', '-')}\n"
            f"⏳ Closed: {payload.get('entry.clsdate', '-')} - {payload.get('entry.clstime', '-')}\n"
            f"🔧 PM Status: {payload.get('entry.1011663080')}\n"
            f"⚙️ Resolution: {payload.get('entry.245892019')}\n"
            f"💬 Comment: {payload.get('entry.38555627')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"እባክዎ መረጃውን አረጋግጠው 'Submit Form' የሚለውን ይጫኑ።"
        )
        final_kb = [
            [InlineKeyboardButton("🚀 Submit Form", callback_data="f_final_submit")],
            [InlineKeyboardButton("❌ Cancel / Abort", callback_data="cancel_action")]
        ]
        await query.edit_message_text(text=preview_msg, reply_markup=InlineKeyboardMarkup(final_kb), parse_mode="Markdown")
        return

    # 8. POST TO GOOGLE FORM
    if data == "f_final_submit":
        if chat_id not in USER_FORM_STATES: return
        await query.edit_message_text("🚀 Sending comprehensive data bundle to Google Forms...")
        
        payload = USER_FORM_STATES[chat_id]['extracted_payload']
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(FORM_URL, data=payload)
                if resp.status_code in [200, 302]:
                    await query.edit_message_text("✅ *Google Form Successfully Submitted!* Dashboard & technician inputs fully synchronized.", parse_mode="Markdown")
                else:
                    await query.edit_message_text(f"❌ *Submission Failed.* Google form engine returned status code: {resp.status_code}")
        except Exception as e:
            await query.edit_message_text(f"❌ *Network / Connection Error:* {str(e)}")
        
        USER_FORM_STATES.pop(chat_id, None)
        return

    if data.startswith("drpt_"):
        parts = data.split("_")
        cases, _ = await scrape_website_cases()
        report_output = format_technician_daily_report(cases, parts[2], parts[1])
        await query.edit_message_text(text=report_output, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"dtgpm_menu_{parts[2]}")]], parse_mode="Markdown"))
        return

    if data == "back_to_daily_techs":
        keyboard = [[InlineKeyboardButton(tech, callback_data=f"dtech_{tech}")] for tech in sorted(ALLOWED_TECHNICIANS)]
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
        await query.edit_message_text("Select an Adama District Technician to view their Daily report:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("wrep_"):
        tech_name = data.split("_")[1]
        cases, _ = await scrape_website_cases()
        await query.edit_message_text(text=format_technician_weekly_report(cases, tech_name), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="back_to_techs")]]), parse_mode="Markdown")
        return

    if data == "back_to_techs":
        keyboard = [[InlineKeyboardButton(tech, callback_data=f"wrep_{tech}")] for tech in sorted(ALLOWED_TECHNICIANS)]
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
        await query.edit_message_text("Select an Adama District Technician to view their weekly cases report (Monday - Sunday):", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("askterm_"):
        case_id = data.split("_")[1]
        await query.edit_message_text(text=f"⚠️ *Confirmation Required*\n\nAre you sure you want to terminate/close Case ID: *{case_id}*?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Go Back", callback_data=f"view_{case_id}")], [InlineKeyboardButton("✅ Yes, Terminate", callback_data=f"do_terminate_{case_id}")], [InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_action")]]), parse_mode="Markdown")
        return

    if data.startswith("do_terminate_"):
        case_id = data.split("_")[2]
        await query.edit_message_text(f"⏳ Attempting terminal closure for Case ID `{case_id}`...", parse_mode="Markdown")
        success, err_msg = await terminate_case_on_dashboard(case_id)
        if success: await query.edit_message_text(f"✅ *Success!* Case ID `{case_id}` marked as Terminated.", parse_mode="Markdown")
        else: await query.edit_message_text(text=f"❌ *Termination failed:*\n`{err_msg}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Try Again", callback_data=f"askterm_{case_id}")], [InlineKeyboardButton("Cancel", callback_data="cancel_action")]]), parse_mode="Markdown")
        return

    if data.startswith("view_") or data.startswith("refresh_"):
        case_id = data.split("_")[2] if len(data.split("_")) == 3 else data.split("_")[1]
        cases, _ = await scrape_website_cases()
        target = next((c for c in cases if c['case_id'] == case_id), None)
        if not target: return await query.edit_message_text("❌ Record lost or finalized.")
        text, kb = build_case_detail_ui(target)
        await query.edit_message_text(text, reply_markup=kb)

# ==========================================
# 10. TEXT MESSAGE HANDLER FOR FORMS INPUT
# ==========================================
async def message_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in USER_FORM_STATES: return

    state_data = USER_FORM_STATES[chat_id]
    
    if state_data['step'] == 'WAITING_FOR_RESOLUTION':
        resolution_text = update.message.text
        if not resolution_text or resolution_text.strip() == "":
            await update.message.reply_text("❌ እባክዎ ትክክለኛ የፅሁፍ መግለጫ ያስገቡ።")
            return
            
        state_data['extracted_payload']['entry.245892019'] = resolution_text 
        state_data['step'] = 'PREVIEW_READY'
        
        preview_kb = [
            [InlineKeyboardButton("🔎 View Summary & Submit", callback_data="f_trigger_preview")],
            [InlineKeyboardButton("❌ Abort", callback_data="cancel_action")]
        ]
        await update.message.reply_text("✅ *ሁሉም መረጃዎች በስኬት ተሰባስበዋል!* እባክዎ ከታች ያለውን ማረጋገጫ በተን ይጫኑ።", reply_markup=InlineKeyboardMarkup(preview_kb), parse_mode="Markdown")

# ==========================================
# 11. STARTUP MENU INITIALIZER
# ==========================================
async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Initialize bot profile"),
        BotCommand("pending", "View open and unresolved cases"),
        BotCommand("daily", "View daily technician cases report"),
        BotCommand("report", "View weekly technician performance metrics"),
        BotCommand("summary", "View overall weekly summary metrics"),
        BotCommand("export", "Generate incident logs Excel sheet")
    ]
    await application.bot.set_my_commands(commands)
    asyncio.create_task(start_independent_alarm_loop(application.bot))

# ==========================================
# 12. ENGINE INITIATION
# ==========================================
def main():
    if not BOT_TOKEN:
        logger.error("SYSTEM ERROR: TELEGRAM_BOT_TOKEN is missing.")
        return

    threading.Thread(target=run_health_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("daily", daily_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("summary", summary_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CallbackQueryHandler(button_click_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_input_handler))

    application.run_polling()

if __name__ == '__main__':
    main()
