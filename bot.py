import os
import logging
import asyncio
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# 1. ሎጊንግ ማስተካከያ (Logging Setup)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# 2. የአካባቢ ተለዋዋጮችን (Environment Variables) ከRender ማግኘት
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")

# 3. Render እንዳይዘጋ የሚረዳው የጤና መፈተሻ ሰርቨር (Keep-Alive Health Server)
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is Running and Alive!")
        
    def log_message(self, format, *args):
        return  # ሎጉን እንዳይጨናነቅ ጸጥ ለማድረግ

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logging.info(f"Health check server started on port {port}")
    server.serve_forever()

# 4. ከዌብሳይቱ API መረጃ የሚስበው ዋናው ተግባር (API Scraper)
async def scrape_website_cases():
    if not EMAIL or not PASSWORD:
        return [], "Error: EMAIL or PASSWORD environment variables are not set on Render!"

    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    api_url = 'https://api.tech24et.com/api/callentries?limit=200&callstatus=&start_date=&end_date=&active=&bank=&branch=&district='

    # ለሎግኢን የሚያስፈልጉ መሰረታዊ ሄደሮች
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Content-Type': 'application/json',
        'Origin': 'https://tech24et.com',
        'Referer': 'https://tech24et.com/'
    }

    login_data = {
        'email': EMAIL,
        'password': PASSWORD
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0) as session:
        try:
            # ሀ. መጀመሪያ CSRF Cookie ለማግኘት ጥሪ እናደርጋለን
            csrf_response = await session.get(csrf_url)
            logging.info(f"CSRF cookie status: {csrf_response.status_code}")

            # ለ. ከኩኪው ውስጥ XSRF-TOKEN የሚለውን ፈልገን በሄደር ውስጥ እናስገባለን (ይህ 419 ስህተትን ይፈታል!)
            xsrf_token = session.cookies.get("XSRF-TOKEN")
            if xsrf_token:
                decoded_token = urllib.parse.unquote(xsrf_token)
                session.headers.update({
                    'X-XSRF-TOKEN': decoded_token
                })
                logging.info("X-XSRF-TOKEN successfully injected into headers.")
            else:
                logging.warning("XSRF-TOKEN cookie not found in the initial request!")

            # ሐ. ሎግኢን ማድረግ
            login_response = await session.post(login_url, json=login_data)
            logging.info(f"API Login status: {login_response.status_code}")

            if login_response.status_code not in [200, 201, 204]:
                return [], f"Login failed! Status: {login_response.status_code}"

            # መ. መረጃውን መሳብ
            response = await session.get(api_url)
            logging.info(f"API Fetch status: {response.status_code}")

            if response.status_code != 200:
                return [], f"Failed to fetch data! Status: {response.status_code}"

            data = response.json()
            cases_list = data.get('data', data) if isinstance(data, dict) else data

            if not isinstance(cases_list, list):
                return [], "Error: API response format is invalid!"

            scraped_cases = []
            for item in cases_list:
                case_id = str(item.get('id', ''))
                
                # የባንክ፣ ዲስትሪክትና ቅርንጫፍ መረጃዎችን መውሰድ
                bank_info = item.get('bank')
                bank = bank_info.get('name', '') if isinstance(bank_info, dict) else str(bank_info or '')

                district_info = item.get('district')
                district = district_info.get('name', '') if isinstance(district_info, dict) else str(district_info or '')

                branch_info = item.get('branch')
                branch = branch_info.get('name', '') if isinstance(branch_info, dict) else str(branch_info or '')

                issue = item.get('issue', '')
                date_str = item.get('created_at', '')[:10] if item.get('created_at') else ''
                
                status = item.get('status', 'Pending')
                status_text = "Completed" if status == "Complete" else "Pending"

                # Adama District መረጃዎችን ብቻ መለየት
                if "adama" in district.lower():
                    scraped_cases.append({
                        'case_id': case_id,
                        'bank': bank,
                        'district': district,
                        'branch': branch,
                        'issue': issue,
                        'time_val': "1h",
                        'status': status_text,
                        'date': date_str
                    })

            return scraped_cases, "OK"

        except httpx.RequestError as req_err:
            logging.error(f"Network error: {req_err}")
            return [], "Error: Network connection timeout with API."
        except Exception as e:
            logging.error(f"Parsing error: {e}")
            return [], f"Error: {str(e)}"

# 5. የቴሌግራም ቦት ትዕዛዞች (Bot Commands Handlers)

# /start ትዕዛዝ
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 እንኳን ወደ Tech24 Adama District መከታተያ ቦት በሰላም መጡ!\n\n"
        "የሚከተሉትን ትዕዛዞች ይጠቀሙ፦\n"
        "📋 /report - የሁሉንም ኬዞች ሪፖርት ለማግኘት\n"
        "⏳ /pending - ያልተጠናቀቁ (Pending) ኬዞችን ብቻ ለማየት\n"
        "📊 /monthly - የወሩን ማጠቃለያ ሪፖርት ለማየት"
    )
    await update.message.reply_text(welcome_text)

# /report ትዕዛዝ
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ የAdama District መረጃዎችን ከዌብሳይቱ ላይ እየፈለግኩ ነው፣ እባክዎ ትንሽ ይጠብቁ...")
    
    cases, status_msg = await scrape_website_cases()
    
    if status_msg != "OK":
        await update.message.reply_text(f"❌ ስህተት አጋጥሟል፦\n{status_msg}")
        return

    if not cases:
        await update.message.reply_text("📭 ለአዳማ ዲስትሪክት የተመዘገበ ምንም አይነት ኬዝ አልተገኘም።")
        return

    # ሪፖርት መገንባት
    report_msg = "📋 **የAdama District የቅርብ ጊዜ ኬዞች ሪፖርት** 📋\n\n"
    for i, case in enumerate(cases[:15], 1): # እስከ 15 ኬዞችን እንዲያሳይ
        status_icon = "✅" if case['status'] == "Completed" else "⏳"
        report_msg += (
            f"{i}. **ID:** {case['case_id']}\n"
            f"🏦 **Bank:** {case['bank']} ({case['branch']})\n"
            f"⚠️ **Issue:** {case['issue']}\n"
            f"📅 **Date:** {case['date']}\n"
            f"📌 **Status:** {status_icon} {case['status']}\n"
            f"----------------------------------\n"
        )
    
    await update.message.reply_text(report_msg, parse_mode="Markdown")

# /pending ትዕዛዝ
async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ ያልተጠናቀቁ ኬዞችን በመፈለግ ላይ...")
    
    cases, status_msg = await scrape_website_cases()
    
    if status_msg != "OK":
        await update.message.reply_text(f"❌ ስህተት አጋጥሟል፦\n{status_msg}")
        return

    pending_cases = [c for c in cases if c['status'] == "Pending"]

    if not pending_cases:
        await update.message.reply_text("✅ ሁሉም የAdama District ኬዞች ተጠናቀዋል! ምንም Pending የለም።")
        return

    report_msg = "⏳ **የAdama District በመጠባበቅ ላይ ያሉ (Pending) ኬዞች** ⏳\n\n"
    for i, case in enumerate(pending_cases, 1):
        report_msg += (
            f"{i}. **ID:** {case['case_id']}\n"
            f"🏦 **Bank:** {case['bank']} ({case['branch']})\n"
            f"⚠️ **Issue:** {case['issue']}\n"
            f"📅 **Date:** {case['date']}\n"
            f"----------------------------------\n"
        )
    
    await update.message.reply_text(report_msg, parse_mode="Markdown")

# 6. ዋናው ማስነሻ (Main Function)
def main():
    if not BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN environment variable is missing!")
        return

    # ሀ. የጤና መፈተሻ ሰርቨሩን በሌላ Thread ላይ ማስጀመር (Render እንዳይዘጋው)
    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()

    # ለ. የቴሌግራም ቦት መተግበሪያን መፍጠር
    application = Application.builder().token(BOT_TOKEN).build()

    # ሐ. ትዕዛዞችን ማገናኘት (Handlers)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("pending", pending_command))

    # መ. ቦቱን ስራ ማስጀመር
    logging.info("Bot is starting polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
