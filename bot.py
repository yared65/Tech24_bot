import os
import logging
import asyncio
import threading
import json
import re
import urllib.parse
from datetime import datetime, timedelta
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

# Safely parse and convert NOTIFICATION_CHAT_ID to Integer if it is a number
raw_chat_id = os.environ.get("NOTIFICATION_CHAT_ID", "")
if raw_chat_id.startswith("-") or raw_chat_id.isdigit():
    try:
        NOTIFICATION_CHAT_ID = int(raw_chat_id)
    except ValueError:
        NOTIFICATION_CHAT_ID = raw_chat_id
else:
    NOTIFICATION_CHAT_ID = raw_chat_id

# In-memory tracker to prevent duplicate notifications during auto-polling
SENT_CASES_TRACKER = set()

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
# 3. ROBUST DEEP JSON PARSING HELPER
# ==========================================
def deep_extract_name(data, target_keyword):
    """
    Recursively searches inside dicts/lists to find values matching 
    the target keyword (e.g., 'name', 'bank_name', 'branch_name', etc.).
    """
    if not data:
        return None

    # If it is a dictionary
    if isinstance(data, dict):
        # Specific search for technician names combining First & Last name
        if target_keyword == 'technician':
            first = data.get('first_name') or data.get('name') or data.get('username')
            last = data.get('last_name') or ""
            if first:
                return f"{first} {last}".strip()

        # Specific search for keys containing our keyword
        for key in ['name', 'title', 'bank_name', 'branch_name', 'terminal_id', 'serial_number']:
            if key in data and data[key]:
                if isinstance(data[key], (dict, list)):
                    res = deep_extract_name(data[key], target_keyword)
                    if res: return res
                return str(data[key])
                
        for k, v in data.items():
            if target_keyword.lower() in k.lower() and v:
                if isinstance(v, (dict, list)):
                    res = deep_extract_name(v, target_keyword)
                    if res: return res
                return str(v)
                
        for v in data.values():
            if isinstance(v, (dict, list)):
                res = deep_extract_name(v, target_keyword)
                if res: return res

    # If it is a list
    elif isinstance(data, list):
        for item in data:
            res = deep_extract_name(item, target_keyword)
            if res:
                return res
                
    return None

def clean_raw_string(text):
    """
    Fallback regex cleaner for database-like dump fields.
    """
    if not text:
        return ""
    patterns = [
        r'[\'"]name[\'"]\s*:\s*[\'"]([^"\'}]+)[\'"]',
        r'[\'"]bank_name[\'"]\s*:\s*[\'"]([^"\'}]+)[\'"]',
        r'[\'"]branch_name[\'"]\s*:\s*[\'"]([^"\'}]+)[\'"]',
        r'[\'"]title[\'"]\s*:\s*[\'"]([^"\'}]+)[\'"]',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
            
    cleaned = re.sub(r'[{}\'"\[\]]', '', text)
    if 'name:' in cleaned:
        parts = cleaned.split('name:')
        if len(parts) > 1:
            return parts[1].split(',')[0].strip()
            
    return text.strip()

def extract_field(item, keyword):
    """
    Cleans database rows and extracts the most human-readable name.
    """
    if not item:
        return ""

    if isinstance(item, dict):
        extracted = deep_extract_name(item, keyword)
        if extracted:
            return extracted

    if isinstance(item, str):
        stripped = item.strip()
        if (stripped.startswith('{') and stripped.endswith('}')) or (stripped.startswith('[') and stripped.endswith(']')):
            try:
                valid_json_str = stripped.replace("'", '"').replace("None", "null").replace("True", "true").replace("False", "false")
                parsed = json.loads(valid_json_str)
                extracted = deep_extract_name(parsed, keyword)
                if extracted:
                    return extracted
            except Exception:
                pass
        
        return clean_raw_string(stripped)

    return str(item)

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
            # Refresh Session CSRF
            await session.get(csrf_url)
            xsrf_token = session.cookies.get("XSRF-TOKEN")
            if xsrf_token:
                session.headers.update({
                    'X-XSRF-TOKEN': urllib.parse.unquote(xsrf_token)
                })

            # Authenticate Session
            payload = {'email': EMAIL.strip(), 'password': PASSWORD.strip()}
            login_res = await session.post(login_url, json=payload)
            if login_res.status_code not in [200, 201, 204]:
                return [], f"Login failed! Code: {login_res.status_code}"

            # Fetch Target Log entries
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
                
                # Dynamic scope filter targeting Adama context
                raw_string_dump = str(entry).lower()
                if "adama" in raw_string_dump:
                    case_id = str(entry.get('callentry_id', entry.get('id', 'N/A')))
                    
                    # 1. CLEAN EXTRACTION FOR BANK, BRANCH & TERMINAL
                    bank = extract_field(entry.get('bank'), 'bank') or extract_field(entry, 'bank')
                    branch = extract_field(entry.get('branch'), 'branch') or extract_field(entry, 'branch')
                    terminal = extract_field(entry.get('terminal'), 'terminal') or extract_field(entry, 'terminal')
                    
                    # 2. ROBUST ISSUE (DESCRIPTION) SEARCH
                    issue = entry.get('description') or entry.get('issue') or entry.get('issue_description') or entry.get('title') or "N/A"
                    
                    # 3. ROBUST TECHNICIAN SEARCH (Checks sub-objects and combinations)
                    tech_obj = entry.get('technician') or entry.get('user') or {}
                    technician = ""
                    if isinstance(tech_obj, dict):
                        technician = extract_field(tech_obj, 'technician')
                    if not technician:
                        technician = extract_field(entry, 'technician') or "Assigned Tech"
                        
                    # 4. ROBUST PHONE SEARCH
                    tech_phone = "N/A"
                    if isinstance(tech_obj, dict):
                        tech_phone = tech_obj.get('phone') or tech_obj.get('mobile') or tech_obj.get('phone_number') or "N/A"
                    if tech_phone == "N/A":
                        tech_phone = entry.get('tech_phone') or entry.get('phone') or "N/A"
                        
                    comment = entry.get('comment', 'No comments.')
                    district = "Adama"
                    
                    # 5. ROBUST REPORTED DATE SEARCH
                    created_at = entry.get('created_at') or entry.get('reported_at') or entry.get('start_date') or ''
                    date_str = str(created_at)[:16].replace("T", " ") if created_at else "N/A"
                    
                    try:
                        date_obj = datetime.strptime(str(created_at)[:19], "%Y-%m-%dT%H:%M:%S")
                    except Exception:
                        try:
                            date_obj = datetime.strptime(str(created_at)[:10], "%Y-%m-%d")
                        except Exception:
                            date_obj = datetime.now()

                    # 6. ADVANCED STATUS RESOLUTION (FIX FOR COMPLETED CASES SHOWING PENDING)
                    status_raw = str(entry.get('status', '')).strip().lower()
                    progress_raw = str(entry.get('progress', '')).strip().lower()
                    is_closed_val = entry.get('is_closed')
                    
                    closed_keywords = ["complete", "completed", "done", "close", "closed", "resolved", "terminated", "success", "success-closed"]
                    
                    is_completed = False
                    # Check status string
                    if any(kw in status_raw for kw in closed_keywords):
                        is_completed = True
                    # Check progress string
                    elif any(kw in progress_raw for kw in closed_keywords):
                        is_completed = True
                    # Check boolean status
                    elif is_closed_val is True or str(is_closed_val).lower() in ['true', '1', 'yes']:
                        is_completed = True

                    status_text = "Completed" if is_completed else "Pending"

                    scraped_cases.append({
                        'case_id': case_id,
                        'bank': bank if bank else "Awash Bank",
                        'district': district,
                        'branch': branch if branch else "Adama Branch",
                        'terminal': terminal if terminal else "ATM",
                        'issue': issue,
                        'status': status_text,
                        'comment': comment,
                        'technician': technician,
                        'tech_phone': tech_phone,
                        'date': date_str,
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
# 5. AUTOMATIC 10-MINUTE PENDING MONITOR
# ==========================================
async def auto_monitor_dashboard(context: ContextTypes.DEFAULT_TYPE):
    if not NOTIFICATION_CHAT_ID:
        logger.warning("NOTIFICATION_CHAT_ID variable is missing! Auto-polling paused.")
        return

    logger.info("Running scheduled 10-minute automated dashboard query...")
    cases, status = await scrape_website_cases()
    if status != "OK":
        logger.error(f"Auto-monitor failed to fetch cases: {status}")
        return

    pending_cases = [c for c in cases if c['status'] == "Pending"]
    if not pending_cases:
        logger.info("Auto-monitor scan finished: No pending cases found.")
        return

    new_pending_cases = []
    for c in pending_cases:
        if c['case_id'] not in SENT_CASES_TRACKER:
            new_pending_cases.append(c)
            SENT_CASES_TRACKER.add(c['case_id'])

    if not new_pending_cases:
        return

    if len(new_pending_cases) == 1:
        case = new_pending_cases[0]
        text, kb = build_case_detail_ui(case)
        await context.bot.send_message(
            chat_id=NOTIFICATION_CHAT_ID,
            text=f"🚨 *NEW PENDING ATM CASE DETECTED!*\n\n{text}",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    else:
        text = f"🚨 *{len(new_pending_cases)} New Pending Cases Detected!*\nSelect a case button below to view details and actions:"
        keyboard = []
        for case in new_pending_cases:
            clean_bank = extract_field(case['bank'], 'bank') or case['bank']
            clean_branch = extract_field(case['branch'], 'branch') or case['branch']
            button_label = f"🏧 {clean_bank} - {clean_branch} (ID: {case['case_id']})"
            keyboard.append([InlineKeyboardButton(button_label, callback_data=f"view_{case['case_id']}")])
        
        await context.bot.send_message(
            chat_id=NOTIFICATION_CHAT_ID,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

# ==========================================
# 6. DYNAMIC UI BUILDERS
# ==========================================
def build_case_detail_ui(case):
    # Deep extract and clean all fields for the interface
    clean_bank = extract_field(case['bank'], 'bank') or str(case['bank'])
    clean_branch = extract_field(case['branch'], 'branch') or str(case['branch'])
    clean_terminal = extract_field(case['terminal'], 'terminal') or extract_field(case['terminal'], 'serial_number') or str(case['terminal'])
    clean_tech = extract_field(case['technician'], 'technician') or str(case['technician'])

    # Safely escape text to avoid Markdown parsing exceptions
    safe_bank = clean_bank.replace("_", "\\_").replace("*", "\\*")
    safe_branch = clean_branch.replace("_", "\\_").replace("*", "\\*")
    safe_terminal = clean_terminal.replace("_", "\\_").replace("*", "\\*")
    safe_issue = str(case['issue']).replace("_", "\\_").replace("*", "\\*")
    safe_tech = clean_tech.replace("_", "\\_").replace("*", "\\*")
    
    text = (
        f"📋 *Case ID:* `{case['case_id']}`\n"
        f"🏧 *Terminal:* {safe_terminal}\n"
        f"🏛 *Bank:* {safe_bank}\n"
        f"📍 *Branch:* {safe_branch}\n"
        f"⚠️ *Issue:* {safe_issue}\n"
        f"📌 *Status:* {case['status']}\n"
        f"🌍 *District:* {case['district']}\n"
        f"💬 *Comment:* {case['comment']}\n"
        f"👤 *Technician:* {safe_tech}\n"
        f"📞 *Tech Phone:* {case['tech_phone']}\n"
        f"📅 *Reported At:* {case['date']} (EAT)\n"
    )
    keyboard = [
        [
            InlineKeyboardButton("🛑 Terminate", callback_data=f"terminate_{case['case_id']}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{case['case_id']}")
        ],
        [InlineKeyboardButton("❌ Dismiss Panel", callback_data="cancel_action")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

# ==========================================
# 7. EXCEL & REPORT ENGINE GENERATORS
# ==========================================
def format_summary_report(cases, days_limit=7, title="Weekly"):
    now = datetime.now()
    cutoff_date = now - timedelta(days=days_limit)
    filtered_cases = [c for c in cases if c['date_obj'] >= cutoff_date]

    if not filtered_cases:
        return f"📭 No cases recorded on dashboard in the past {days_limit} days."

    report_lines = [f"🏧 *{title} Report /Yared Girma/*\n"]

    for case in filtered_cases:
        try:
            date_formatted = case['date_obj'].strftime("%d/%m/%Y")
        except Exception:
            date_formatted = case['date']
            
        clean_bank = extract_field(case['bank'], 'bank') or case['bank']
        clean_branch = extract_field(case['branch'], 'branch') or case['branch']

        case_string = (
            f"®️ *{date_formatted}* Registered | {clean_branch} | "
            f"{clean_bank} | ({case['issue']}) | {case['status']}"
        )
        report_lines.append(case_string)

    report_lines.append("\n*Generally*")

    bank_analytics = {}
    for case in filtered_cases:
        b_name = extract_field(case['bank'], 'bank') or case['bank']
        if b_name not in bank_analytics:
            bank_analytics[b_name] = {"registered": 0, "completed": 0}
        
        bank_analytics[b_name]["registered"] += 1
        if case['status'] == "Completed":
            bank_analytics[b_name]["completed"] += 1

    for bank_name, stats in bank_analytics.items():
        summary_line = f"🏛 *{bank_name}* Registered {stats['registered']}  |  Completed - {stats['completed']}"
        report_lines.append(summary_line)

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
        clean_bank = extract_field(case['bank'], 'bank') or case['bank']
        clean_branch = extract_field(case['branch'], 'branch') or case['branch']
        clean_terminal = extract_field(case['terminal'], 'terminal') or extract_field(case['terminal'], 'serial_number') or case['terminal']
        clean_tech = extract_field(case['technician'], 'technician') or case['technician']

        row_data = [
            case['case_id'], clean_terminal, clean_bank, clean_branch,
            case['issue'], case['status'], case['district'], case['comment'],
            clean_tech, case['tech_phone'], case['date']
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
    welcome_text = (
        "👋 *Welcome to Tech24 Adama District Bot*\n\n"
        "💻 *Available Commands Menu:*\n"
        "• /pending - View currently open / unresolved cases\n"
        "• /terminate - View pending cases to select & complete\n"
        "• /report - View weekly formatted summary report\n"
        "• /monthly - View monthly formatted summary report\n"
        "• /export - Download structured incident Excel spreadsheets"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Processing live dashboard query...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Connection Failure:*\n{status}", parse_mode="Markdown")

    pending_cases = [c for c in cases if c['status'] == "Pending"]
    if not pending_cases:
        return await update.message.reply_text("✅ *Awesome!* No unresolved cases in Adama dashboard.", parse_mode="Markdown")

    if len(pending_cases) == 1:
        text, kb = build_case_detail_ui(pending_cases[0])
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        text = f"📋 *Adama Active Logs ({len(pending_cases)} Pending)*\nSelect a button to view specific case actions:"
        keyboard = []
        for c in pending_cases:
            clean_bank = extract_field(c['bank'], 'bank') or c['bank']
            clean_branch = extract_field(c['branch'], 'branch') or c['branch']
            button_lbl = f"🏧 {clean_bank} - {clean_branch} (ID: {c['case_id']})"
            keyboard.append([InlineKeyboardButton(button_lbl, callback_data=f"view_{c['case_id']}")])
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def terminate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Requesting terminal targets...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Error:* {status}", parse_mode="Markdown")

    pending_cases = [c for c in cases if c['status'] == "Pending"]
    if not pending_cases:
        return await update.message.reply_text("✅ No active targets available for closure.", parse_mode="Markdown")

    text = "🛑 *Select Case ID to terminate/complete:* "
    keyboard = []
    for c in pending_cases:
        clean_bank = extract_field(c['bank'], 'bank') or c['bank']
        clean_branch = extract_field(c['branch'], 'branch') or c['branch']
        button_lbl = f"Case {c['case_id']} - {clean_bank} ({clean_branch})"
        keyboard.append([InlineKeyboardButton(button_lbl, callback_data=f"terminate_{c['case_id']}")])
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Fetching dashboard Weekly records...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Database Query Refused:*\n{status}", parse_mode="Markdown")

    report_text = format_summary_report(cases, days_limit=7, title="Weekly")
    await update.message.reply_text(report_text, parse_mode="Markdown")

async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Fetching dashboard Monthly records...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Database Query Refused:*\n{status}", parse_mode="Markdown")

    report_text = format_summary_report(cases, days_limit=30, title="Monthly")
    await update.message.reply_text(report_text, parse_mode="Markdown")

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Writing and formatting Excel spreadsheet...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Export Blocked:* {status}", parse_mode="Markdown")

    if not cases:
        return await update.message.reply_text("❌ *Export Cancelled:* No cases matched query scope.", parse_mode="Markdown")

    excel_file = generate_excel_bytes(cases)
    excel_file.name = f"ATM_Incident_Report_Adama_{datetime.now().strftime('%Y%m%d')}.xlsx"

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=excel_file,
        caption="📊 *Clean ATM database dump structured successfully.*",
        parse_mode="Markdown"
    )

# ==========================================
# 9. INLINE BUTTON CALLBACK HANDLER
# ==========================================
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel_action":
        await query.message.delete()
        return

    if data.startswith("view_"):
        case_id = data.split("_")[1]
        cases, status = await scrape_website_cases()
        if status != "OK":
            return await query.edit_message_text(f"❌ Scraper failure while loading case: {status}")
        
        target = next((c for c in cases if c['case_id'] == case_id), None)
        if not target:
            return await query.edit_message_text("❌ Record lost or completed in background.")

        text, kb = build_case_detail_ui(target)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

    elif data.startswith("refresh_"):
        case_id = data.split("_")[2] if len(data.split("_")) == 3 else data.split("_")[1]
        cases, status = await scrape_website_cases()
        if status != "OK":
            return await query.edit_message_text(f"❌ Scraper Sync Failed: {status}")

        target = next((c for c in cases if c['case_id'] == case_id), None)
        if not target:
            return await query.edit_message_text("❌ Record completed or deleted on portal.")

        text, kb = build_case_detail_ui(target)
        await query.edit_message_text(f"🔄 *Refreshed at:* {datetime.now().strftime('%H:%M:%S')}\n\n{text}", reply_markup=kb, parse_mode="Markdown")

    elif data.startswith("terminate_"):
        case_id = data.split("_")[1]
        await query.edit_message_text(f"⏳ Attempting terminal state authorization closure for Case ID `{case_id}`...", parse_mode="Markdown")
        
        success, err_msg = await terminate_case_on_dashboard(case_id)
        if success:
            await query.edit_message_text(f"✅ *Success!* Case ID `{case_id}` marked as Terminated on active dashboard database.", parse_mode="Markdown")
        else:
            keyboard = [[InlineKeyboardButton("🔄 Try Again", callback_data=f"terminate_{case_id}")],
                        [InlineKeyboardButton("❌ Dismiss Panel", callback_data="cancel_action")]]
            await query.edit_message_text(
                text=f"❌ *Auth/Session rejection during termination attempt:*\n`{err_msg}`",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

# ==========================================
# 10. STARTUP ENGLISH MENU INITIALIZER
# ==========================================
async def post_init(application: Application) -> None:
    logger.info("Setting bot command definitions cleanly to English layout during startup sequence...")
    commands = [
        BotCommand("start", "Initialize bot profile"),
        BotCommand("pending", "View open and unresolved cases"),
        BotCommand("terminate", "Access case termination actions"),
        BotCommand("report", "View weekly performance metrics"),
        BotCommand("monthly", "View monthly performance metrics"),
        BotCommand("export", "Generate incident logs Excel sheet")
    ]
    await application.bot.set_my_commands(commands)
    logger.info("English Menu commands loaded successfully onto API profile.")

# ==========================================
# 11. ENGINE INITIATION
# ==========================================
def main():
    if not BOT_TOKEN:
        logger.error("SYSTEM ERROR: TELEGRAM_BOT_TOKEN environment variable is missing.")
        return

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
    job_queue.run_repeating(auto_monitor_dashboard, interval=600, first=10)
    logger.info("Auto-monitor dashboard background task configured to cycle every 10 minutes.")

    logger.info("Bot application polling successfully initiated.")
    application.run_polling()

if __name__ == '__main__':
    main()
