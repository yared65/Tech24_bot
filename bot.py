import logging
import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# የቦት ቶከን እና የዌብሳይት ዝርዝሮች
BOT_TOKEN = os.getenv('BOT_TOKEN')
EMAIL = os.getenv('EMAIL')
PASSWORD = os.getenv('PASSWORD')

# ድግግሞሽን ለመከላከል
sent_cases = set()

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# የዌብሳይት ክትትል ተግባር
async def check_website_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    login_data = {'email': EMAIL, 'password': PASSWORD, 'submit': 'Login'}
    
    async with httpx.AsyncClient(follow_redirects=True) as session:
        try:
            await session.post('https://tech24et.com/client/index.php', data=login_data)
            response = await session.get('https://tech24et.com/client/cases.php')
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                table = soup.find('table')
                if not table: return
                
                rows = table.find_all('tr')[1:]
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) > 5:
                        case_id = cols[1].text.strip()
                        district = cols[3].text.strip()
                        
                        # የድግግሞሽ መከላከያ እና የአዳማ ዲስትሪክት ማጣሪያ
                        if "Adama District" in district and case_id not in sent_cases:
                            message = (
                                f"🔔 **ATM Incident Notification**\n\n"
                                f"📄 **ID:** {case_id}\n"
                                f"🏦 **Bank:** {cols[2].text.strip()}\n"
                                f"⚠️ **Issue:** {cols[5].text.strip()}\n"
                                f"🏢 **Branch:** {cols[4].text.strip()}\n"
                                f"📍 **District:** {district}\n"
                            )
                            await context.bot.send_message(chat_id=chat_id, text=message)
                            sent_cases.add(case_id)
        except Exception as e:
            logging.error(f"Error occurred: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # ነባር ስራዎችን ማጽዳት
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    
    context.job_queue.run_repeating(check_website_job, interval=900, first=5, chat_id=chat_id, name=str(chat_id))
    await update.message.reply_text("✅ የAdama District ክትትል ተጀምሯል።")


# --- Render Timed out እንዳይል የሚከላከል Dummy Server ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()
# --------------------------------------------------------

def main():
    # Health Serverን ከጀርባ ማስጀመር
    threading.Thread(target=run_health_server, daemon=True).start()
    
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is missing!")
        return
        
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    print("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
                        district = cols[3].text.strip()
                        
                        # የድግግሞሽ መከላከያ እና የአዳማ ዲስትሪክት ማጣሪያ
                        if "Adama District" in district and case_id not in sent_cases:
                            message = (
                                f"🔔 **ATM Incident Notification**\n\n"
                                f"📄 **ID:** {case_id}\n"
                                f"🏦 **Bank:** {cols[2].text.strip()}\n"
                                f"⚠️ **Issue:** {cols[5].text.strip()}\n"
                                f"🏢 **Branch:** {cols[4].text.strip()}\n"
                                f"📍 **District:** {district}\n"
                            )
                            await context.bot.send_message(chat_id=chat_id, text=message)
                            sent_cases.add(case_id)
        except Exception as e:
            logging.error(f"Error occurred: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # ነባር ስራዎችን ማጽዳት
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    
    context.job_queue.run_repeating(check_website_job, interval=900, first=5, chat_id=chat_id, name=str(chat_id))
    await update.message.reply_text("✅ የAdama District ክትትል ተጀምሯል።")

def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    print("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
