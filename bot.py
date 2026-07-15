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

# 2. የአካባቢ ተለዋዋጮችን ከRender ማግኘት
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")

# 3. Render እንዳይዘጋ የሚረዳው የጤና መፈተሻ ሰርቨር
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is Running and Alive!")
        
    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logging.info(f"Health check server started on port {port}")
    server.serve_forever()

# 4. ከዌብሳይቱ API መረጃ የሚስበው ዋናው ተግባር
async def scrape_website_cases():
    if not EMAIL or not PASSWORD:
        return [], "Error: EMAIL or PASSWORD environment variables are not set on Render!"

    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    api_url = 'https://api.tech24et.com/api/callentries?limit=200&callstatus=&start_date=&end_date=&active=&bank=&branch=&district='

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

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0, verify=False) as session:
        try:
            # ሀ. CSRF Cookie ማግኘት
            csrf_response = await session.get(csrf_url)
            logging.info(f"CSRF cookie status: {csrf_response.status_code}")

            # ለ. XSRF-TOKEN በሄደር ውስጥ ማካተት
            xsrf_token = session.cookies.get("XSRF-TOKEN")
            if xsrf_token:
                decoded_token = urllib.parse.unquote(xsrf_token)
                session.headers.update({
                    'X-XSRF-TOKEN': decoded_token
                })
                logging.info("X-XSRF-TOKEN successfully injected.")

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

            try:
                data = response.json()
            except ValueError:
                return [], "Error: API returned HTML instead of JSON."

            if not data:
                return [], "Error: API returned empty response!"

            cases_list = None
            if isinstance(data, dict):
                cases_list = data.get('data')
                if cases_list is None:
                    cases_list = data
            else:
                cases_list = data

            if not isinstance(cases_list, list):
                return [], "Error: API response data format is not a list!"

            scraped_cases = []
            for item in cases_list:
                if not item or not isinstance(item, dict):
                    continue
                
                case_id = str(item.get('id', ''))
                
                # የባንክ መረጃን በጥንቃቄ መውሰድ
                bank_info = item.get('bank')
                bank = ""
                if isinstance(bank_info, dict):
                    bank = bank_info.get('name', '')
                elif bank_info is not None:
                    bank = str(bank_info)

                # የዲስትሪክት መረጃን በጥንቃቄ መውሰድ
                district_info = item.get('district')
                district = ""
                if isinstance(district_info, dict):
                    district = district_info.get('name', '')
                elif district_info is not None:
                    district = str(district_info)

                # የቅርንጫፍ መረጃን በጥንቃቄ መውሰድ
                branch_info = item.get('branch')
                branch = ""
                if isinstance(branch_info, dict):
                    branch = branch_info.get('name', '')
                elif branch_info is not None:
                    branch = str(branch_info)

                issue = str(item.get('issue') or '')
                created_at = item.get('created_at')
                date_str = str(created_at)[:10] if created_at else ""
                status = str(item.get('status') or 'Pending')
                status_text = "Completed" if status == "Complete" else "Pending"

                # "Adama" ብቻ የሆኑትን ዲስትሪክቶች መለየት (exact match)
                if district.strip().lower() == "adama":
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
            return [], f"Error: Network connection problem ({str(req_err)})"
        except Exception as e:
            return [], f"Error: {str(e)}"

# 🛠️ 5. አዲሱ የሙከራ/ስህተት መፈለጊያ ተግባር (test_api)
async def test_api_call():
    """ይህ ተግባር ሎግኢን አድርጎ ከኤፒአይ የሚመጡትን የመጀመሪያ 3 ዲስትሪክቶች ዝርዝር ያሳያል"""
    if not EMAIL or not PASSWORD:
        return "Error: EMAIL or PASSWORD environment variables are not set on Render!"

    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    api_url = 'https://api.tech24et.com/api/callentries?limit=200'

    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Content-Type': 'application/json'
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0, verify=False) as session:
        try:
            # 1. CSRF
            csrf_res = await session.get(csrf_url)
            xsrf_token = session.cookies.get("XSRF-TOKEN")
            if xsrf_token:
                session.headers.update({'X-XSRF-TOKEN': urllib.parse.unquote(xsrf_token)})

            # 2. Login
            login_res = await session.post(login_url, json={'email': EMAIL, 'password': PASSWORD})
            if login_res.status_code not in [200, 201, 204]:
                return f"❌ Login Failed! Status Code: {login_res.status_code}\nResponse: {login_res.text[:200]}"

            # 3. Fetch Data
            api_res = await session.get(api_url)
            if api_res.status_code != 200:
                return f"❌ Fetch Failed! Status Code: {api_res.status_code}"

            data = api_res.json()
            cases_list = data.get('data', data) if isinstance(data, dict) else data

            if not cases_list or not isinstance(cases_list, list):
                return f"❌ API Connected, but data format is unexpected:\n{str(data)[:300]}"

            # በሲስተሙ ውስጥ ያሉትን የመጀመሪያዎቹን 3 ዲስትሪክቶች ስም ማውጣት
            found_districts = []
            for item in cases_list[:10]: # ከመጀመሪያዎቹ 10 ውስጥ
                dist_info = item.get('district')
                if isinstance(dist_info, dict):
                    found_districts.append(dist_info.get('name', ''))
                elif dist_info:
                    found_districts.append(str(dist_info))

            return (
                f"✅ ሎግኢን ሰርቷል! የኤፒአይ ግንኙነት ስኬታማ ነው።\n\n"
                f"📊 በሲስተሙ ውስጥ ያገኘኋቸው አንዳንድ የዲስትሪክት ስሞች፦\n"
                f"👉 {', '.join(list(set(found_districts))[:5])}\n\n"
                f"💡 እባክህ እነዚህ ስሞች ውስጥ 'Adama' የሚለው ቃል በትክክል እንዴት እንደተጻፈ እይልኝ።"
            )

        except Exception as e:
            return f"❌ ሙከራው አልተሳካም! የቴክኒክ ስህተት፦\n{str(e)}"

# 6. የቴሌግራም ቦት ትዕዛዞች

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 እንኳን ወደ Tech24 Adama መከታተያ ቦት በሰላም መጡ!\n\n"
        "የሚከተሉትን ትዕዛዞች ይጠቀሙ፦\n"
        "📋 /report - የAdama ኬዞች ሪፖርት ለማግኘት\n"
        "⏳ /pending - ያልተጠናቀቁ (Pending) ኬዞችን ብቻ ለማየት\n"
        "📊 /monthly - የወሩን ማጠቃለያ ሪፖርት ለማየት\n"
        "🛠️ /test - የኤፒአይ ግንኙነትን ለመፈተሽ (ስህተቱን ለማወቅ)"
    )
    await update.message.reply_text(welcome_text)

# አዲሱ /test ትዕዛዝ
async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 የኤፒአይ ግንኙነትን እና መረጃዎችን እየመረመርኩ ነው...")
    test_result = await test_api_call()
    await update.message.reply_text(test_result)

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ የAdama መረጃዎችን ከዌብሳይቱ ላይ እየፈለግኩ ነው፣ እባክዎ ትንሽ ይጠብቁ...")
    cases, status_msg = await scrape_website_cases()
    
    if status_msg != "OK":
        await update.message.reply_text(f"❌ ስህተት አጋጥሟል፦\n{status_msg}")
        return

    if not cases:
        await update.message.reply_text("📭 ለAdama የተመዘገበ ምንም አይነት ኬዝ አልተገኘም።")
        return

    report_msg = "📋 **የAdama የቅርብ ጊዜ ኬዞች ሪፖርት** 📋\n\n"
    for i, case in enumerate(cases[:15], 1):
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

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ ያልተጠናቀቁ የAdama ኬዞችን በመፈለግ ላይ...")
    cases, status_msg = await scrape_website_cases()
    
    if status_msg != "OK":
        await update.message.reply_text(f"❌ ስህተት አጋጥሟል፦\n{status_msg}")
        return

    pending_cases = [c for c in cases if c['status'] == "Pending"]

    if not pending_cases:
        await update.message.reply_text("✅ ሁሉም የAdama ኬዞች ተጠናቀዋል! ምንም Pending የለም።")
        return

    report_msg = "⏳ **የAdama በመጠባበቅ ላይ ያሉ (Pending) ኬዞች** ⏳\n\n"
    for i, case in enumerate(pending_cases[:15], 1):
        report_msg += (
            f"{i}. **ID:** {case['case_id']}\n"
            f"🏦 **Bank:** {case['bank']} ({case['branch']})\n"
            f"⚠️ **Issue:** {case['issue']}\n"
            f"📅 **Date:** {case['date']}\n"
            f"----------------------------------\n"
        )
    await update.message.reply_text(report_msg, parse_mode="Markdown")

async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 የAdama የወሩን ማጠቃለያ ሪፖርት በማዘጋጀት ላይ...")
    cases, status_msg = await scrape_website_cases()
    
    if status_msg != "OK":
        await update.message.reply_text(f"❌ ስህተት አጋጥሟል፦\n{status_msg}")
        return

    if not cases:
        await update.message.reply_text("📭 ምንም አይነት መረጃ አልተገኘም።")
        return

    total_cases = len(cases)
    completed_cases = len([c for c in cases if c['status'] == "Completed"])
    pending_cases = total_cases - completed_cases
    success_rate = (completed_cases / total_cases * 100) if total_cases > 0 else 0

    monthly_msg = (
        f"📊 **የAdama የወሩ ማጠቃለያ ሪፖርት** 📊\n\n"
        f"📁 **ጠቅላላ የኬዞች ብዛት:** {total_cases}\n"
        f"✅ **የተጠናቀቁ (Completed):** {completed_cases}\n"
        f"⏳ **በመጠባበቅ ላይ (Pending):** {pending_cases}\n"
        f"📈 **የአፈጻጸም ምጣኔ (Success Rate):** {success_rate:.1f}%\n\n"
        f"🎈 ሰላም ስራ!"
    )
    await update.message.reply_text(monthly_msg, parse_mode="Markdown")

# 7. ዋናው ማስነሻ
def main():
    if not BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN environment variable is missing!")
        return

    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("monthly", monthly_command))
    application.add_handler(CommandHandler("test", test_command))  # የቴስቱ ትዕዛዝ እዚህ ጋር ተያይዟል

    logging.info("Bot is starting polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
