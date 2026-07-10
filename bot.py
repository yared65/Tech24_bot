import logging
import requests
import asyncio
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# 1. የቴሌግራም መረጃዎች (ከፎቶው ላይ የተወሰደ)
BOT_TOKEN = '8222734631:AAHBdiZiVKSqYdKKNUOEm7UCv7Yi2vbPQz0'

# 2. የቴክ24 ሊንኮች እና መግቢያ አካውንት
LOGIN_URL = 'https://tech24et.com/client/index.php'
DASHBOARD_URL = 'https://tech24et.com/client/dashboard.php'
EMAIL = 'yaredgirma65@gmail.com'
PASSWORD = 'Tech24@123'

# ሰርቨር ላይ ስህተቶች ካሉ በሎግ (Log) መከታተል እንዲቻል
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# በየ 15 ደቂቃው ዌብሳይቱን የሚፈትሽ ዋናው ተግባር
async def check_website_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    session = requests.Session()
    
    login_data = {
        'email': EMAIL,
        'password': PASSWORD,
        'submit': 'Login'
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        logging.info("ወደ tech24et.com ሎግኢን በማድረግ ላይ...")
        # 1. ሎግኢን ማድረግ
        session.post(LOGIN_URL, data=login_data, headers=headers)
        
        # 2. ወደ ዳሽቦርድ መግባት
        dashboard_response = session.get(DASHBOARD_URL, headers=headers)
        
        if dashboard_response.status_code == 200:
            soup = BeautifulSoup(dashboard_response.text, 'html.parser')
            page_text = soup.get_text()
            
            # 3. "ያሬድ ግርማ" የሚለውን ስም መፈለግ
            if "ያሬድ ግርማ" in page_text or "Yared Girma" in page_text:
                message = "🔔 ሰላም ያሬድ ግርማ!\nበ tech24et.com ዳሽቦርድ ላይ ያንተ ስም የተመዘገበበት አዲስ የ ATM ስራ ተገኝቷል። ፈጥነህ እይ!"
                await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")
                logging.info("አዲስ መረጃ ተገኝቶ ለቴሌግራም ተልኳል።")
            else:
                # በየ 15 ደቂቃው ምንም መረጃ እንደሌለ ቦቱ ላይ ያሳውቃል (ይህ እንዲመጣ ካልፈለግክ ከስር ያለውን መስመር ማጥፋት ትችላለህ)
                await context.bot.send_message(chat_id=chat_id, text="🔄 እየፈለግኩ ነው... እስካሁን 'ያሬድ ግርማ' የሚል አዲስ ስም አልተገኘም።")
                logging.info("ፍተሻ ተጠናቋል፡ 'ያሬድ ግርማ' የሚል ስም አልተገኘም።")
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ ዌብሳይቱን መክፈት አልተቻለም። Status: {dashboard_response.status_code}")
            logging.error(f"ዳሽቦርድ መክፈት አልተቻለም Status code: {dashboard_response.status_code}")
            
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ ስህተት ተከስቷል: {e}")
        logging.error(f"Exception ተከስቷል: {e}")

# /start ሲባል የ 15 ደቂቃውን ክትትል የሚጀምር
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # ቀደም ሲል የነበረ ተመሳሳይ ክትትል ካለ ያጠፋዋል
    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in current_jobs:
        job.schedule_removal()
        
    # በየ 15 ደቂቃው (900 ሰከንድ) እንዲሮጥ ማዘዝ
    # ቦቱን እንዳስነሳኸው ወዲያውኑ ፍተሻ እንዲጀምር first=5 (ከ5 ሰከንድ በኋላ) ተደርጓል
    context.job_queue.run_repeating(check_website_job, interval=900, first=5, chat_id=chat_id, name=str(chat_id))
    
    await update.message.reply_text("🚀 የቴክ24 ክትትል በቦቱ ላይ በስኬት ተጀምሯል! በየ 15 ደቂቃው ዌብሳይቱን እየገባሁ እፈትሻለሁ።")

# /stop ሲባል ክትትሉን የሚያቆም
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    
    if not current_jobs:
        await update.message.reply_text("የቆመ ክཏትል የለም።")
        return
        
    for job in current_jobs:
        job.schedule_removal()
        
    await update.message.reply_text("🛑 የቴክ24 ክትትል በጊዜያዊነት ቆሟል።")

import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()





def main():
    # ቦቱን ማስነሳት
    threading.Thread(target=run_health_server, daemon=True).start()
    application = Application.builder().token(BOT_TOKEN).build()

    # የቴሌግራም ትዕዛዞችን (Commands) መመዝገብ
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))

    # ቦቱን በቋሚነት ማሰራት (Render ላይ ሳይቋረጥ እንዲሰራ)
    application.run_polling()


    if __name__ == '__main__':
       main()
