import os
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiohttp import web
import asyncio

API_TOKEN = os.environ.get('BOT_TOKEN')
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)


# --- Блок для Render ---
async def handle(request):
    return web.Response(text="Bot is running!")


async def start_health_check():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    # Берем порт, который дает Render, или 8080 по умолчанию
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()


# -----------------------

@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    # По вашему запросу: проверка только для Керченская 11
    buttons = ["💡 Керченская 11", "📋 График", "❓ Помощь"]
    keyboard.add(*buttons)
    await message.answer("Выберите действие из меню:", reply_markup=keyboard)


if __name__ == '__main__':
    # Запускаем "пустой" сервер для проверки Render
    loop = asyncio.get_event_loop()
    loop.create_task(start_health_check())

    # Запускаем бота
    executor.start_polling(dp, skip_updates=True)