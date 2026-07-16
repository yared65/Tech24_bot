import os
import logging
import asyncio
import threading
import urllib.parse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# 📂 የተላኩ ኬዞች ID እና ኖቲፊኬሽን የሚላክላቸውን ቻቶች ሴቭ ማድረጊያ ፋይሎች
NOTIFIED_CASES_FILE = "notified_cases.json"
CHATS_FILE = "registered_chats.json"

# ሴቭ የተደረጉ ዳታዎችን ከፋይል ላይ መጫኛ ተግባራት
def load_data(file_name, default_val):
    if os.path.exists(file_name):
        try:
            with open(file_name, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading {file_name}: {e}")
    return default_val

def save_data(file_name, data):
    try:
        with open(file_name, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logging.error(f"Error saving {file_name}: {e}")

# ዳታዎችን በሜሞሪ መያዝ
notified_cases = load_data(NOTIFIED_CASES_FILE, [])
registered_chats = load_data(CHATS_FILE, [])

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

# 💡 ከዳታው ውስጥ ተስማሚ ቁልፎችን (Keys) በራስ-ሰር የሚፈልግ ብልህ ተግባር
def extract_field(item, keyword):
    if not isinstance(item, dict):
        return ""
    
    # 1. ልክ እንደ keyword የሆነው ቁልፍ ካለ በቀጥታ መውሰድ (e.g., 'district')
    for k, v in item.items():
        if k.lower() == keyword.lower():
            if isinstance(v, dict):
                return v.get('name', v.get('title', str(v)))
            return str(v)
            
    # 2. ከፊል ቁልፍ ፍለጋ (e.g., 'callentry_district' ውስጥ 'district' ካለ መውሰድ)
    for k, v in item.items():
        if keyword.lower() in k.lower():
            if isinstance(v, dict):
                return v.get('name', v.get('title', str(v)))
            return str(v)
            
    return ""

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

            cases_list = data.get('data', []) if isinstance(data, dict) else data
            if not isinstance(cases_list, list):
                return [], "Error: API response data format is not a list!"

            scraped_cases = []
            for item in cases_list:
                if not item or not isinstance(item, dict):
                    continue
                
                # 🎯 ብልህ ማጣሪያ (Smart Filter)፦ "adama" የሚለው ቃል በጠቅላላው የዳታው ክፍል ውስጥ ካለ
                item_str_lower = str(item).lower()
                if "adama" in item_str_lower:
                    # መለያዎችን በራስ-ሰር ፈልጎ ማውጣት
                    case_id = str(item.get('callentry_id', item.get('id', 'N/A')))
                    bank = extract_field(item, 'bank')
                    branch = extract_field(item, 'branch')
                    issue = extract_field(item, 'description') or extract_field(item, 'issue') or "No Description"
                    created_at = item.get('created_at', item.get('start_date', ''))
                    date_str = str(created_at) if created_at else "N/A"
                    comment = extract_field(item, 'comment') or extract_field(item, 'remark') or "No Comment"
                    
                    # Status መለየት
                    status = extract_field(item, 'status') or extract_field(item, 'progress') or "Pending"
                    status_text = "Completed" if status.lower() in ["complete", "completed", "1", "done"] else "Pending"

                    scraped_cases.append({
                        'case_id': case_id,
                        'bank': bank if bank else "Awash/Dashen",
                        'district': "Adama",
                        'branch': branch if branch else "Adama Branch",
                        'issue': issue,
                        'status': status_text,
                        'date': date_str,
                        'comment': comment
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
            cases_list = data.get('data', []) if isinstance(data, dict) else data

            if not cases_list or not isinstance(cases_list, list):
                return "❌ API Connected, but returned unexpected format."

            total_scraped = len(cases_list)
            adama_count = sum(1 for item in cases_list if "adama" in str(item).lower())
            sample_keys = list(cases_list[0].keys()) if cases_list else []

            return (
                f"✅ ግንኙነቱ ሙሉ በሙሉ ተሳክቷል!\n\n"
                f"📊 በሲስተሙ ውስጥ በአጠቃላይ {total_scraped} የቅርብ ጊዜ ኬዞች ተገኝተዋል።\n"
                f"🎯 ከእነዚህ ውስጥ **{adama_count}** የ Adama ኬዞች ተለይተዋል።\n\n"
                f"🔑 የኤፒአይ ቁልፎች ዝርዝር፦\n`{', '.join(sample_keys[:8])}...`\n\n"
                f"💡 አሁን /report ብለው በመሞከር ማረጋገጥ ይችላሉ።"
            )

        except Exception as e:
            return f"❌ የቴክኒክ ስህተት አጋጥሟል፦\n{str(e)}"

# 🔄 በየ 10 ደቂቃው ሮጦ አዳዲስ ኬዞችን ቼክ የሚያደርገው background Job
async def check_new_cases(context: ContextTypes.DEFAULT_TYPE):
    logging.info("Background job: Checking for new Adama cases...")
    
    global notified_cases, registered_chats
    if not registered_chats:
        logging.info("No chats registered yet. Skipping check.")
        return

    cases, status_msg = await scrape_website_cases()
    if status_msg != "OK" or not cases:
        return

    # አዳዲስ ኬዞችን ብቻ መለየት (ከተመዘገቡት ID ውጭ የሆኑትን)
    new_cases = [c for c in cases if c['case_id'] not in notified_cases]

    if not new_cases:
        logging.info("No new Adama cases found.")
        return

    logging.info(f"Found {len(new_cases)} new cases. Sending notifications...")

    # አዲስ ኬዝ ሲገኝ ለእያንዳንዱ የተመዘገበ ቻት/ግሩፕ ኖቲፊኬሽን መላክ
    for case in new_cases:
        # ልክ በፎቶው ላይ እንዳለው ዓይነት የተዋበ ፎርማት
        notification_text = (
            f"⚠️ **ATM Incident Notification** ⚠️\n\n"
            f"📄 **ID:** {case['case_id']},\n"
            f"🏦 **Bank:** {case['bank']},\n"
            f"⚠️ **Issue:** {case['issue']},\n"
            f"🏢 **Branch:** {case['branch']},\n"
            f"📍 **District:** {case['district']},\n"
            f"💬 **Comment:** {case['comment']},\n"
            f"🕒 **Reported at:** {case['date']}"
        )

        # "Open In tech24et.com dashboard" የሚለውን በተን ማዘጋጀት
        keyboard = [
            [InlineKeyboardButton("Open In tech24et.com dashboard ↗️", url="https://tech24et.com/")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # ለሁሉም የተመዘገቡ ተጠቃሚዎች መላክ
        for chat_id in registered_chats:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=notification_text,
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
            except Exception as send_err:
                logging.error(f"Failed to send notification to chat {chat_id}: {send_err}")

        # ይህንን ID ድጋሚ እንዳይልከው ሴቭ ማድረግ
        notified_cases.append(case['case_id'])

    # የተላኩትን ID እና የቻት መረጃዎች በፋይል ላይ ማዘመን
    save_data(NOTIFIED_CASES_FILE, notified_cases)

# 6. የቴሌግራም ቦት ትዕዛዞች
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    global registered_chats

    # ቻት አይዲው ከዚህ በፊት ካልተመዘገበ መመዝገብ
    if chat_id not in registered_chats:
        registered_chats.append(chat_id)
        save_data(CHATS_FILE, registered_chats)
        logging.info(f"New chat registered for notifications: {chat_id}")

    welcome_text = (
        "👋 እንኳን ወደ Tech24 Adama መከታተያ ቦት በሰላም መጡ!\n\n"
        "🔔 ቦቱ በየ **10 ደቂቃው** በስተጀርባ አዳዲስ የ Adama ኬዞችን ቼክ ያደርጋል። አዲስ ኬዝ ሲገባ ወዲያውኑ ኖቲፊኬሽን ይልክልዎታል።\n\n"
        "የሚከተሉትን ትዕዛዞች በፈለጉት ጊዜ መጠቀም ይችላሉ፦\n"
        "📋 /report - የAdama ኬዞች ሪፖርት ለማግኘት\n"
        "⏳ /pending - ያልተጠናቀቁ (Pending) ኬዞችን ብቻ ለማየት\n"
        "📊 /monthly - የወሩን ማጠቃለያ ሪፖርት ለማየት\n"
        "🛠️ /test - የኤፒአይ ግንኙነትን ፈጣን ፍተሻ ለማድረግ"
    )
    await update.message.reply_text(welcome_text)

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 የኤፒአይ ግንኙነትን እና መረጃዎችን እየመረመርኩ ነው...")
    test_result = await test_api_call()
    await update.message.reply_text(test_result, parse_mode="Markdown")

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
    # 'job_queue'ን ለመጠቀም አዲሱን ስሪት እናስጀምራለን
    application = Application.builder().token(BOT_TOKEN).build()

    # ትዕዛዞችን ማገናኘት (Handlers)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("monthly", monthly_command))

    # ⏳ በየ 10 ደቂቃው (600 ሰከንድ) የሚሮጠውን background job መመዝገብ
    job_queue = application.job_queue
    # የመጀመሪያው ቼክ ቦቱ በጀመረ በ 10ኛው ሰከንድ ላይ ይሮጣል፤ ከዚያ በየ 600 ሰከንዱ (10 ደቂቃ) ይደጋገማል
    job_queue.run_repeating(check_new_cases, interval=600, first=10)
    logging.info("Background Job Queue for checking new cases initialized (Interval: 10 mins).")

    # ቦቱን ስራ ማስጀመር
    logging.info("Bot is starting polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
