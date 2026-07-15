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
    # ገደቡን ወደ 200 ከፍ በማድረግ ሁሉንም የቅርብ ጊዜ ኬዞች መሳብ እንችላለን
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

            # በፎቶዎቹ መሰረት ዳታው የሚገኘው በ 'data' ቁልፍ ውስጥ ነው
            cases_list = data.get('data', []) if isinstance(data, dict) else data
            if not isinstance(cases_list, list):
                return [], "Error: API response data format is not a list!"

            scraped_cases = []
            for item in cases_list:
                if not item or not isinstance(item, dict):
                    continue
                
                # 💡 በምስሉ ላይ እንደሚታየው 'district' የሚለው ቁልፍ በቀጥታ አለ።
                # District Object ወይም String መሆኑን በማረጋገጥ ዋጋውን እናወጣለን።
                district_val = item.get('district', '')
                district_name = ""
                if isinstance(district_val, dict):
                    district_name = district_val.get('name', '')
                elif isinstance(district_val, str):
                    district_name = district_val
                
                district_clean = str(district_name).strip().lower()

                # 🎯 የ Adama ማጣሪያ፦ District "adama" መሆኑን ብቻ መፈተሽ በቂ ነው!
                if district_clean == "adama":
                    # ID ማውጣት
                    case_id = str(item.get('callentry_id', item.get('id', '')))
                    
                    # Bank ማውጣት
                    bank_val = item.get('bank', '')
                    bank_name = bank_val.get('name', '') if isinstance(bank_val, dict) else str(bank_val)
                    
                    # Branch ማውጣት
                    branch_val = item.get('branch', '')
                    branch_name = branch_val.get('name', '') if isinstance(branch_val, dict) else str(branch_val)
                    
                    # Description / Issue ማውጣት (በምስሉ መሰረት 'callentry_description' ነው)
                    issue = str(item.get('callentry_description', item.get('issue', 'No description')))
                    
                    # Date ማውጣት (በምስሉ መሰረት 'created_at' ወይም 'start_date' ሊሆን ይችላል)
                    created_at = item.get('created_at', item.get('start_date', ''))
                    date_str = str(created_at)[:10] if created_at else "N/A"
                    
                    # Status (ሁኔታ) ማውጣት
                    # በምስሉ ላይ እንደሚታየው የ status ቁልፍ 'callentry_status' ወይም 'status' ሊሆን ይችላል።
                    status = str(item.get('callentry_status', item.get('status', 'Pending')))
                    
                    # 'completed' ወይም 'complete' ወይም '1' ከሆኑ Completed ይባላል
                    if status.lower() in ["complete", "completed", "1", "done"]:
                        status_text = "Completed"
                    else:
                        status_text = "Pending"

                    scraped_cases.append({
                        'case_id': case_id,
                        'bank': bank_name if bank_name else "N/A",
                        'district': "Adama",
                        'branch': branch_name if branch_name else "Adama Branch",
                        'issue': issue,
                        'status': status_text,
                        'date': date_str
                    })

            return scraped_cases, "OK"

        except Exception as e:
            return [], f"Error: {str(e)}"

# 5. የኤፒአይ ግንኙነትን ፈጣን ፍተሻ (test_api)
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
            cases_list = data.get('data', []) if isinstance(data, dict) else data

            if not isinstance(cases_list, list):
                return "❌ API Connected, but returned unexpected format."

            total_scraped = len(cases_list)

            # በዳታው ውስጥ 'Adama' የሆኑትን መቁጠር
            adama_count = 0
            for item in cases_list:
                district_val = item.get('district', '')
                district_name = district_val.get('name', '') if isinstance(district_val, dict) else str(district_val)
                if str(district_name).strip().lower() == "adama":
                    adama_count += 1

            return (
                f"✅ ግንኙነቱ ሙሉ በሙሉ ተሳክቷል!\n\n"
                f"📊 በሲስተሙ ውስጥ በአጠቃላይ {total_scraped} የቅርብ ጊዜ ኬዞች ተገኝተዋል።\n"
                f"🎯 ከእነዚህ ውስጥ **{adama_count}** የ Adama ኬዞች ናቸው።\n\n"
                f"💡 አሁን የ Adama ማጣሪያ በስኬት ተስተካክሏል፤ እባክዎ /report ብለው ይሞክሩ።"
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
    # ለመልዕክት ማሳጠሪያ የቅርብ ጊዜ 15ቱን ብቻ እናሳያለን
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
        f"🎈 መልካም የስራ ጊዜ!"
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
