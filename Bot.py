from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

API_TOKEN = '513563991:AAHWLkZJ7muloN4dkanbcgPZmWb4KjJ2omg'
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)


@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    # Создаем кнопки
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = ["💡 Керченская 11", "📋 График", "❓ Помощь"]
    keyboard.add(*buttons)

    await message.answer("Выберите действие из меню:", reply_markup=keyboard)


if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)