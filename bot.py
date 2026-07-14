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

# የቦት ቶከን እና የዌብሳይት ዝርዝሮች ከRender Environment Variables ይነበባሉ
BOT_TOKEN = os.getenv('BOT_TOKEN')
EMAIL = os.getenv('EMAIL')
PASSWORD = os.getenv('PASSWORD')

# ድግግሞሽን ለመከላከል የተላኩ ኬዞች ማከማቻ
sent_cases = set()

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- የዌብሳይት መረጃዎችን በአንድ ላይ ሰብስቦ የሚያመጣ Helper Function ---
async def scrape_website_cases():
    login_data = {'email': EMAIL, 'password': PASSWORD, 'submit': 'Login'}
    async with httpx.AsyncClient(follow_redirects=True) as session:
        try:
            await session.post('https://tech24et.com/client/index.php', data=login_data)
            response = await session.get('https://tech24et.com/client/cases.php')
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                table = soup.find('table')
                if not table:
                    return []
                
                scraped_cases = []
                rows = table.find_all('tr')[1:]
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) > 5:
                        case_id = cols[1].text.strip()
                        bank = cols[2].text.strip()
                        district = cols[3].text.strip()
                        branch = cols[4].text.strip()
                        issue = cols[5].text.strip()
                        
                        time_val = cols[6].text.strip() if len(cols) > 6 else "1h"
                        status = cols[7].text.strip() if len(cols) > 7 else "Pending"
                        
                        date_str = datetime.now().strftime("%d/%m/%Y")
                        for col in cols:
                            text = col.text.strip()
                            match = re.search(r'\d{2}/\d{2}/\d{4}', text)
                            if match:
                                date_str = match.group(0)
                                break
                                
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
                return scraped_cases
        except Exception as e:
            logging.error(f"Scraping error: {e}")
            return []
    return []

# --- 1. /start (የAdama District ክትትል ማስጀመሪያ) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    
    context.job_queue.run_repeating(check_website_job, interval=900, first=5, chat_id=chat_id, name=str(chat_id))
    await update.message.reply_text("✅ የAdama District ክትትል ተጀምሯል።")

# በየ15 ደቂቃው ራሱ የሚሰራው ጆብ
async def check_website_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    cases = await scrape_website_cases()
    
    for c in cases:
        if "Adama District" in c['district'] and c['case_id'] not in sent_cases:
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
    cases = await scrape_website_cases()
    
    adama_pending = [c for c in cases if "Adama District" in c['district'] and c['status'].lower() != "completed"]
    
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

# --- 3. /terminate (Completed case - መዝጊያ) ---
async def terminate_cases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ ለመዝጋት የሚሆኑ ኬዞችን በመፈለግ ላይ...")
    cases = await scrape_website_cases()
    adama_pending = [c for c in cases if "Adama District" in c['district'] and c['status'].lower() != "completed"]
    
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
    cases = await scrape_website_cases()
    adama_cases = [c for c in cases if "Adama District" in c['district']]
    
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

# --- 5. /monthly (monthly Report - Excel ሪፖርት መላኪያ) ---
async def monthly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 የ 1 ወር የ Excel ሪፖርት እየተዘጋጀ ነው...")
    cases = await scrape_website_cases()
    adama_cases = [c for c in cases if "Adama District" in c['district']]
    
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

# --- የባተን ክሊኮችን የሚያስተናግድ Callback Handler ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "cancel_action":
        await query.edit_message_text("❌ ክዋኔው ተሰርዟል።")
        return
        
    elif data.startswith("view_"):
        case_id = data.split("_")[1]
        cases = await scrape_website_cases()
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
        cases = await scrape_website_cases()
        adama_pending = [c for c in cases if "Adama District" in c['district'] and c['status'].lower() != "completed"]
        
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
                await session.post('https://tech24et.com/client/index.php', data=login_data)
                
                terminate_url = 'https://tech24et.com/client/cases.php'
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

# --- Render Timed out እንዳይል የሚከላከል Dummy Web Server ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# ቦቱ ሲጀምር የቴሌግራም ሜኑዎችን (Commands) ራሱ እንዲፈጥር ማድረግ
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
    
    # Handlers
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
