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
# 3. HELPER FUNCTIONS TO CLEAN JSON STRINGS
# ==========================================
def safe_parse_json(val):
    """Safely parse a string representation of a dict/json."""
    if not val:
        return {}
    if isinstance(val, dict):
        return val
    try:
        # If it's a string representation of a dict (common in raw database exports)
        # Replace single quotes with double quotes for valid JSON
        if isinstance(val, str):
            cleaned = val.replace("'", '"').replace("None", "null").replace("True", "true").replace("False", "false")
            return json.loads(cleaned)
    except Exception:
        pass
    return {}

def extract_field(item, keyword):
    """Extracts nested value based on keywords (e.g. bank, branch, terminal)."""
    parsed = safe_parse_json(item)
    if not parsed:
        if isinstance(item, str):
            return item
        return ""
    
    # Try direct key matches
    for k, v in parsed.items():
        if k.lower() == keyword.lower():
            if isinstance(v, dict):
                return v.get('name', v.get('title', str(v)))
            return str(v)
            
    # Try partial matches
    for k, v in parsed.items():
        if keyword.lower() in k.lower():
            if isinstance(v, dict):
                return v.get('name', v.get('title', str(v)))
            return str(v)
    return ""

# ==========================================
# 4. SCRAPER & DASHBOARD API CALLS
# ==========================================
async def scrape_website_cases():
    """
    Fetches raw case data from the server API.
    Re-authenticates or maintains sessions dynamically.
    """
    if not EMAIL or not PASSWORD:
        return [], "Error: EMAIL or PASSWORD environment variables missing!"

    login_url = "https://api.tech24et.com/api/auth/login"  # Replace with actual login endpoint if needed
    data_url = "https://api.tech24et.com/api/callentries"   # Replace with actual entries endpoint

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # Step 1: Authenticate
            payload = {"email": EMAIL, "password": PASSWORD}
            login_res = await client.post(login_url, json=payload)
            if login_res.status_code != 200:
                return [], f"Login failed with status {login_res.status_code}"
            
            # Step 2: Fetch Data (Cookies/Tokens are automatically managed by AsyncClient if using a session, 
            # otherwise handle JWT Token if returned in JSON)
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
                return [], f"Failed to fetch data (Status {response.status_code})"

            raw_cases = response.json()
            
            # Normalize cases to a standard dictionary structure
            cleaned_cases = []
            for item in raw_cases:
                # Raw extraction and cleaning of JSON fields
                bank_name = extract_field(item.get("bank", ""), "bank") or extract_field(item.get("bank_id", ""), "name")
                branch_name = extract_field(item.get("branch", ""), "branch") or extract_field(item.get("branch_id", ""), "name")
                terminal_id = extract_field(item.get("terminal", ""), "terminal") or extract_field(item.get("terminal_id", ""), "name") or item.get("terminal_id", "")
                
                # Handling reported date
                created_at_str = item.get("created_at") or item.get("Creation Time") or ""
                parsed_date = None
                if created_at_str:
                    try:
                        # Common ISO or standard formats
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
                    "issue": item.get("issue_description") or item.get("Issue Descrp") or "No issue description",
                    "status": item.get("status") or item.get("Resolution S") or "Pending",
                    "comment": item.get("comment") or item.get("Notes/Comments") or "None",
                    "date_obj": parsed_date,
                    "date": parsed_date.strftime("%d/%m/%Y")
                }
                cleaned_cases.append(case_obj)
                
            return cleaned_cases, "OK"
        except Exception as e:
            logger.error(f"Error in scraping: {e}")
            return [], str(e)

async def terminate_case_on_dashboard(case_id):
    """Triggers the case closing endpoint on the API."""
    terminate_url = f'https://api.tech24et.com/api/callentries/{case_id}/close'
    
    # Re-login to get fresh token/session
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
            return False, f"Failed with server status: {response.status_code}"
        except Exception as e:
            return False, str(e)

# ==========================================
# 5. EXCEL EXPORT ENGINE (REFINED)
# ==========================================
def generate_excel_bytes(cases):
    """Generates a perfectly styled, un-cluttered Excel file."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ATM Incident Log"
    
    # Grid lines visible
    ws.views.sheetView[0].showGridLines = True
    
    # Headers
    headers = [
        "Case ID", "Bank", "Branch", "Terminal ID", 
        "Issue Description", "Resolution Status", "Creation Date", "Notes/Comments"
    ]
    ws.append(headers)
    
    # Styles
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid") # Dark Blue
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style='thin', color='D9D9D9'),
        right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'),
        bottom=Side(style='thin', color='D9D9D9')
    )
    
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    
    # Writing rows
    for case in cases:
        row_data = [
            case.get("case_id", ""),
            case.get("bank", ""),
            case.get("branch", ""),
            case.get("terminal", ""),
            case.get("issue", ""),
            case.get("status", ""),
            case.get("date", ""),
            case.get("comment", "")
        ]
        ws.append(row_data)
        
    # Styling Data Rows & Auto-adjust Column Widths
    for row in range(2, ws.max_row + 1):
        for col in range(1, len(headers) + 1):
            cell = ws.cell(row=row, column=col)
            cell.font = Font(name="Calibri", size=10)
            cell.border = thin_border
            if col in [1, 4, 6, 7]: # Align IDs, statuses and dates to center
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
# 6. DYNAMIC REPORT ENGINES
# ==========================================
def format_summary_report(cases, days_limit=7, is_weekly=True):
    """Formats raw database logs into beautiful requested format."""
    now = datetime.now()
    cutoff_date = now - timedelta(days=days_limit)
    
    # Filter cases in range
    filtered_cases = [c for c in cases if c['date_obj'] >= cutoff_date]
    
    title_label = "Weekly" if is_weekly else "Monthly"
    if not filtered_cases:
        return f"🏧 **{title_label} Report**\n\nNo records found for this period."

    # Sort cases chronologically
    filtered_cases.sort(key=lambda x: x['date_obj'])
    
    report_lines = []
    report_lines.append(f"🏧 **{title_label} report**")
    
    bank_stats = {}
    
    for case in filtered_cases:
        date_str = case['date']
        branch = case['branch']
        bank = case['bank']
        issue = case['issue']
        status = case['status']
        
        # Build individual line
        line = f"®️ `{date_str}` Registered | **{branch}** | **{bank}** | ({issue}) | *{status}*"
        report_lines.append(line)
        
        # Group stats
        if bank not in bank_stats:
            bank_stats[bank] = {"registered": 0, "completed": 0}
        
        bank_stats[bank]["registered"] += 1
        if status.lower() in ["completed", "terminated", "resolved"]:
            bank_stats[bank]["completed"] += 1

    report_lines.append("\nGenerally:")
    for bank, stats in bank_stats.items():
        report_lines.append(f"🏛 **{bank}**:\n   Registered: {stats['registered']} | Completed: {stats['completed']}")
        
    return "\n".join(report_lines)

# ==========================================
# 7. TELEGRAM BOT HANDLERS (ENGLISH COMMANDS)
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Dynamically force/set English menu layout to Telegram server
    commands = [
        BotCommand("start", "Initialize your session"),
        BotCommand("pending", "View open/unresolved cases"),
        BotCommand("terminate", "Access list of cases to terminate"),
        BotCommand("report", "View structured Weekly summary report"),
        BotCommand("monthly", "View structured Monthly summary report"),
        BotCommand("export", "Generate and download database spreadsheet")
    ]
    await context.bot.set_my_commands(commands)
    
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
    processing = await update.message.reply_text("⏳ Fetching pending cases...")
    cases, status = await scrape_website_cases()
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)
    
    if status != "OK":
        return await update.message.reply_text(f"❌ Error connecting to database: {status}")
        
    pending = [c for c in cases if c['status'].lower() not in ["completed", "terminated", "resolved"]]
    
    if not pending:
        return await update.message.reply_text("✨ No actions needed. All logged issues are completed.")
        
    for case in pending[:10]:  # Limit output to prevent flooding
        text = (
            f"**Case ID:** `{case['case_id']}`\n"
            f"**Bank:** {case['bank']}\n"
            f"**Branch:** {case['branch']}\n"
            f"**Terminal:** {case['terminal']}\n"
            f"**Issue:** {case['issue']}\n"
            f"**Status:** {case['status']}\n"
            f"**Date:** {case['date']}"
        )
        keyboard = [[InlineKeyboardButton("🛑 Terminate", callback_data=f"term_{case['case_id']}")]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def terminate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Fetching active cases to terminate...")
    cases, status = await scrape_website_cases()
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ Error connecting to database: {status}")

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
        return await update.message.reply_text(f"❌ Error generating report: {status}")

    report_text = format_summary_report(cases, days_limit=7, is_weekly=True)
    await update.message.reply_text(report_text, parse_mode="Markdown")

async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Generating Monthly Report...")
    cases, status = await scrape_website_cases()
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ Error generating report: {status}")

    report_text = format_summary_report(cases, days_limit=30, is_weekly=False)
    await update.message.reply_text(report_text, parse_mode="Markdown")

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    processing = await update.message.reply_text("⏳ Generating Cleaned Excel Spreadsheet...")
    cases, status = await scrape_website_cases()
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=processing.message_id)

    if status != "OK":
        return await update.message.reply_text(f"❌ Error exporting: {status}")

    excel_file = generate_excel_bytes(cases)
    filename = f"atm-case-report-{datetime.now().strftime('%Y-%b').lower()}.xlsx"
    
    await update.message.reply_document(
        document=excel_file,
        filename=filename,
        caption="📊 **ATM Case Log Export**\n📅 **Reporting Period:** Current Active Data"
    )

# ==========================================
# 8. CALLBACK ACTIONS
# ==========================================
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("term_"):
        case_id = data.split("_")[1]
        await query.edit_message_text(f"⏳ Processing termination request for Case ID: `{case_id}`...", parse_mode="Markdown")
        
        success, message = await terminate_case_on_dashboard(case_id)
        if success:
            await query.edit_message_text(f"✅ **Case ID {case_id} Successfully Terminated!**", parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ **Failed to terminate Case ID {case_id}.**\nDetail: {message}", parse_mode="Markdown")

# ==========================================
# 9. MAIN APP INITIALIZATION
# ==========================================
def main():
    # Start Flask status check server in background thread (For Render / Uptime Keep-alive)
    threading.Thread(target=run_health_server, daemon=True).start()

    # Build and initialize telegram bot application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("terminate", terminate_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("monthly", monthly_command))
    application.add_handler(CommandHandler("export", export_command))
    
    # Callback Handlers (For interactive button clicks)
    application.add_handler(CallbackQueryHandler(button_click_handler))

    # Run bot polling
    logger.info("Starting Telegram Bot...")
    application.run_polling()

if __name__ == '__main__':
    main()
