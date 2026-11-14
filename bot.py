import telebot
import json
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

TOKEN = "9108041844:AAEpdJrO-udnMU32MHxB6KUOClCZFJZ2vIw"
WEB_APP_URL = "https://kurmanbek18.github.io/prophet_arena_demo_bot/"

bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    # Обработка реферальных ссылок (например: /start ref_12345)
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_id = args[1].split("_")[1]
        # Здесь можно сохранить реферала в базу данных
        print(f"Пользователь {message.from_user.id} пришел по рефке от {referrer_id}")

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(text="🚀 ЗАПУСТИТЬ PREDICTION ARENA", web_app=WebAppInfo(url=WEB_APP_URL)),
        InlineKeyboardButton(text="📢 Официальный канал", url="https://t.me/durov"), # Замени на свой канал
        InlineKeyboardButton(text="💬 Чат сообщества", url="https://t.me/durov")
    )
    
    welcome_text = (
        f"<b>Добро пожаловать в Prediction Arena, {message.from_user.first_name}!</b> 🔥\n\n"
        "📈 Делай точные прогнозы на крипту и технологии.\n"
        "⚡️ Майни монеты в квантовом ядре.\n"
        "👥 Приглашай друзей и получай пассивный доход в PTS.\n\n"
        "Жми кнопку ниже и зарабатывай прямо сейчас!"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=kb, parse_mode="HTML")

@bot.message_handler(content_types=['web_app_data'])
def handle_web_app_data(message):
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get('action')

        if action == 'bet':
            choice = data.get('choice')
            amount = data.get('amount')
            bot.send_message(
                message.chat.id, 
                f"✅ Ставка зарегистрирована!\n🎯 Выбор: <b>{choice}</b>\n💰 Сумма: <b>{amount} PTS</b>", 
                parse_mode="HTML"
            )
        
        elif action == 'buy_boost':
            bot.send_message(
                message.chat.id, 
                "⭐️ Запрос на покупку ускорителя через <b>Telegram Stars</b> принят в обработку!", 
                parse_mode="HTML"
            )

    except Exception as e:
        bot.send_message(message.chat.id, "⚠️ Ошибка синхронизации данных с сервером.")

print("🚀 Бот Prediction Arena запущен и готов к взрывному росту!")
bot.polling(none_stop=True)
