import os
import logging
import asyncio
import threading
import json
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

raw_chat_id = os.environ.get("NOTIFICATION_CHAT_ID", "")
if raw_chat_id.startswith("-") or raw_chat_id.isdigit():
    try:
        NOTIFICATION_CHAT_ID = int(raw_chat_id)
    except ValueError:
        NOTIFICATION_CHAT_ID = raw_chat_id
else:
    NOTIFICATION_CHAT_ID = raw_chat_id

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
    """
    Recursively scans and extracts deep nested key names from raw dictionaries.
    """
    if not data:
        return ""
    
    parsed_data = safe_parse_json(data) if isinstance(data, str) else data
    if not isinstance(parsed_data, dict):
        return str(parsed_data)

    # Search list of candidate sub-keys
    for key in key_hierarchy:
        if key in parsed_data:
            val = parsed_data[key]
            if isinstance(val, dict):
                return clean_extracted_value(val, key_hierarchy)
            return str(val)
            
    # Fallback to general child object scans
    for k, v in parsed_data.items():
        if isinstance(v, dict):
            res = clean_extracted_value(v, key_hierarchy)
            if res:
                return res
    return ""

def get_relative_time(date_obj):
    now = datetime.now()
    diff = now - date_obj
    seconds = diff.total_seconds()
    
    if seconds < 0:
        return "just now", "now"
        
    minutes = int(seconds // 60)
    hours = int(minutes // 60)
    days = int(hours // 24)
    
    if days > 0:
        return f"about {days} day{'s' if days > 1 else ''} ago", f"{days}d"
    elif hours > 0:
        return f"about {hours} hour{'s' if hours > 1 else ''} ago", f"{hours}h"
    elif minutes > 0:
        return f"about {minutes} minute{'s' if minutes > 1 else ''} ago", f"{minutes}m"
    else:
        return "just now", "now"

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
                    case_id = str(entry.get('callentry_id', entry.get('id', 'N/A')))
                    
                    # 1. Bank Name Extraction
                    bank = clean_extracted_value(entry.get('bank'), ['bank_name', 'bankname', 'name', 'title'])
                    if not bank:
                        bank = clean_extracted_value(entry, ['bank_name', 'bankname'])
                    if not bank or bank.isdigit():
                        bank = "Awash"

                    # 2. Branch Name Extraction
                    branch = clean_extracted_value(entry.get('branch'), ['branch_name', 'branchname', 'name', 'title'])
                    if not branch:
                        branch = clean_extracted_value(entry, ['branch_name', 'branchname'])
                    if not branch or branch.isdigit():
                        branch = "Adama Branch"

                    # 3. Terminal/ATM Name Extraction
                    terminal = clean_extracted_value(entry.get('terminal'), ['atmterminal_name', 'atmterminal_no', 'terminal', 'name'])
                    if not terminal:
                        terminal = clean_extracted_value(entry, ['atmterminal_name', 'atmterminal_no', 'terminal'])
                    if not terminal or '{' in terminal:
                        terminal = "ATM_1"

                    # 4. Issue Extraction
                    raw_issue = entry.get('description') or entry.get('issue') or "ATM"
                    issue = clean_extracted_value(raw_issue, ['issuesubcat_name', 'issuecat_name', 'name', 'title'])
                    if not issue or '{' in issue:
                        issue = "ATM Issue"

                    # 5. Technician Extraction
                    tech_data = entry.get('technician')
                    technician = "Not Assigned"
                    tech_phone = "N/A"
                    if tech_data and isinstance(tech_data, dict):
                        if 'callentry_id' not in tech_data:
                            technician = tech_data.get('name', tech_data.get('username', 'Not Assigned'))
                            tech_phone = tech_data.get('phone', 'N/A')

                    comment = entry.get('comment') or "No comments."
                    district = "Adama"
                    
                    created_at = entry.get('created_at', entry.get('start_date', ''))
                    date_str = str(created_at)[:19].replace("T", " ") if created_at else "N/A"
                    
                    try:
                        date_obj = datetime.strptime(str(created_at)[:19], "%Y-%m-%dT%H:%M:%S")
                    except Exception:
                        try:
                            date_obj = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            try:
                                date_obj = datetime.strptime(str(created_at)[:10], "%Y-%m-%d")
                            except Exception:
                                date_obj = datetime.now()

                    # 6. Status Extraction
                    status_raw = ""
                    for k in ['callentry_status', 'callentry_progress', 'status', 'progress']:
                        val = entry.get(k)
                        if val:
                            status_raw = str(val).lower()
                            break
                    
                    if not status_raw:
                        for parent_key in ['technician', 'description']:
                            parent_val = entry.get(parent_key)
                            if isinstance(parent_val, dict):
                                for k in ['callentry_status', 'callentry_progress', 'status', 'progress']:
                                    if k in parent_val:
                                        status_raw = str(parent_val[k]).lower()
                                        break
                                if status_raw:
                                    break

                    if status_raw in ["complete", "completed", "done", "1"]:
                        status_text = "Completed"
                    else:
                        status_text = "On going"

                    scraped_cases.append({
                        'case_id': case_id,
                        'bank': bank,
                        'district': district,
                        'branch': branch,
                        'terminal': terminal,
                        'atm_name': terminal,
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
# 5. AUTOMATIC 10-MINUTE PENDING MONITOR
# ==========================================
async def auto_monitor_dashboard(context: ContextTypes.DEFAULT_TYPE):
    if not NOTIFICATION_CHAT_ID:
        return

    cases, status = await scrape_website_cases()
    if status != "OK":
        return

    pending_cases = [c for c in cases if c['status'] == "On going"]
    if not pending_cases:
        return

    new_pending_cases = []
    for c in pending_cases:
        if c['case_id'] not in SENT_CASES_TRACKER:
            new_pending_cases.append(c)
            SENT_CASES_TRACKER.add(c['case_id'])

    if not new_pending_cases:
        return

    for case in new_pending_cases:
        notif_text = (
            f"*ATM Incident Notification*\n\n"
            f"📄 *ID:* {case['case_id']}\n"
            f"🏦 *Bank:* {case['bank']}\n"
            f"⚠️ *Issue:* {case['issue']}\n"
            f"🏢 *Branch:* {case['branch']}\n"
            f"📍 *District:* {case['district']}\n"
            f"💬 *Comment:* {case['comment']}\n"
            f"🕒 *Reported at:* {case['date_raw']}"
        )
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Open In tech24et.com dashboard", url="https://tech24et.com/")]
        ])
        
        await context.bot.send_message(
            chat_id=NOTIFICATION_CHAT_ID,
            text=notif_text,
            reply_markup=kb,
            parse_mode="Markdown"
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
        f"ATM Name: {case['atm_name']}\n"
        f"Comment: {case['comment']}\n"
        f"Technician: {case['technician']}\n"
        f"Technician Phone: {case['tech_phone']}\n"
        f"Reported At: {case['date_raw']} (East Africa Time)\n"
        f"Relative Time: {relative_long}"
    )
    
    keyboard = [
        [InlineKeyboardButton("Terminate", callback_data=f"askterm_{case['case_id']}")],
        [InlineKeyboardButton("Refresh", callback_data=f"refresh_{case['case_id']}")],
        [InlineKeyboardButton("Cancel", callback_data="cancel_action")]
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

    report_lines = [f"📋 *{title} Report /yared Girma/* 📋\n"]

    for idx, case in enumerate(filtered_cases, start=1):
        try:
            date_formatted = case['date_obj'].strftime("%d/%m/%Y %I:%M %p")
        except Exception:
            date_formatted = case['date_raw']
            
        case_string = (
            f"{idx}. ID: {case['case_id']}\n"
            f"🏦 Bank: {case['bank']} ({case['branch']})\n"
            f"⚠️ Issue: {case['issue']}\n"
            f"📅 Date: {date_formatted}\n"
            f"📌 Status: {'✅' if case['status'] == 'Completed' else '⏳'} {case['status']}\n"
            f"----------------------------------------"
        )
        report_lines.append(case_string)

    report_lines.append("\n*Generally*")

    bank_analytics = {}
    for case in filtered_cases:
        b_name = case['bank']
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
    welcome_text = (
        "👋 *Welcome to Tech24 Adama District Bot*\n\n"
        "💻 *Available Commands Menu:*\n"
        "• /pending - View currently open / unresolved cases\n"
        "• /report - View weekly performance metrics\n"
        "• /monthly - View monthly performance metrics\n"
        "• /export - Download structured incident Excel spreadsheets"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Searching for unresolved Adama cases...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Connection Failure:*\n{status}", parse_mode="Markdown")

    pending_cases = [c for c in cases if c['status'] == "On going"]
    if not pending_cases:
        keyboard = [[InlineKeyboardButton("Check in dashboard", url="https://tech24et.com/")]]
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
            
            # STYLED BUTTON NAME: [Time] | [CaseID] | [Bank] | [Branch]
            button_lbl = f"{relative_short} | {c['case_id']} | {c['bank']} | {c['branch']}"
            keyboard.append([InlineKeyboardButton(button_lbl, callback_data=f"view_{c['case_id']}")])
            
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_action")])
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Fetching Adama records, please wait...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Error:* {status}", parse_mode="Markdown")

    report_text = format_summary_report(cases, days_limit=7, title="Weekly")
    await update.message.reply_text(report_text, parse_mode="Markdown")

async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Fetching Adama records, please wait...")
    cases, status = await scrape_website_cases()
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ *Error:* {status}", parse_mode="Markdown")

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
    current_month = datetime.now().strftime('%B').lower()
    excel_file.name = f"case-report-2026-{current_month}.xlsx"

    caption_text = (
        f"📊 *ATM Cases Report – {datetime.now().strftime('%B %Y')}*\n\n"
        "This report contains all ATM cases reported for monitoring."
    )

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=excel_file,
        caption=caption_text,
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

    if data.startswith("askterm_"):
        case_id = data.split("_")[1]
        confirm_text = (
            f"⚠️ *Confirmation Required*\n\n"
            f"Are you sure you want to terminate/close Case ID: *{case_id}*?"
        )
        confirm_keyboard = [
            [InlineKeyboardButton("🔙 Go Back", callback_data=f"view_{case_id}")],
            [InlineKeyboardButton("✅ Yes, Terminate", callback_data=f"do_terminate_{case_id}")],
            [InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_action")]
        ]
        await query.edit_message_text(
            text=confirm_text,
            reply_markup=InlineKeyboardMarkup(confirm_keyboard),
            parse_mode="Markdown"
        )
        return

    if data.startswith("do_terminate_"):
        case_id = data.split("_")[2]
        await query.edit_message_text(f"⏳ Attempting terminal closure for Case ID `{case_id}`...", parse_mode="Markdown")
        
        success, err_msg = await terminate_case_on_dashboard(case_id)
        if success:
            await query.edit_message_text(f"✅ *Success!* Case ID `{case_id}` marked as Terminated.", parse_mode="Markdown")
        else:
            keyboard = [
                [InlineKeyboardButton("🔄 Try Again", callback_data=f"askterm_{case_id}")],
                [InlineKeyboardButton("Cancel", callback_data="cancel_action")]
            ]
            await query.edit_message_text(
                text=f"❌ *Termination failed:*\n`{err_msg}`",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
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
# 10. STARTUP MENU INITIALIZER
# ==========================================
async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Initialize bot profile"),
        BotCommand("pending", "View open and unresolved cases"),
        BotCommand("report", "View weekly performance metrics"),
        BotCommand("monthly", "View monthly performance metrics"),
        BotCommand("export", "Generate incident logs Excel sheet")
    ]
    await application.bot.set_my_commands(commands)

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
    application.add_handler(CommandHandler("monthly", monthly_command))
    application.add_handler(CommandHandler("export", export_command))
    
    application.add_handler(CallbackQueryHandler(button_click_handler))

    job_queue = application.job_queue
    job_queue.run_repeating(auto_monitor_dashboard, interval=600, first=10)

    application.run_polling()

if __name__ == '__main__':
    main()
