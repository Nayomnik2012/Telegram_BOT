import logging
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                    level=logging.INFO,
                    filename='bot.log'
                    )

def start_bot(bot, update):
    mytext = "Hello {}! \nYou are ready?".format(update.message.chat.first_name)
    update.message.reply_text(mytext)

def chat(bot, update):
    update.message.reply_text('Hello!')

def main():
    updtr = Updater('717979594:AAG4mWBvGaSIWpRb303ui9nBFl8px5u8QIw')
    updtr.dispatcher.add_handler(CommandHandler("start", start_bot))
    updtr.dispatcher.add_handler(MessageHandler(Filters.text, chat))
    updtr.start_polling()
    updtr.idle()

if __name__ == "__main__":
    logging.info('Bot started!')
    main()


