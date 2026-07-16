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

# ቀደም ሲል የተላኩ ኬዞችን መመዝገቢያ (በየ 10 ደቂቃው ደጋግሞ ኖቲፊኬሽን እንዳይልክ ለመከላከል)
SENT_CASES_TRACKER = set()

# ==========================================
# 2. FLASK SERVER FOR UPTIME
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
# 3. JSON PARSING HELPERS (CLEANER ENGINE)
# ==========================================
def safe_parse_json(val):
    if not val:
        return {}
    if isinstance(val, dict):
        return val
    try:
        if isinstance(val, str):
            # Convert single quotes to double quotes for valid JSON
            cleaned = val.replace("'", '"').replace("None", "null").replace("True", "true").replace("False", "false")
            return json.loads(cleaned)
    except Exception:
        pass
    return {}

def extract_field(item, keyword):
    """
    JSON ጽሑፎችን ፈልቅቆ ንጹህ ስም ብቻ ያወጣል (ለምሳሌ፦ Awash, Oda Boqotu, ATM1)
    """
    parsed = safe_parse_json(item)
    if not parsed:
        if isinstance(item, str):
            return item
        return ""
    
    # Check directly for common key variations
    for k, v in parsed.items():
        if k.lower() in [keyword.lower(), f"{keyword.lower()}name", f"{keyword.lower()}_name"]:
            if isinstance(v, dict):
                return v.get('name', v.get('title', str(v)))
            return str(v)
            
    # Fallback search if not found directly
    for k, v in parsed.items():
        if keyword.lower() in k.lower():
            if isinstance(v, dict):
                return v.get('name', v.get('title', str(v)))
            return str(v)
    return ""

# ==========================================
# 4. API CONNECTIONS
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
                # ንጹህ ስሞችን ብቻ የመፍለቂያ ክፍል (ያለ JSON ዝርክርክ)
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
# 5. AUTOMATIC 10-MINUTE MONITOR
# ==========================================
async def auto_monitor_dashboard(context: ContextTypes.DEFAULT_TYPE):
    if not NOTIFICATION_CHAT_ID:
        return
        
    logger.info("Auto-monitoring check started...")
    cases, status = await scrape_website_cases()
    if status != "OK":
        return

    pending = [c for c in cases if c['status'].lower() not in ["completed", "terminated", "resolved"]]
    
    for case in pending:
        if case['case_id'] not in SENT_CASES_TRACKER:
            text = (
                f"🚨 **New Pending ATM Case Registered!**\n\n"
                f"🆔 **Case ID:** `{case['case_id']}`\n"
                f"🏛 **Bank:** {case['bank']}\n"
                f"📍 **Branch:** {case['branch']}\n"
                f"🖥 **Terminal ID:** {case['terminal']}\n"
                f"⚠️ **Issue:** {case['issue']}\n"
                f"👤 **Technician Assigned:** {case['technician']}\n"
                f"📅 **Date:** {case['date']}"
            )
            keyboard = [[InlineKeyboardButton("🛑 Terminate", callback_data=f"term_{case['case_id']}")]]
            
            try:
                await context.bot.send_message(
                    chat_id=NOTIFICATION_CHAT_ID,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                SENT_CASES_TRACKER.add(case['case_id'])
            except Exception as e:
                logger.error(f"Failed sending alert: {e}")

# ==========================================
# 6. DYNAMIC UI FORMATTER
# ==========================================
async def send_pending_cases_ui(chat_id, context: ContextTypes.DEFAULT_TYPE):
    cases, status = await scrape_website_cases()
    if status != "OK":
        return await context.bot.send_message(chat_id=chat_id, text=f"❌ Error: {status}")

    pending = [c for c in cases if c['status'].lower() not in ["completed", "terminated", "resolved"]]
    
    if not pending:
        return await context.bot.send_message(chat_id=chat_id, text="✨ No actions needed. All logged issues are completed.")

    # ሁኔታ 1፦ አንድ ኬዝ ብቻ ሲኖር (ንጹህ ፎርማት - እንደ ፎቶ 2)
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

    # ሁኔታ 2፦ ሁለት እና ከዚያ በላይ ኬዝ ሲኖር (ልክ እንደ ፎቶ 3 ዝርዝር ብቻ)
    else:
        text = "The following ATM cases have been reported and are currently pending action. Select a case from the list below to view details and proceed with resolution."
        keyboard = []
        for case in pending[:15]:
            button_label = f"{case['case_id']} | {case['bank']} | {case['branch']}"
            keyboard.append([InlineKeyboardButton(button_label, callback_data=f"view_{case['case_id']}")])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 7. EXCEL & REPORT ENGINE
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

    report_lines.append("
