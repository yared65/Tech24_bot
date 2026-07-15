import os
import logging
import asyncio
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# 1. ሎጊንግ ማስተካከያ (Logging)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# 2. የአካባቢ ተለዋዋጮች ከ Render
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")

# 3. Render እንዳይዘጋ የሚረዳው የጤና መፈተሻ ሰርቨር (Keep-Alive)
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
    api_url = 'https://api.tech24et.com/api/callentries?limit=200'

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://tech24et.com',
        'Referer': 'https://tech24et.com/'
    }

    login_data = {
        'email': EMAIL.strip(),
        'password': PASSWORD.strip()
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0, verify=False) as session:
        try:
            # ሀ. CSRF Cookie ማግኘት
            await session.get(csrf_url)

            xsrf_token = session.cookies.get("XSRF-TOKEN")
            if xsrf_token:
                session.headers.update({
                    'X-XSRF-TOKEN': urllib.parse.unquote(xsrf_token)
                })

            # ለ. ሎግኢን ማካሄድ
            login_response = await session.post(login_url, json=login_data)
            if login_response.status_code not in [200, 201, 204]:
                return [], f"Login failed! Status: {login_response.status_code}"

            # ሐ. መረጃውን መሳብ
            response = await session.get(api_url)
            if response.status_code != 200:
                return [], f"Failed to fetch data! Status: {response.status_code}"

            try:
                data = response.json()
            except ValueError:
                return [], "Error: API returned HTML instead of JSON."

            # የዳታ ፎርማት ማስተካከያ
            cases_list = data.get('data', data) if isinstance(data, dict) else data
            if not isinstance(cases_list, list):
                return [], "Error: API response data format is not a list!"

            scraped_cases = []
            for item in cases_list:
                if not item or not isinstance(item, dict):
                    continue
                
                # 💡 ጠንካራ የዲስትሪክት ፍተሻ (Robust District Extraction)
                # APIው ዲስትሪክቱን በዲክሽነሪም ይላከው ወይም በቀጥታ ቴክስት፣ እዚህ ጋር ሁለቱንም እንፈትሻለን፦
                district_val = item.get('district', '')
                district = ""
                
                if isinstance(district_val, dict):
                    # nested object ከሆነ ስሙን እንወስዳለን
                    district = district_val.get('name', district_val.get('district_name', ''))
                elif isinstance(district_val, str):
                    # ቀጥታ ጽሑፍ ከሆነ ራሱን እንወስዳለን
                    district = district_val
                
                # ዲስትሪክቱን ወደ ስትሪንግ ቀይረን ባዶ ቦታዎችን እናጸዳለን
                district_clean = str(district or '').strip().lower()

                # 🎯 የዲስትሪክቱ ስም "adama" መሆኑን ማረጋገጫ
                if "adama" in district_clean or district_clean == "adama":
                    case_id = str(item.get('id', item.get('case_id', '')))
                    
                    # የባንክ መረጃ
                    bank_info = item.get('bank')
                    bank = bank_info.get('name', '') if isinstance(bank_info, dict) else str(bank_info or '')

                    # የቅርንጫፍ መረጃ
                    branch_info = item.get('branch')
                    branch = branch_info.get('name', '') if isinstance(branch_info, dict) else str(branch_info or '')

                    # ችግሩ (Issue/Case Type)
                    issue = str(item.get('issue') or item.get('case_type') or '')
                    
                    # አስተያየት (Comment)
                    comment = str(item.get('comment') or '')

                    created_at = item.get('created_at')
                    date_str = str(created_at)[:10] if created_at else ""
                    
                    status = str(item.get('status') or 'Pending')
                    status_text = "Completed" if status.lower() in ["complete", "completed"] else "Pending"

                    scraped_cases.append({
                        'case_id': case_id,
                        'bank': bank,
                        'district': district if district else "Adama",
                        'branch': branch,
                        'issue': issue,
                        'comment': comment,
                        'status': status_text,
                        'date': date_str
                    })

            return scraped_cases, "OK"

        except Exception as e:
            return [], f"Error: {str(e)}"

# 5. የኤፒአይ ግንኙነትን እና የመጀመሪያዎቹን መረጃዎች መፈተሻ (test_api)
async def test_api_call():
    if not EMAIL or not PASSWORD:
        return "Error: EMAIL or PASSWORD environment variables are not set on Render!"

    csrf_url = 'https://api.tech24et.com/sanctum/csrf-cookie'
    login_url = 'https://api.tech24et.com/api/login'
    api_url = 'https://api.tech24et.com/api/callentries?limit=200'

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
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
                session.headers.update({'X-XSRF-TOKEN': urllib.parse.unquote(xsrf_token)})

            login_res = await session.post(login_url, json={'email': EMAIL.strip(), 'password': PASSWORD.strip()})
            if login_res.status_code not in [200, 201, 204]:
                return f"❌ Login Failed! Status Code: {login_res.status_code}"

            api_res = await session.get(api_url)
            if api_res.status_code != 200:
                return f"❌ Fetch Failed! Status Code: {api_res.status_code}"

            data = api_res.json()
            cases_list = data.get('data', data) if isinstance(data, dict) else data

            if not cases_list or not isinstance(cases_list, list):
                return "❌ API Connected, but returned unexpected format."

            # የተገኙ ዲስትሪክቶችን እና ኪዎችን (keys) ለማየት
            sample_keys = list(cases_list[0].keys()) if cases_list else []
            found_districts = []
            
            for item in cases_list:
                dist_val = item.get('district', '')
                if isinstance(dist_val, dict):
                    dist_name = dist_val.get('name', dist_val.get('district_name', ''))
                else:
                    dist_name = str(dist_val or '')
                if dist_name:
                    found_districts.append(dist_name)

            unique_districts = list(set(found_districts))

            return (
                f"✅ ግንኙነቱ ሙሉ በሙሉ ተሳክቷል!\n\n"
                f"📊 በሲስተሙ ውስጥ የተገኙ ቁልፎች (Keys)፦ {', '.join(sample_keys[:6])}\n"
                f"👉 በዳታው ውስጥ ያሉ ዲስትሪክቶች፦ {', '.join(unique_districts[:10])}\n\n"
                f"💡 ሁሉም ነገር መስራት ጀምሯል።"
            )

        except Exception as e:
            return f"❌ የቴክኒክ ስህተት አጋጥሟል፦\n{str(e)}"

# 6. የቴሌግራም ቦት ትዕዛዞች
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 እንኳን ወደ Tech24 Adama መከታተያ ቦት በሰላም መጡ!\n\n"
        "የሚከተሉትን ትዕዛዞች ይጠቀሙ፦\n"
        "📋 /report - የAdama ኬዞች ሪፖርት ለማግኘት\n"
        "⏳ /pending - ያልተጠናቀቁ (Pending) ኬዞችን ብቻ ለማየት\n"
        "📊 /monthly - የወሩን ማጠቃለያ ሪፖርት ለማየት\n"
        "🛠️ /test - የኤፒአይ ግንኙነትን ፈጣን ፍተሻ ለማድረግ"
    )
    await update.message.reply_text(welcome_text)

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
        comment_str = f"\n💬 **Comment:** {case['comment']}" if case['comment'] else ""
        report_msg += (
            f"{i}. **ID:** {case['case_id']}\n"
            f"🏦 **Bank:** {case['bank']} ({case['branch']})\n"
            f"⚠️ **Issue:** {case['issue']}{comment_str}\n"
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
        comment_str = f"\n💬 **Comment:** {case['comment']}" if case['comment'] else ""
        report_msg += (
            f"{i}. **ID:** {case['case_id']}\n"
            f"🏦 **Bank:** {case['bank']} ({case['branch']})\n"
            f"⚠️ **Issue:** {case['issue']}{comment_str}\n"
            f"📅 **Date:** {case['date']}\n"
            f"----------------------------------\n"
        )
    await update.message.reply_text(report_msg, parse_mode="Markdown")

async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 የAdama የወሩ ማጠቃለያ ሪፖርት በማዘጋጀት ላይ...")
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

    # የጤና መፈተሻ ሰርቨር ማስጀመር
    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()

    # የቴሌግራም ቦት መተግበሪያን መፍጠር
    application = Application.builder().token(BOT_TOKEN).build()

    # ትዕዛዞችን ማገናኘት (Handlers)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("monthly", monthly_command))

    # ቦቱን ስራ ማስጀመር
    logging.info("Bot is starting polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
