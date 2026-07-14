import io
import logging
import httpx
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from datetime import datetime
import re
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

BOT_TOKEN = os.getenv('BOT_TOKEN')
EMAIL = os.getenv('EMAIL')
PASSWORD = os.getenv('PASSWORD')

sent_cases = set()

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- የዌብሳይት መረጃዎችን በአይነትና በራስጌ (Header) ለይቶ የሚያመጣ ብልጥ ተግባር ---
async def scrape_website_cases():
    if not EMAIL or not PASSWORD:
        return [], "Error: EMAIL or PASSWORD environment variables are not set on Render!"
        
    login_data = {'email': EMAIL, 'password': PASSWORD, 'submit': 'Login'}
    
    async with httpx.AsyncClient(follow_redirects=True) as session:
        try:
            # 1. ትክክለኛውን URL በራሱ ጊዜ ማወቅ
            test_url = 'https://tech24et.com/cases'
            try:
                response = await session.get(test_url)
                if response.status_code == 404:
                    target_url = 'https://tech24et.com/client/cases.php'
                    login_url = 'https://tech24et.com/client/index.php'
                else:
                    target_url = test_url
                    login_url = str(response.url)
                    if "cases" in login_url:
                        login_url = 'https://tech24et.com/index.php'
            except Exception:
                target_url = 'https://tech24et.com/client/cases.php'
                login_url = 'https://tech24et.com/client/index.php'
                
            logging.info(f"Using Login URL: {login_url}")
            logging.info(f"Using Cases URL: {target_url}")
            
            # 2. Login ማድረግ
            login_response = await session.post(login_url, data=login_data)
            logging.info(f"Login POST completed with status: {login_response.status_code}")
            
            # 3. የኬዞችን ገጽ መውሰድ
            cases_response = await session.get(target_url)
            if cases_response.status_code != 200:
                return [], f"Error: Received HTTP {cases_response.status_code} from {target_url}"
                
            soup = BeautifulSoup(cases_response.text, 'html.parser')
            table = soup.find('table')
            
            if not table:
                # Login መሳካቱን መፈተሽ
                if soup.find('form') or "login" in cases_response.text.lower():
                    return [], "Error: Login failed! (The website redirected us back to a login form. Please double-check your EMAIL and PASSWORD in Render's Environment Variables)"
                return [], "Error: Website table not found! (Are you sure this is the correct cases page?)"
                
            # 4. ዓምዶችን (Columns) በስማቸው በራስ-ሰር መለየት
            headers = []
            thead = table.find('thead')
            if thead:
                headers = [th.text.strip().lower() for th in thead.find_all('th')]
            else:
                first_row = table.find('tr')
                if first_row:
                    headers = [th.text.strip().lower() for th in first_row.find_all(['th', 'td'])]
            
            idx_case_id = 1
            idx_bank = 2
            idx_district = 3
            idx_branch = 4
            idx_issue = 5
            idx_status = -1
            
            for i, h in enumerate(headers):
                if "case id" in h:
                    idx_case_id = i
                elif "bank" in h:
                    idx_bank = i
                elif "district" in h:
                    idx_district = i
                elif "branch" in h:
                    idx_branch = i
                elif "case type" in h or "issue" in h:
                    idx_issue = i
                elif "status" in h:
                    idx_status = i

            # 5. መረጃዎችን መለየት (Tbody ካለ እሱን መጠቀም)
            tbody = table.find('tbody')
            if tbody:
                rows = tbody.find_all('tr')
            else:
                rows = table.find_all('tr')[1:]
                
            scraped_cases = []
            for row in rows:
                cols = row.find_all('td')
                if len(cols) > max(idx_case_id, idx_bank, idx_district, idx_branch, idx_issue):
                    case_id = cols[idx_case_id].text.strip()
                    bank = cols[idx_bank].text.strip()
                    district = cols[idx_district].text.strip()
                    branch = cols[idx_branch].text.strip()
                    issue = cols[idx_issue].text.strip()
                    
                    status = "Pending"
                    if idx_status != -1 and idx_status < len(cols):
                        status = cols[idx_status].text.strip()
                    else:
                        if "completed" in row.text.lower():
                            status = "Completed"
                    
                    time_val = "1h"
                    date_str = datetime.now().strftime("%d/%m/%Y")
                    row_text = row.text.strip()
                    match = re.search(r'\d{2}/\d{2}/\d{4}', row_text)
                    if match:
                        date_str = match.group(0)
                            
                    scraped_cases.append({
                        'case_id': case_id,
                        'bank': bank,
                        'district': district,
                        'branch': branch,
                        'issue': issue,
                        'time_val': time_val,
                        'status': status,
                        'date': date_str
                    })
                    
            if not scraped_cases:
                return [], "Success: Connected, but 0 total cases found in the table."
                
            return scraped_cases, "OK"
            
        except Exception as e:
            logging.error(f"Scraping error: {e}")
            return [], f"Error during scraping: {str(e)}"

# --- 1. /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    
    context.job_queue.run_repeating(check_website_job, interval=900, first=5, chat_id=chat_id, name=str(chat_id))
    await update.message.reply_text("✅ የAdama District ክትትል ተጀምሯል።")

async def check_website_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    cases, status = await scrape_website_cases()
    
    if status == "OK":
        for c in cases:
            if "adama" in c['district'].lower() and c['case_id'] not in sent_cases:
                message = (
                    f"🔔 **ATM Incident Notification**\n\n"
                    f"📄 **ID:** {c['case_id']}\n"
                    f"🏦 **Bank:** {c['bank']}\n"
                    f"⚠️ **Issue:** {c['issue']}\n"
                    f"🏢 **Branch:** {c['branch']}\n"
                    f"📍 **District:** {c['district']}\n"
                )
                await context.bot.send_message(chat_id=chat_id, text=message)
                sent_cases.add(c['case_id'])

# --- 2. /pending (finding - የፔንዲንግ ኬዞች ዝርዝር) ---
async def pending_cases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ የፔንዲንግ መረጃዎችን ከዌብሳይቱ ላይ በመፈለግ ላይ...")
    cases, status = await scrape_website_cases()
    
    if status != "OK":
        await update.message.reply_text(f"❌ ስህተት አጋጥሟል፦\n`{status}`", parse_mode="Markdown")
        return
        
    adama_pending = [c for c in cases if "adama" in c['district'].lower() and c['status'].lower() != "completed"]
    
    if not adama_pending:
        await update.message.reply_text("📭 ለAdama District ምንም ፔንዲንግ (Pending) ኬዝ አልተገኘም።")
        return
        
    keyboard = []
    for c in adama_pending:
        btn_text = f"{c['case_id']} | {c['bank']} | {c['branch']} | {c['time_val']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"view_{c['case_id']}")])
        
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_action")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "The following ATM cases have been reported and are currently pending action. "
        "select a case from the list below to view details and proceed with resolution.",
        reply_markup=reply_markup
    )

# --- 3. /terminate (Completed case) ---
async def terminate_cases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ ለመዝጋት የሚሆኑ ኬዞችን በመፈለግ ላይ...")
    cases, status = await scrape_website_cases()
    
    if status != "OK":
        await update.message.reply_text(f"❌ ስህተት አጋጥሟል፦\n`{status}`", parse_mode="Markdown")
        return
        
    adama_pending = [c for c in cases if "adama" in c['district'].lower() and c['status'].lower() != "completed"]
    
    if not adama_pending:
        await update.message.reply_text("📭 ለመዝጋት (Terminate ለማድረግ) የሚሆን ፔንዲንግ ኬዝ አልተገኘም።")
        return
        
    keyboard = []
    for c in adama_pending:
        btn_text = f"❌ Terminate: {c['case_id']} | {c['bank']} | {c['branch']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"term_{c['case_id']}")])
        
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_action")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "⚠️ ለመዝጋት (Terminate ለማድረግ) የፈለጉትን ኬዝ ይምረጡ፦",
        reply_markup=reply_markup
    )

# --- 4. /report (Weekly Activity - ሳምንታዊ ሪፖርት) ---
async def weekly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ የሳምንት ሪፖርት እየተዘጋጀ ነው...")
    cases, status = await scrape_website_cases()
    
    if status != "OK":
        await update.message.reply_text(f"❌ ስህተት አጋጥሟል፦\n`{status}`", parse_mode="Markdown")
        return
        
    adama_cases = [c for c in cases if "adama" in c['district'].lower()]
    
    if not adama_cases:
        await update.message.reply_text("📍 ለAdama District የተመዘገበ ምንም ኬዝ አልተገኘም።")
        return
        
    report_lines = [".       Weekly report/yared Girma/\n"]
    
    bank_counts = {}
    for c in adama_cases:
        line = f"® {c['date']} Registered {c['branch']} Branch {c['bank']}({c['issue']} )"
        report_lines.append(line)
        report_lines.append("")
        
        bank_counts[c['bank']] = bank_counts.get(c['bank'], 0) + 1
        
    report_lines.append("\n         Generally\n")
    for bank_name, count in bank_counts.items():
        report_lines.append(f"{bank_name} Registered")
        report_lines.append(f"   resolved - {count}")
        
    report_lines.append(".")
    
    await update.message.reply_text("\n".join(report_lines))

# --- 5. /monthly (monthly Report - Excel ሪፖርት) ---
async def monthly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 የ 1 ወር የ Excel ሪፖርት እየተዘጋጀ ነው...")
    cases, status = await scrape_website_cases()
    
    if status != "OK":
        await update.message.reply_text(f"❌ ስህተት አጋጥሟል፦\n`{status}`", parse_mode="Markdown")
        return
        
    adama_cases = [c for c in cases if "adama" in c['district'].lower()]
    
    if not adama_cases:
        await update.message.reply_text("📍 ለAdama District ምንም ኬዝ አልተገኘም።")
        return
        
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Monthly Cases Report"
    
    headers = ["Date", "Case ID", "Bank", "Branch", "Issue", "District", "Status"]
    ws.append(headers)
    
    header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")
    
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        
    for c in adama_cases:
        ws.append([
            c['date'],
            c['case_id'],
            c['bank'],
            c['branch'],
            c['issue'],
            c['district'],
            c['status']
        ])
        
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)
        
    excel_file = io.BytesIO()
    wb.save(excel_file)
    excel_file.seek(0)
    
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=excel_file,
        filename="Monthly_Report_Adama_District.xlsx",
        caption="📊 የAdama District ወርሃዊ የኤክሴል ሪፖርት ተዘጋጅቷል።"
    )

# --- Button Callback Handler ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "cancel_action":
        await query.edit_message_text("❌ ክዋኔው ተሰርዟል።")
        return
        
    elif data.startswith("view_"):
        case_id = data.split("_")[1]
        cases, status = await scrape_website_cases()
        
        if status != "OK":
            await query.edit_message_text(f"❌ ስህተት፦ `{status}`", parse_mode="Markdown")
            return
            
        case = next((c for c in cases if c['case_id'] == case_id), None)
        
        if not case:
            await query.edit_message_text("❌ የኬዙ መረጃ አልተገኘም።")
            return
            
        message = (
            f"🔔 **ATM Incident Details**\n\n"
            f"📄 **ID:** {case['case_id']}\n"
            f"🏦 **Bank:** {case['bank']}\n"
            f"⚠️ **Issue:** {case['issue']}\n"
            f"🏢 **Branch:** {case['branch']}\n"
            f"📍 **District:** {case['district']}\n"
            f"⏰ **Time:** {case['time_val']}\n"
            f"📊 **Status:** {case['status']}\n"
        )
        
        keyboard = [
            [InlineKeyboardButton("✅ Complete/Terminate Case", callback_data=f"term_{case_id}")],
            [InlineKeyboardButton("⬅️ Back to List", callback_data="back_to_pending")]
        ]
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        
    elif data == "back_to_pending":
        cases, status = await scrape_website_cases()
        
        if status != "OK":
            await query.edit_message_text(f"❌ ስህተት፦ `{status}`", parse_mode="Markdown")
            return
            
        adama_pending = [c for c in cases if "adama" in c['district'].lower() and c['status'].lower() != "completed"]
        
        if not adama_pending:
            await query.edit_message_text("📭 ለAdama District ምንም ፔንዲንግ ኬዝ አልተገኘም።")
            return
            
        keyboard = []
        for c in adama_pending:
            btn_text = f"{c['case_id']} | {c['bank']} | {c['branch']} | {c['time_val']}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"view_{c['case_id']}")])
            
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_action")])
        await query.edit_message_text(
            "The following ATM cases have been reported and are currently pending action. "
            "select a case from the list below to view details and proceed with resolution.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    elif data.startswith("term_"):
        case_id = data.split("_")[1]
        await query.edit_message_text(f"⏳ ኬዝ ID {case_id}-ን ከዌብሳይቱ ላይ ለመዝጋት እየሞከርኩ ነው...")
        
        login_data = {'email': EMAIL, 'password': PASSWORD, 'submit': 'Login'}
        async with httpx.AsyncClient(follow_redirects=True) as session:
            try:
                # Login ለማወቅ መጀመሪያ መሞከር
                test_url = 'https://tech24et.com/cases'
                try:
                    response = await session.get(test_url)
                    if response.status_code == 404:
                        login_url = 'https://tech24et.com/client/index.php'
                    else:
                        login_url = str(response.url)
                        if "cases" in login_url:
                            login_url = 'https://tech24et.com/index.php'
                except Exception:
                    login_url = 'https://tech24et.com/client/index.php'

                await session.post(login_url, data=login_data)
                
                # Update ማድረግ
                terminate_url = 'https://tech24et.com/client/cases.php'
                if "client" not in login_url:
                    terminate_url = 'https://tech24et.com/cases'
                    
                payload = {
                    'case_id': case_id,
                    'action': 'complete',
                    'status': 'Completed',
                    'submit': 'Update'
                }
                response = await session.post(terminate_url, data=payload)
                
                if response.status_code == 200:
                    await query.edit_message_text(f"✅ Case **{case_id}** on the website has been successfully marked as Completed/Terminated! 🎉", parse_mode="Markdown")
                else:
                    await query.edit_message_text(f"⚠️ Case {case_id} update sent, but server returned status: {response.status_code}")
            except Exception as e:
                logging.error(f"Error terminating case {case_id}: {e}")
                await query.edit_message_text(f"❌ Error: ኬዙን በዌብሳይቱ ላይ መዝጋት አልተቻለም።")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Start Bot"),
        BotCommand("pending", "finding"),
        BotCommand("terminate", "Completed case"),
        BotCommand("report", "Weekly Activity"),
        BotCommand("monthly", "monthly Report")
    ])

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is missing!")
        return
        
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pending", pending_cases))
    application.add_handler(CommandHandler("terminate", terminate_cases))
    application.add_handler(CommandHandler("report", weekly_report))
    application.add_handler(CommandHandler("monthly", monthly_report))
    application.add_handler(CallbackQueryHandler(callback_handler))
    
    print("Bot is running with custom menus...")
    application.run_polling()

if __name__ == '__main__':
    main()
