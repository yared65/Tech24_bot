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

# 🎯 ALLOWED TECHNICIANS
ALLOWED_TECHNICIANS = [
    "Girmaye Kelil",
    "Yared Girma",
    "Yohanis Getiye",
   
]

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

def find_matching_technician(dashboard_tech_name):
    if not dashboard_tech_name or str(dashboard_tech_name).strip().lower() in ["none", "not assigned", "-"]:
        return None
    dash_clean = " ".join(str(dashboard_tech_name).strip().split()).lower()
    for tech in ALLOWED_TECHNICIANS:
        tech_clean = " ".join(str(tech).strip().split()).lower()
        if tech_clean == dash_clean:
            return tech
    return None

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
                            "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M",
                            "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%Y-%m-%d"
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
                        'case_id': case_id, 'bank': bank, 'district': district, 'branch': branch,
                        'terminal': terminal_no, 'atm_name': terminal_name, 'issue': issue,
                        'status': status_text, 'comment': comment, 'technician': technician,
                        'tech_phone': tech_phone, 'date_raw': date_str, 'date_obj': date_obj
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
        'Accept': 'application/json', 'Content-Type': 'application/json',
        'Origin': 'https://tech24et.com', 'Referer': 'https://tech24et.com/'
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
                    
                    # 🔗 ወደ Login ፔጅ ብቻ እንዲወስድ የተደረገ ሊንክ
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
                        # 🔗 ወደ Login ፔጅ ብቻ እንዲወስድ የተደረገ ሊንክ
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

# ==========================================
# 5.5 GLOBAL MAINTENANCE RESPONSE GENERATOR
# ==========================================
def get_maintenance_message():
    return (
        "🚨 *SYSTEM NOTICE / MAINTENANCE ALERT* 🚨\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ *For All bot user !!!*\n"
        "The bot was under maintenance and we working on to getback to work please be patient 🙏 🙏🙏 Thank you for understanding us \n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛠️ _Status: Upgrading systems & optimization ongoing_"
    )

# ==========================================
# 6. DYNAMIC UI BUILDERS
# ==========================================
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
        f"Reported At: {case['date_obj'].strftime('%d/%m/%Y %H:%M:%S')} (East Africa Time)\n"
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
def format_technician_weekly_report(cases, selected_tech):
    now = get_eat_now()
    start_of_week = now - timedelta(days=now.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = start_of_week + timedelta(days=6, hours=23, minutes=59, seconds=59)

    filtered_cases = []
    for c in cases:
        if start_of_week <= c['date_obj'] <= end_of_week:
            matched_tech = find_matching_technician(c['technician'])
            if matched_tech and matched_tech.lower() == selected_tech.lower():
                filtered_cases.append(c)

    if not filtered_cases:
        return (
            f"📋 *Adama District Weekly Cases Report - {selected_tech}* 📋\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📭 *Currently, there are no recorded cases assigned to this technician for this week.*\n\n"
            f"🌟 Keep up the great work!\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    report_lines = [f"📋 *Adama District Weekly Cases Report - {selected_tech}* 📋\n"]

    for idx, c in enumerate(filtered_cases, start=1):
        date_formatted = c['date_obj'].strftime("%d/%m/%Y")
        status_emoji = "✅ Completed" if c['status'] == "Completed" else "⏳ On going"
        
        line = (
            f"{idx}. ID: {c['case_id']}\n"
            f"🏦 Bank: {c['bank']} ({c['branch']} branch)\n"
            f"⚠️ Issue: {c['issue']}\n"
            f"📅 Date: {date_formatted}\n"
            f"📌 Status: {status_emoji}\n"
            f"----------------------------------------"
        )
        report_lines.append(line)

    report_lines.append("\n        *Generally*")
    bank_analytics = {}
    for case in filtered_cases:
        b_name = case['bank']
        if b_name not in bank_analytics:
            bank_analytics[b_name] = {"completed": 0, "ongoing": 0}
        
        if case['status'] == "Completed":
            bank_analytics[b_name]["completed"] += 1
        else:
            bank_analytics[b_name]["ongoing"] += 1

    for bank_name, stats in bank_analytics.items():
        report_lines.append(f"*{bank_name} bank*")
        report_lines.append(f"    Completed-{stats['completed']}")
        report_lines.append(f"    On going-{stats['ongoing']}")

    return "\n".join(report_lines)

def format_weekly_summary_matrix(cases):
    now = get_eat_now()
    start_of_week = now - timedelta(days=now.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = start_of_week + timedelta(days=6, hours=23, minutes=59, seconds=59)

    filtered_cases = [c for c in cases if start_of_week <= c['date_obj'] <= end_of_week]

    report_lines = ["📋 *Weekly Summary Report of Matrix* 📋\n"]

    tech_stats = {tech: {"completed": 0, "ongoing": 0} for tech in ALLOWED_TECHNICIANS}
    total_completed = 0
    total_ongoing = 0
    other_district_or_unassigned = 0

    for case in filtered_cases:
        matched_tech = find_matching_technician(case['technician'])
        
        if matched_tech:
            if case['status'] == "Completed":
                tech_stats[matched_tech]["completed"] += 1
                total_completed += 1
            else:
                tech_stats[matched_tech]["ongoing"] += 1
                total_ongoing += 1
        else:
            other_district_or_unassigned += 1

    for tech in ALLOWED_TECHNICIANS:
        stats = tech_stats[tech]
        report_lines.append(f" 👤 Technician *{tech}* {stats['completed']} case completed {stats['ongoing']} on going.\n")

    report_lines.append("")
    total_cases = total_completed + total_ongoing
    report_lines.append(f" 🟧 Totally in *Adama District* {total_completed} completed {total_ongoing} on going cases.")
    
    if other_district_or_unassigned > 0:
        report_lines.append(f" 🔍 Unassigned / Other District Cases: *{other_district_or_unassigned}*")

    if total_cases > 0:
        completion_pct = (total_completed / total_cases) * 100
        report_lines.append(f"🎯 Completion Rate: *{completion_pct:.1f}%*")
    else:
        report_lines.append("🎯 Completion Rate: *0%*")

    num_techs = len(ALLOWED_TECHNICIANS)
    if num_techs > 0:
        avg_completed = total_completed / num_techs
        report_lines.append(f"📊 Average Completed cases per Tech: *{avg_completed:.1f}*")
    else:
        report_lines.append("📊 Average Completed cases per Tech: *0*")

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
        row_data = [
            case['case_id'], case['terminal'], case['bank'], case['branch'],
            case['issue'], case['status'], case['district'], case['comment'],
            case['technician'], case['tech_phone'], case['date_raw']
        ]
        ws.append(row_data)
        for col_num in range(1, len(headers) + 1):
            cell = ws.cell(row=row_idx, column=col_num)
            cell.font = data_font
            cell.border = grid_border
            if col_num in [1, 2, 6, 10, 11]:
                cell.alignment = Alignment(horizontal="center")
            else:
                cell.alignment = Alignment(horizontal="left")

    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            val_str = str(cell.value or '')
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output

# ==========================================
# 8. TELEGRAM COMMAND HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE:
        return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")

    chat_id = update.effective_chat.id
    ACTIVE_USERS_TRACKER.add(chat_id)

    welcome_text = (
        "👋 *Welcome to Tech24 Adama District Bot*\n\n"
        "💻 *Available Commands Menu:*\n"
        "• /pending - View currently open / unresolved cases\n"
        "• /report - View weekly performance metrics by technician\n"
        "• /summary - View overall weekly matrix summary\n"
        "• /export - Download structured incident Excel spreadsheets"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

    try:
        cases, status = await scrape_website_cases()
        if status == "OK":
            pending_statuses = ["on going", "pending", "open", "0"]
            pending_cases = [c for c in cases if str(c['status']).lower() in pending_statuses or c['status'] == "On going"]
            
            now = get_eat_now()
            for case in pending_cases:
                time_diff = now - case['date_obj']
                hours_ago = int(time_diff.total_seconds() // 3600)
                mins_ago = int((time_diff.total_seconds() % 3600) // 60)
                age_str = f"{hours_ago}h {mins_ago}m ago" if hours_ago > 0 else f"{mins_ago}min ago"

                notif_text = (
                    f"🚨 *ATM Incident Alert (Active)* 🚨\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📄 *ID:* `{case['case_id']}`\n"
                    f"🏦 *Bank:* {case['bank']}\n"
                    f"🏢 *Branch:* {case['branch']}\n"
                    f"⚠️ *Issue:* {case['issue']}\n"
                    f"📍 *District:* {case['district']}\n"
                    f"💬 *Comment:* {case['comment']}\n"
                    f"🕒 *Reported at:* {case['date_raw']} ({age_str})\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 _Status: Pending Action / Unresolved_"
                )
                # 🔗 ወደ Login ፔጅ ብቻ እንዲወስድ የተደረገ ሊንክ
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("Check in dashboard", url="https://tech24et.com/login")]])
                await context.bot.send_message(chat_id=chat_id, text=notif_text, reply_markup=kb, parse_mode="Markdown")
                SENT_CASES_TRACKER.add(case['case_id'])
    except Exception as e:
        logger.error(f"Error in instant start trigger: {str(e)}")

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE:
        return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")

    processing = await update.message.reply_text("⏳ Searching dashboard portal for Adama logs, please wait...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Connection Failure:*\n{status}", parse_mode="Markdown")

    pending_cases = [c for c in cases if c['status'] == "On going"]
    if not pending_cases:
        # 🔗 እዚህ ጋርም ወደ Login ፔጅ ብቻ እንዲወስድ ተስተካክሏል
        keyboard = [[InlineKeyboardButton("Check in dashboard", url="https://tech24et.com/login")]]
        return await update.message.reply_text(
            "✅ All Adama cases are completed! No pending cases found.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    if len(pending_cases) == 1:
        text, kb = build_case_detail_ui(pending_cases[0])
        await update.message.reply_text(text, reply_markup=kb)
    else:
        text = "The following ATM cases have been reported and are currently pending action. Select a case from the list below to view details."
        keyboard = []
        for c in pending_cases:
            _, relative_short = get_relative_time(c['date_obj'])
            button_lbl = f"{relative_short} | {c['case_id']} | {c['bank']} | {c['branch']}"
            keyboard.append([InlineKeyboardButton(button_lbl, callback_data=f"view_{c['case_id']}")])
            
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_action")])
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE:
        return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")

    text = "Select an Adama District Technician to view their weekly cases report (Monday - Sunday):"
    keyboard = []
    for tech in sorted(ALLOWED_TECHNICIANS):
        keyboard.append([InlineKeyboardButton(tech, callback_data=f"wrep_{tech}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE:
        return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")

    processing = await update.message.reply_text("⏳ Searching dashboard portal for Adama logs, please wait...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Error:* {status}", parse_mode="Markdown")

    report_text = format_weekly_summary_matrix(cases)
    await update.message.reply_text(report_text, parse_mode="Markdown")

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE:
        return await update.message.reply_text(get_maintenance_message(), parse_mode="Markdown")

    processing = await update.message.reply_text("⏳ Writing and formatting Excel spreadsheet...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Export Blocked:* {status}", parse_mode="Markdown")

    if not cases:
        return await update.message.reply_text("❌ *Export Cancelled:* No cases matched query scope.", parse_mode="Markdown")

    excel_file = generate_excel_bytes(cases)
    current_month = get_eat_now().strftime('%B').lower()
    current_year = get_eat_now().strftime('%Y')
    excel_file.name = f"case-report-{current_year}-{current_month}.xlsx"

    caption_text = (
        f"📊 *ATM Cases Report – {get_eat_now().strftime('%B %Y')}*\n\n"
        "This report contains all ATM cases reported for monitoring."
    )
    await context.bot.send_document(chat_id=update.effective_chat.id, document=excel_file, caption=caption_text, parse_mode="Markdown")

# ==========================================
# 9. INLINE BUTTON CALLBACK HANDLER
# ==========================================
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if MAINTENANCE_MODE:
        try: await query.edit_message_text(get_maintenance_message(), parse_mode="Markdown")
        except Exception: await context.bot.send_message(chat_id=update.effective_chat.id, text=get_maintenance_message(), parse_mode="Markdown")
        return

    data = query.data
    if data == "cancel_action":
        await query.message.delete()
        return

    if data.startswith("wrep_"):
        tech_name = data.split("_")[1]
        cases, status = await scrape_website_cases()
        if status != "OK":
            return await query.edit_message_text(f"❌ Error pulling logs: {status}")
        report_output = format_technician_weekly_report(cases, tech_name)
        back_kb = [[InlineKeyboardButton("🔙 Back to Technicians List", callback_data="back_to_techs")]]
        await query.edit_message_text(text=report_output, reply_markup=InlineKeyboardMarkup(back_kb), parse_mode="Markdown")
        return

    if data == "back_to_techs":
        text = "Select an Adama District Technician to view their weekly cases report (Monday - Sunday):"
        keyboard = []
        for tech in sorted(ALLOWED_TECHNICIANS):
            keyboard.append([InlineKeyboardButton(tech, callback_data=f"wrep_{tech}")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("askterm_"):
        case_id = data.split("_")[1]
        confirm_text = f"⚠️ *Confirmation Required*\n\nAre you sure you want to terminate/close Case ID: *{case_id}*?"
        confirm_keyboard = [
            [InlineKeyboardButton("🔙 Go Back", callback_data=f"view_{case_id}")],
            [InlineKeyboardButton("✅ Yes, Terminate", callback_data=f"do_terminate_{case_id}")],
            [InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_action")]
        ]
        await query.edit_message_text(text=confirm_text, reply_markup=InlineKeyboardMarkup(confirm_keyboard), parse_mode="Markdown")
        return

    if data.startswith("do_terminate_"):
        case_id = data.split("_")[2]
        await query.edit_message_text(f"⏳ Attempting terminal closure for Case ID `{case_id}`...", parse_mode="Markdown")
        success, err_msg = await terminate_case_on_dashboard(case_id)
        if success:
            await query.edit_message_text(f"✅ *Success!* Case ID `{case_id}` marked as Terminated.", parse_mode="Markdown")
        else:
            keyboard = [[InlineKeyboardButton("🔄 Try Again", callback_data=f"askterm_{case_id}")], [InlineKeyboardButton("Cancel", callback_data="cancel_action")]]
            await query.edit_message_text(text=f"❌ *Termination failed:*\n`{err_msg}`", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("view_"):
        case_id = data.split("_")[1]
        cases, status = await scrape_website_cases()
        if status != "OK":
            return await query.edit_message_text(f"❌ Scraper failure: {status}")
        target = next((c for c in cases if c['case_id'] == case_id), None)
        if not target:
            return await query.edit_message_text("❌ Record lost or completed in background.")
        text, kb = build_case_detail_ui(target)
        await query.edit_message_text(text, reply_markup=kb)

    elif data.startswith("refresh_"):
        case_id = data.split("_")[2] if len(data.split("_")) == 3 else data.split("_")[1]
        cases, status = await scrape_website_cases()
        if status != "OK":
            return await query.edit_message_text(f"❌ Scraper Sync Failed: {status}")
        target = next((c for c in cases if c['case_id'] == case_id), None)
        if not target:
            return await query.edit_message_text("❌ Record completed or deleted on portal.")
        text, kb = build_case_detail_ui(target)
        await query.edit_message_text(text, reply_markup=kb)

# ==========================================
# 10. STARTUP MENU INITIALIZER & LOOP STARTER
# ==========================================
async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Initialize bot profile"),
        BotCommand("pending", "View open and unresolved cases"),
        BotCommand("report", "View weekly technician performance metrics"),
        BotCommand("summary", "View overall weekly summary metrics"),
        BotCommand("export", "Generate incident logs Excel sheet")
    ]
    await application.bot.set_my_commands(commands)
    asyncio.create_task(start_independent_alarm_loop(application.bot))

# ==========================================
# 11. ENGINE INITIATION
# ==========================================
def main():
    if not BOT_TOKEN:
        logger.error("SYSTEM ERROR: TELEGRAM_BOT_TOKEN is missing.")
        return

    threading.Thread(target=run_health_server, daemon=True).start()

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
