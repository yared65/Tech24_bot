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
NOTIFICATION_CHAT_ID = os.environ.get("NOTIFICATION_CHAT_ID") 

# ቀደም ሲል የተላኩ ኬዞችን መመዝገቢያ (ደጋግሞ ኖቲፊኬሽን እንዳይልክ ለመከላከል)
SENT_CASES_TRACKER = set()

# ==========================================
# 2. FLASK SERVER FOR UPTIME (RENDER)
# ==========================================
app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

@app.route('/')
def home():
    return "OK", 200

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Flask server on port {port}...")
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

# ==========================================
# 3. JSON PARSING HELPERS (CLEAN TEXTS)
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
    parsed = safe_parse_json(item)
    if not parsed:
        if isinstance(item, str):
            return item
        return ""
    
    for k, v in parsed.items():
        if k.lower() in [keyword.lower(), f"{keyword.lower()}name", f"{keyword.lower()}_name"]:
            if isinstance(v, dict):
                return v.get('name', v.get('title', str(v)))
            return str(v)
            
    for k, v in parsed.items():
        if keyword.lower() in k.lower():
            if isinstance(v, dict):
                return v.get('name', v.get('title', str(v)))
            return str(v)
    return ""

# ==========================================
# 4. API SCRAPER & ACTION ENGINES
# ==========================================
async def scrape_website_cases():
    if not EMAIL or not PASSWORD:
        return [], "Error: EMAIL or PASSWORD environment variables missing!"

    login_url = "https://api.tech24et.com/api/auth/login"
    data_url = "https://api.tech24et.com/api/callentries"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            payload = {"email": EMAIL, "password": PASSWORD}
            login_res = await client.post(login_url, json=payload)
            if login_res.status_code != 200:
                return [], f"Login failed: {login_res.status_code}"
            
            token = ""
            try:
                token = login_res.json().get("token", login_res.json().get("access_token", ""))
            except Exception:
                pass

            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            response = await client.get(data_url, headers=headers)
            if response.status_code != 200:
                return [], f"Failed data fetch: {response.status_code}"

            raw_cases = response.json()
            cleaned_cases = []
            
            for item in raw_cases:
                bank_name = extract_field(item.get("bank", ""), "bank") or extract_field(item.get("bank_id", ""), "name")
                branch_name = extract_field(item.get("branch", ""), "branch") or extract_field(item.get("branch_id", ""), "name")
                terminal_id = extract_field(item.get("terminal", ""), "atmterminal") or extract_field(item.get("terminal_id", ""), "name") or item.get("terminal_id", "")
                issue_desc = extract_field(item.get("issue_description", ""), "issuecat") or item.get("issue_description") or item.get("Issue Descrp") or "No issue description"
                technician = extract_field(item.get("assigned_to", ""), "user") or item.get("assigned_to_id", "Unassigned")

                created_at_str = item.get("created_at") or item.get("Creation Tim") or ""
                parsed_date = None
                if created_at_str:
                    try:
                        parsed_date = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    except Exception:
                        try:
                            parsed_date = datetime.strptime(created_at_str, "%d/%m/%Y")
                        except Exception:
                            parsed_date = datetime.now()
                else:
                    parsed_date = datetime.now()

                case_obj = {
                    "case_id": str(item.get("id") or item.get("Case ID") or ""),
                    "bank": bank_name if bank_name else "Unknown Bank",
                    "branch": branch_name if branch_name else "Unknown Branch",
                    "terminal": terminal_id if terminal_id else "Unknown Terminal",
                    "issue": issue_desc,
                    "status": item.get("status") or item.get("Resolution S") or "Pending",
                    "comment": item.get("comment") or item.get("Notes/Comments") or "None",
                    "technician": technician if technician else "Unassigned",
                    "date_obj": parsed_date,
                    "date": parsed_date.strftime("%d/%m/%Y %I:%M %p")
                }
                cleaned_cases.append(case_obj)
                
            return cleaned_cases, "OK"
        except Exception as e:
            logger.error(f"Error in scraping: {e}")
            return [], str(e)

async def terminate_case_on_dashboard(case_id):
    terminate_url = f'https://api.tech24et.com/api/callentries/{case_id}/close'
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            login_url = "https://api.tech24et.com/api/auth/login"
            payload = {"email": EMAIL, "password": PASSWORD}
            login_res = await client.post(login_url, json=payload)
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }
            try:
                token = login_res.json().get("token", login_res.json().get("access_token", ""))
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            except Exception:
                pass

            response = await client.post(terminate_url, json={"status": "Completed"}, headers=headers)
            if response.status_code in [200, 201, 204]:
                return True, "Successfully terminated."
            return False, f"Error code: {response.status_code}"
        except Exception as e:
            return False, str(e)

# ==========================================
# 5. AUTOMATIC 10-MINUTE PENDING MONITOR
# ==========================================
async def auto_monitor_dashboard(context: ContextTypes.DEFAULT_TYPE):
    if not NOTIFICATION_CHAT_ID:
        logger.warning("NOTIFICATION_CHAT_ID is not configured!")
        return
        
    logger.info("Auto-monitoring check started...")
    cases, status = await scrape_website_cases()
    if status != "OK":
        logger.error(f"Scrape failed during auto-monitor: {status}")
        return

    pending = [c for c in cases if c['status'].lower() not in ["completed", "terminated", "resolved"]]
    
    if not pending:
        logger.info("No pending cases found during check.")
        return

    # ሁኔታ 1፦ አንድ Pending ኬዝ ብቻ ሲኖር (2ኛው ፎቶ ፎርማት ይልካል)
    if len(pending) == 1:
        case = pending[0]
        if case['case_id'] not in SENT_CASES_TRACKER:
            text = (
                f"📋 **Case ID Details: {case['case_id']}**\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🏛 **Bank:** {case['bank']}\n"
                f"📍 **Branch:** {case['branch']}\n"
                f"🖥 **Terminal:** {case['terminal']}\n"
                f"⚠️ **Issue:** {case['issue']}\n"
                f"🔴 **Status:** {case['status']}\n"
                f"👤 **Technician Assigned:** {case['technician']}\n"
                f"📅 **Logged Time:** {case['date']} (EAT)"
            )
            keyboard = [
                [InlineKeyboardButton("🛑 Terminate Case", callback_data=f"term_{case['case_id']}")]
            ]
            try:
                await context.bot.send_message(
                    chat_id=NOTIFICATION_CHAT_ID,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                SENT_CASES_TRACKER.add(case['case_id'])
            except Exception as e:
                logger.error(f"Failed sending single case alert: {e}")

    # ሁኔታ 2፦ ሁለት እና ከዚያ በላይ ኬዝ ሲኖር (3ኛው ፎቶ ፎርማት ይልካል)
    else:
        new_cases_found = any(c['case_id'] not in SENT_CASES_TRACKER for c in pending)
        if new_cases_found:
            text = "⚠️ **Multiple Pending ATM cases have been reported.** Select a case below to view details or proceed with resolution:"
            keyboard = []
            for case in pending[:15]:
                button_label = f"{case['case_id']} | {case['bank']} | {case['branch']}"
                keyboard.append([InlineKeyboardButton(button_label, callback_data=f"view_{case['case_id']}")])
                SENT_CASES_TRACKER.add(case['case_id'])
            
            try:
                await context.bot.send_message(
                    chat_id=NOTIFICATION_CHAT_ID,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed sending multiple cases alert: {e}")

# ==========================================
# 6. MANUAL DYNAMIC UI DISPLAY FOR /PENDING
# ==========================================
async def send_pending_cases_ui(chat_id, context: ContextTypes.DEFAULT_TYPE):
    cases, status = await scrape_website_cases()
    if status != "OK":
        return await context.bot.send_message(chat_id=chat_id, text=f"❌ Error: {status}")

    pending = [c for c in cases if c['status'].lower() not in ["completed", "terminated", "resolved"]]
    
    if not pending:
        return await context.bot.send_message(chat_id=chat_id, text="✨ No actions needed. All logged issues are completed.")

    if len(pending) == 1:
        case = pending[0]
        text = (
            f"📋 **Case ID Details: {case['case_id']}**\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🏛 **Bank:** {case['bank']}\n"
            f"📍 **Branch:** {case['branch']}\n"
            f"🖥 **Terminal:** {case['terminal']}\n"
            f"⚠️ **Issue:** {case['issue']}\n"
            f"🔴 **Status:** {case['status']}\n"
            f"👤 **Technician Assigned:** {case['technician']}\n"
            f"📅 **Logged Time:** {case['date']} (EAT)"
        )
        keyboard = [
            [InlineKeyboardButton("🛑 Terminate Case", callback_data=f"term_{case['case_id']}")],
            [InlineKeyboardButton("🔄 Refresh Details", callback_data="refresh_pending")]
        ]
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    else:
        text = "The following ATM cases have been reported and are currently pending action. Select a case from the list below to view details and proceed with resolution."
        keyboard = []
        for case in pending[:15]:
            button_label = f"{case['case_id']} | {case['bank']} | {case['branch']}"
            keyboard.append([InlineKeyboardButton(button_label, callback_data=f"view_{case['case_id']}")])
        
        keyboard.append([InlineKeyboardButton("🔄 Refresh List", callback_data="refresh_pending")])
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 7. EXCEL & REPORT ENGINES
# ==========================================
def format_summary_report(cases, days_limit=7, is_weekly=True):
    now = datetime.now()
    cutoff_date = now - timedelta(days=days_limit)
    filtered_cases = [c for c in cases if c['date_obj'] >= cutoff_date]
    
    title = f"Weekly report/yared Girma/" if is_weekly else "Monthly report/yared Girma/"
    if not filtered_cases:
        return f"📊 **{title}**\n\nNo records found for this period."

    filtered_cases.sort(key=lambda x: x['date_obj'])
    
    report_lines = []
    report_lines.append(title)
    report_lines.append("")

    bank_stats = {}

    for case in filtered_cases:
        date_str = case['date'].split(" ")[0]
        branch = case['branch']
        bank = case['bank']
        issue = case['issue']
        status = case['status']
        
        line = f"®{date_str} Registered |{branch} Branch |{bank}({issue} |{status}"
        report_lines.append(line)
        report_lines.append("")
        
        if bank not in bank_stats:
            bank_stats[bank] = {"registered": 0, "completed": 0}
        
        bank_stats[bank]["registered"] += 1
        if status.lower() in ["completed", "terminated", "resolved"]:
            bank_stats[bank]["completed"] += 1

    report_lines.append("     Generally ")
    report_lines.append("")
    
    for bank, stats in bank_stats.items():
        clean_bank_name = bank.replace(" Bank", "").replace(" bank", "").strip()
        report_lines.append(f"{clean_bank_name} Registered {stats['registered']}")
        report_lines.append(f"          Completed -{stats['completed']}")

    return "\n".join(report_lines)

def generate_excel_bytes(cases):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ATM Incident Log"
    ws.views.sheetView[0].showGridLines = True
    
    headers = [
        "Case ID", "Bank", "Branch", "Terminal ID", 
        "Issue Description", "Resolution Status", "Creation Date", "Notes/Comments"
    ]
    ws.append(headers)
    
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style='thin', color='D9D9D9'), right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'), bottom=Side(style='thin', color='D9D9D9')
    )
    
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    
    for case in cases:
        row_data = [
            case.get("case_id", ""),
            case.get("bank", ""),
            case.get("branch", ""),
            case.get("terminal", ""),
            case.get("issue", ""),
            case.get("status", ""),
            case.get("date", "").split(" ")[0],
            case.get("comment", "")
        ]
        ws.append(row_data)
        
    for row in range(2, ws.max_row + 1):
        for col in range(1, len(headers) + 1):
            cell = ws.cell(row=row, column=col)
            cell.font = Font(name="Calibri", size=10)
            cell.border = thin_border
            if col in [1, 4, 6, 7]:
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)
        
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer

# ==========================================
# 8. TELEGRAM COMMAND HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 **Welcome to Tech24 Adama District Bot**\n\n"
        "💻 **Available Commands:**\n"
        "• `/pending` - View all current open/unresolved cases\n"
        "• `/terminate` - Access list of cases to quickly terminate/complete\n"
        "• `/report` - View structured Weekly summary report\n"
        "• `/monthly` - View structured Monthly summary report\n"
        "• `/export` - Generate and download spreadsheet raw database dump"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Processing dashboard query...")
    await send_pending_cases_ui(update.effective_chat.id, context)
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

async def terminate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Fetching cases to terminate...")
    cases, status = await scrape_website_cases()
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ Error: {status}")

    pending = [c for c in cases if c['status'].lower() not in ["completed", "terminated", "resolved"]]
    if not pending:
        return await update.message.reply_text("✨ No actions needed. All logged issues are completed.")

    keyboard = []
    for case in pending[:15]:
        button_label = f"ID: {case['case_id']} | {case['bank']} ({case['branch']})"
        keyboard.append([InlineKeyboardButton(button_label, callback_data=f"term_{case['case_id']}")])

    await update.message.reply_text(
        "Select a case from the list below to **Terminate** (mark as completed):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Generating Weekly Report...")
    cases, status = await scrape_website_cases()
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ Error: {status}")

    report_text = format_summary_report(cases, days_limit=7, is_weekly=True)
    await update.message.reply_text(report_text)

async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Generating Monthly Report...")
    cases, status = await scrape_website_cases()
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ Error: {status}")

    report_text = format_summary_report(cases, days_limit=30, is_weekly=False)
    await update.message.reply_text(report_text)

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Exporting Clean Spreadsheet...")
    cases, status = await scrape_website_cases()
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ Error: {status}")

    excel_file = generate_excel_bytes(cases)
    filename = f"atm-case-report-{datetime.now().strftime('%Y-%b').lower()}.xlsx"
    
    await update.message.reply_document(
        document=excel_file,
        filename=filename,
        caption="📊 **ATM Case Log Export**\n📅 **Reporting Period:** Clean Database Dump"
    )

# ==========================================
# 9. INLINE BUTTON CALLBACK HANDLER
# ==========================================
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "refresh_pending":
        await query.delete_message()
        await send_pending_cases_ui(update.effective_chat.id, context)
        return

    if data.startswith("view_"):
        case_id = data.split("_")[1]
        cases, status = await scrape_website_cases()
        if status != "OK":
            return await query.edit_message_text(f"❌ Error refreshing: {status}")
            
        case = next((c for c in cases if c['case_id'] == case_id), None)
        if not case:
            return await query.edit_message_text("❌ Case details not found.")

        text = (
            f"📋 **Case ID Details: {case['case_id']}**\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🏛 **Bank:** {case['bank']}\n"
            f"📍 **Branch:** {case['branch']}\n"
            f"🖥 **Terminal:** {case['terminal']}\n"
            f"⚠️ **Issue:** {case['issue']}\n"
            f"🔴 **Status:** {case['status']}\n"
            f"👤 **Technician Assigned:** {case['technician']}\n"
            f"📅 **Logged Time:** {case['date']} (EAT)"
        )
        keyboard = [
            [InlineKeyboardButton("🛑 Terminate Case", callback_data=f"term_{case['case_id']}")],
            [InlineKeyboardButton("🔙 Back to List", callback_data="refresh_pending")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("term_"):
        case_id = data.split("_")[1]
        await query.edit_message_text(f"⏳ Processing termination request for Case ID: `{case_id}`...", parse_mode="Markdown")
        
        success, message = await terminate_case_on_dashboard(case_id)
        if success:
            await query.edit_message_text(f"✅ **Case ID {case_id} Successfully Terminated!**", parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ **Failed to terminate Case ID {case_id}.**\nDetail: {message}", parse_mode="Markdown")

# ==========================================
# 10. SYSTEM STARTUP MENU INITIALIZER (CRITICAL FIX)
# ==========================================
async def post_init(application: Application) -> None:
    """
    ይህ ፈንክሽን ቦቱ ልክ እንደበራ በጀርባው የቴሌግራም ሜኑዎችን በአስተማማኝ ሁኔታ 
    ወደ እንግሊዝኛ የሚጭንበት ክፍል ነው።
    """
    logger.info("Setting bot commands menu to English during startup...")
    commands = [
        BotCommand("start", "Initialize your session"),
        BotCommand("pending", "View open/unresolved cases"),
        BotCommand("terminate", "Access list of cases to terminate"),
        BotCommand("report", "View structured Weekly summary report"),
        BotCommand("monthly", "View structured Monthly summary report"),
        BotCommand("export", "Generate and download database spreadsheet")
    ]
    await application.bot.set_my_commands(commands)
    logger.info("English Menu commands loaded successfully!")

# ==========================================
# 11. ENGINE INITIATION
# ==========================================
def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    # post_init ፈንክሽን እዚህ ላይ ተጭኗል
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    job_queue = application.job_queue

    # በየ 10 ደቂቃው ዳሽቦርዱን ቼክ እያደረገ የሚልክበት (Auto Polling)
    job_queue.run_repeating(auto_monitor_dashboard, interval=600, first=10)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("terminate", terminate_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("monthly", monthly_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CallbackQueryHandler(button_click_handler))

    application.run_polling()

if __name__ == '__main__':
    main()
