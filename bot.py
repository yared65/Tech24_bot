import os
import logging
import asyncio
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, ContextTypes
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# Load environment variables
#load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
EMAIL = os.getenv('EMAIL')
PASSWORD = os.getenv('PASSWORD')

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# Global session to maintain cookies across requests
session = httpx.AsyncClient(follow_redirects=True)

async def check_website_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    login_data = {'email': EMAIL, 'password': PASSWORD, 'submit': 'Login'}
    
    try:
        # 1. Login and fetch dashboard in one session
        await session.post('https://tech24et.com/client/index.php', data=login_data)
        response = await session.get('https://tech24et.com/client/dashboard.php')
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            if "ያሬድ ግርማ" in soup.get_text() or "Yared Girma" in soup.get_text():
                await context.bot.send_message(chat_id=chat_id, text="🔔 አዲስ የ ATM ስራ ተገኝቷል! ፈጥነህ እይ!")
            else:
                logging.info("ፍተሻ ተጠናቋል፡ ምንም አዲስ መረጃ የለም።")
        else:
            logging.error(f"Failed to access dashboard: {response.status_code}")
            
    except Exception as e:
        logging.error(f"Error occurred: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Clear existing jobs
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    
    context.job_queue.run_repeating(check_website_job, interval=900, first=5, chat_id=chat_id, name=str(chat_id))
    await update.message.reply_text("✅ ክትትል ተጀምሯል።")

# Simple Health Server for Render
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"Bot is alive")

def run_health_server():
    server = HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 10000))), HealthCheckHandler)
    server.serve_forever()
    print(f"DEBUG: BOT_TOKEN is {os.getenv('BOT_TOKEN')}")
 
def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.run_polling()

if __name__ == '__main__':
    main()
