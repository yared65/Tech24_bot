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

# --- አስተማማኝ Scraper ለአዲሱ የሰንጠረዥ መዋቅር ---
async def scrape_website_cases():
    if not EMAIL or not PASSWORD:
        return [], "Error: EMAIL or PASSWORD environment variables are not set on Render!"
        
    login_url = 'https://tech24et.com/client/index.php'
    target_url = 'https://tech24et.com/client/cases.php'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    
    login_data = {
        'email': EMAIL,
        'password': PASSWORD,
        'submit': 'Login'
    }
    
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20.0) as session:
        try:
            # 1. መጀመሪያ ሎግኢን ገጹን መክፈት
            await session.get(login_url)
            
            # 2. ሎግኢን ማድረግ (POST)
            login_response = await session.post(login_url, data=login_data)
            logging.info(f"Login POST status: {login_response.status_code}")
            
            # 3. የኬዞችን ገጽ መውሰድ
            cases_response = await session.get(target_url)
            if cases_response.status_code != 200:
                return [], f"Error: Received HTTP {cases_response.status_code} from {target_url}"
                
            soup = BeautifulSoup(cases_response.text, 'html.parser')
            
            # ሰንጠረዡን በ ID መፈለግ (በፎቶ 2907.jpg መሰረት)
            table = soup.find('table', {'id': 'example1'}) or soup.find('table')
            
            if not table:
                if "login" in cases_response.text.lower() or soup.find('form'):
                    return [], "Error: Login session failed! Please check if your EMAIL and PASSWORD are correct."
                return [], "Error: Table 'example1' not found on the page!"
                
            # ረድፎችን ማውጣት
            tbody = table.find('tbody')
            rows = tbody.find_all('tr') if tbody else table.find_all('tr')[1:]
                
            scraped_cases = []
            for row in rows:
                cols = row.find_all('td')
                # ቢያንስ 8 ዓምዶች መኖራቸውን ማረጋገጥ (በፎቶው መሰረት)
                if len(cols) >= 8:
                    case_id = cols[1].text.strip()
                    bank = cols[2].text.strip()
                    district = cols[3].text.strip()
                    branch = cols[4].text.strip()
                    issue = cols[5].text.strip()
                    date_str = cols[6].text.strip()
                    
                    # ስታተስ ለመወሰን፡ በ Action (8ኛው ዓምድ) ውስጥ "Complete" የሚል ሊንክ ካለ Pending ነው፣ ካልሆነ Completed ነው።
                    action_html = str(cols[7])
                    if "Complete" in cols[7].text or "cases.php?id=" in action_html:
                        status = "Pending"
                    else:
                        status = "Completed"
                            
                    scraped_cases.append({
                        'case_id': case_id,
                        'bank': bank,
                        'district': district,
                        'branch': branch,
                        'issue': issue,
                        'time_val': "1h",
                        'status': status,
                        'date': date_str
                    })
                    
            if not scraped_cases:
                return [], "Success: Connected, but found 0 cases in the table."
                
            return scraped_cases, "OK"
            
        except Exception as e:
            logging.error(f"Scraping error: {e}")
            return [], f"Error during scraping: {str(e)}"

# --- 1. /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    
    # በየ15 ደቂቃው (900 ሰከንድ) አዳዲስ ኬዞችን እንዲፈትሽ ማድረግ
    context.job_queue.run_repeating(check_website_job, interval=900, first=5, chat_id=chat_id, name=str(chat_id))
    await update.message.reply_text("✅ የAdama District የኬዞች ክትትል በስኬት ተጀምሯል።")

async def check_website_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    cases, status = await scrape_website_cases()
    
    if status == "OK":
        for c in cases:
            # ለAdama ዲስትሪክት እና ገና ላልተላኩ አዳዲስ ኬዞች ብቻ ማሳወቂያ መላክ
            if "adama" in c['district'].lower() and c['case_id'] not in sent_cases and c['status'] == "Pending":
                message = (
                    f"🔔 **ATM Incident Notification**\n\n"
                    f"📄 **ID:** {c['case_id']}\n"
                    f"🏦 **Bank:** {c['bank']}\n"
                    f"⚠️ **Issue:** {c['issue']}\n"
                    f"🏢 **Branch:** {c['branch']}\n"
                    f"📍 **District:** {c['district']}\n"
                    f"📅 **Date:** {c['date']}\n"
                )
                await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")
                sent_cases.add(c['case_id'])

# --- 2. /pending ---
async def pending_cases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ የፔንዲንግ መረጃዎችን ከዌብሳይቱ ላይ በመፈለግ ላይ...")
    cases, status = await scrape_website_cases()
    
    if status != "OK":
        await update.message.reply_text(f"❌ ስህተት አጋጥሟል፦\n`{status}`", parse_mode="Markdown")
        return
        
    adama_pending = [c for c in cases if "adama" in c['district'].lower() and c['status'] == "Pending"]
    
    if not adama_pending:
        await update.message.reply_text("📭 ለAdama District ምንም ፔንዲንግ (Pending) ኬዝ አልተገኘም።")
        return
        
    keyboard = []
    for c in adama_pending:
        btn_text = f"{c['case_id']} | {c['bank']} | {c['branch']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"view_{c['case_id']}")])
        
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_action")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "የሚከተሉት የኤቲኤም ኬዞች ክፍት ናቸው። ዝርዝር መረጃ ለማየት እና ለመዝጋት አንዱን ይምረጡ፦",
        reply_markup=reply_markup
    )

# --- 3. /terminate ---
async def terminate_cases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ ለመዝጋት የሚሆኑ ኬዞችን በመፈለግ ላይ...")
    cases, status = await scrape_website_cases()
    
    if status != "OK":
        await update.message.reply_text(f"❌ ስህተት አጋጥሟል፦\n`{status}`", parse_mode="Markdown")
        return
        
    adama_pending = [c for c in cases if "adama" in c['district'].lower() and c['status'] == "Pending"]
    
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

# --- 4. /report ---
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

# --- 5. /monthly (Excel ሪፖርት) ---
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
            f"📅 **Date:** {case['date']}\n"
            f"📊 **Status:** {case['status']}\n"
        )
        
        keyboard = [
            [InlineKeyboardButton("✅ Complete Case (ዝጋ)", callback_data=f"term_{case_id}")],
            [InlineKeyboardButton("⬅️ Back to List", callback_data="back_to_pending")]
        ]
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        
    elif data == "back_to_pending":
        cases, status = await scrape_website_cases()
        
        if status != "OK":
            await query.edit_message_text(f"❌ ስህተት፦ `{status}`", parse_mode="Markdown")
            return
            
        adama_pending = [c for c in cases if "adama" in c['district'].lower() and c['status'] == "Pending"]
        
        if not adama_pending:
            await query.edit_message_text("📭 ለAdama District ምንም ፔንዲንግ ኬዝ አልተገኘም።")
            return
            
        keyboard = []
        for c in adama_pending:
            btn_text = f"{c['case_id']} | {c['bank']} | {c['branch']}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"view_{c['case_id']}")])
            
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_action")])
        await query.edit_message_text(
            "የሚከተሉት የኤቲኤም ኬዞች ክፍት ናቸው። ዝርዝር ለማየት አንዱን ይምረጡ፦",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    elif data.startswith("term_"):
        case_id = data.split("_")[1]
        await query.edit_message_text(f"⏳ ኬዝ ID {case_id}-ን ከዌብሳይቱ ላይ ለመዝጋት እየሞከርኩ ነው...")
        
        login_url = 'https://tech24et.com/client/index.php'
        terminate_url = 'https://tech24et.com/client/cases.php'
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        login_data = {'email': EMAIL, 'password': PASSWORD, 'submit': 'Login'}
        
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as session:
            try:
                # 1. ሎግኢን ማድረግ
                await session.get(login_url)
                await session.post(login_url, data=login_data)
                
                # 2. በፎቶ 2910.jpg ላይ ያለውን ትክክለኛ የፎርም ዳታ (POST) መላክ
                payload = {
                    'id': case_id,             # የኬዝ መለያ (id)
                    'status': 'Completed',     # ሁኔታው
                    'remark': 'Resolved via Telegram Bot',
                    'update': 'Update'         # ሰብሚት ቁልፍ ስም (update)
                }
                response = await session.post(terminate_url, data=payload)
                
                if response.status_code == 200:
                    await query.edit_message_text(f"✅ Case **{case_id}** በተሳካ ሁኔታ በዌብሳይቱ ላይ ተዘግቷል! 🎉", parse_mode="Markdown")
                else:
                    await query.edit_message_text(f"⚠️ Case {case_id} ለማዘመን ተሞክሯል፣ ነገር ግን አገልጋዩ የመለሰው ኮድ: {response.status_code}")
            except Exception as e:
                logging.error(f"Error terminating case {case_id}: {e}")
                await query.edit_message_text(f"❌ ስህተት፡ ኬዙን በዌብሳይቱ ላይ መዝጋት አልተቻለም።")

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
        BotCommand("start", "የክትትል ስራ ማስጀመሪያ"),
        BotCommand("pending", "ክፍት ኬዞችን ማሳያ"),
        BotCommand("terminate", "ኬዝ ለመዝጋት መምረጫ"),
        BotCommand("report", "ሳምንታዊ የስራ ሪፖርት"),
        BotCommand("monthly", "ወርሃዊ የኤክሴል ሪፖርት")
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
    
    print("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
