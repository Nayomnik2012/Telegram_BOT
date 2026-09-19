"""Telegram-бот «Кредитный Журнал».

Возможности:
  * белый список пользователей (остальные игнорируются);
  * админ-команды /allow /deny /users;
  * карточка проекта с таблицей «QA проектов» (/project);
  * меню с кнопками (/menu), приветствие при добавлении в группу;
  * кнопка «Написать @user» подставляет адресата в поле ввода той темы,
    где вызвали бота (нужен inline-режим в @BotFather: /setinline);
  * мини HTTP-сервер для Render (порт из PORT) — чтобы пингер не давал
    бесплатному сервису засыпать.

Переменные окружения:
  BOT_TOKEN          токен от @BotFather                     (обязательно)
  ADMIN_IDS          Telegram ID админов через запятую       (обязательно)
  ALLOWED_USER_IDS   постоянный белый список через запятую   (необязательно)
  DB_PATH            путь к SQLite-файлу, по умолчанию bot.db
  PORT               порт health-сервера, по умолчанию 10000
  PROXY_URL          прокси для корпоративной сети           (необязательно)
"""
import html
import logging
import os
import sqlite3
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    TypeHandler,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # не светим токен в логах
log = logging.getLogger("bot")


# =============================================================================
# НАСТРОЙКИ
# =============================================================================
def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "").replace(" ", "")
    return {int(x) for x in raw.split(",") if x.isdigit()}


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = _ids("ADMIN_IDS")
STATIC_ALLOWED = _ids("ALLOWED_USER_IDS")
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "10000"))
PROXY_URL = os.getenv("PROXY_URL") or None

# True  — кнопка «Написать» подставляет @username в поле ввода текущей темы
#         (требует /setinline в @BotFather);
# False — кнопка просто открывает личный чат с сотрудником.
WRITE_VIA_INLINE = True


# =============================================================================
# ДАННЫЕ КАРТОЧКИ ПРОЕКТА  (редактируйте здесь)
# =============================================================================
TITLE = "📁 Кредитный Журнал"

PRODUCTS = [
    "Товарный кредит",
    "БД Лимиты",
    "Кеш на карту",
    "Кредитный лимит на карту: агрегированный",
    "КЕШ кредит",
    "Смена лимита на карте",
    "Оплата Частями",
    "Обработка Лида",
    "Гарантии",
    "Микро кредиты",
]

# Каждая строка таблицы — три независимых поля: name / position / description.
# username нужен только для кнопки «Написать» (в самой таблице его нет).
TEAM = [
    {
        "name": "Alexandr Kovalsky",
        "username": "Alexandr_Kovalsky",
        "position": "Старший QA",
        "description": "Тестирование продуктов КЖ",  # <- замените на свой текст
    },
    # {
    #     "name": "Имя Фамилия",
    #     "username": "username",
    #     "position": "QA",
    #     "description": "Чем занимается",
    # },
]

HEADERS = ("Ответственный", "Должность", "Описание")
# Максимальная ширина колонок в символах (длинный текст переносится).
# Сумма + 6 должна быть около 50, иначе таблица не влезет в пузырь на телефоне.
MAX_WIDTHS = (14, 12, 18)


# =============================================================================
# БЕЛЫЙ СПИСОК
# =============================================================================
class Whitelist:
    """Админы (ADMIN_IDS) + постоянный список (ALLOWED_USER_IDS) + SQLite."""

    def __init__(self, path: str, admins: set[int], static: set[int]):
        self.admins = set(admins)
        self.static = set(static)
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS allowed (user_id INTEGER PRIMARY KEY, note TEXT)"
        )
        self.db.commit()
        self._cache = {r[0] for r in self.db.execute("SELECT user_id FROM allowed")}

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self.admins or user_id in self.static or user_id in self._cache

    def add(self, user_id: int, note: str = "") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO allowed (user_id, note) VALUES (?, ?)", (user_id, note)
        )
        self.db.commit()
        self._cache.add(user_id)

    def remove(self, user_id: int) -> bool:
        cur = self.db.execute("DELETE FROM allowed WHERE user_id = ?", (user_id,))
        self.db.commit()
        self._cache.discard(user_id)
        return cur.rowcount > 0

    def rows(self) -> list[tuple[int, str]]:
        return list(self.db.execute("SELECT user_id, note FROM allowed ORDER BY user_id"))


# =============================================================================
# ТАБЛИЦА И КАРТОЧКА
# =============================================================================
def build_table(rows: list[dict]) -> str:
    data = [HEADERS] + [(r["name"], r["position"], r["description"]) for r in rows]

    widths = []
    for i in range(3):
        longest = max(len(row[i]) for row in data)
        widths.append(min(longest, MAX_WIDTHS[i]))

    def fmt_row(cells) -> list[str]:
        wrapped = [textwrap.wrap(c, widths[i]) or [""] for i, c in enumerate(cells)]
        height = max(len(w) for w in wrapped)
        lines = []
        for k in range(height):
            parts = [
                (wrapped[i][k] if k < len(wrapped[i]) else "").ljust(widths[i])
                for i in range(3)
            ]
            lines.append(" │ ".join(parts).rstrip())
        return lines

    sep = "─┼─".join("─" * w for w in widths)

    out = fmt_row(HEADERS) + [sep]
    for n, row in enumerate(data[1:]):
        out += fmt_row(row)
        if n < len(data) - 2:
            out.append(sep)
    return "\n".join(out)


def card_text() -> str:
    products = "\n".join(f"- {html.escape(p)};" for p in PRODUCTS)
    products = products[:-1] + "."
    table = html.escape(build_table(TEAM))
    return (
        f"<b>{html.escape(TITLE)}</b>\n\n"
        f"{products}\n\n"
        f"<b>👥 QA проектов:</b>\n"
        f"<pre>{table}</pre>"
    )


def card_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for m in TEAM:
        user = m.get("username")
        if not user:
            continue
        label = f"💬 Написать @{user}"
        if WRITE_VIA_INLINE:
            btn = InlineKeyboardButton(label, switch_inline_query_current_chat=f"@{user} ")
        else:
            btn = InlineKeyboardButton(label, url=f"https://t.me/{user}")
        rows.append([btn])
    return InlineKeyboardMarkup(rows)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📁 Карточка проекта", callback_data="menu:project")],
            [
                InlineKeyboardButton("🆔 Мой ID", callback_data="menu:id"),
                InlineKeyboardButton("ℹ️ Помощь", callback_data="menu:help"),
            ],
        ]
    )


HELP_TEXT = (
    "Команды:\n"
    "/project — карточка проекта и QA\n"
    "/menu — меню\n"
    "/id — показать мой ID"
)
ADMIN_HELP = (
    "\n\nДля админов:\n"
    "/allow <user_id> [заметка] — дать доступ\n"
    "/deny <user_id> — забрать доступ\n"
    "/users — список доступа"
)


# =============================================================================
# ДОСТУП (выполняется до всех остальных обработчиков)
# =============================================================================
async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wl: Whitelist = context.bot_data["wl"]

    # --- бота добавили в группу / убрали из неё -------------------------------
    mcm = update.my_chat_member
    if mcm:
        chat = mcm.chat
        joined = mcm.new_chat_member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
        ) and mcm.old_chat_member.status not in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
        )
        if joined and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            if wl.is_allowed(mcm.from_user.id):
                await context.bot.send_message(
                    chat.id, "Бот подключён. Выберите действие:", reply_markup=main_menu()
                )
            else:
                log.warning("Добавлен в чужую группу %s (%s), выхожу", chat.id, chat.title)
                await context.bot.leave_chat(chat.id)
        raise ApplicationHandlerStop

    # --- обычные обновления ------------------------------------------------------
    user = update.effective_user
    if user is None:
        raise ApplicationHandlerStop

    msg = update.effective_message
    if msg and msg.text:
        first = msg.text.split()[0].split("@")[0].lower()
        if first == "/id":  # /id доступен всем — чтобы человек мог узнать свой ID
            return

    if not wl.is_allowed(user.id):
        log.warning("Доступ запрещён: user_id=%s username=%s", user.id, user.username)
        raise ApplicationHandlerStop


# =============================================================================
# ОБЫЧНЫЕ КОМАНДЫ
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Привет! Доступ подтверждён.\n\n" + HELP_TEXT, reply_markup=main_menu()
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = HELP_TEXT
    if update.effective_user.id in ADMIN_IDS:
        text += ADMIN_HELP
    await update.effective_message.reply_text(text)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"user_id: {update.effective_user.id}\nchat_id: {update.effective_chat.id}"
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Меню:", reply_markup=main_menu())


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        card_text(), parse_mode=ParseMode.HTML, reply_markup=card_keyboard()
    )


async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    action = (q.data or "").split(":", 1)[-1]
    if action == "project":
        await q.message.reply_text(
            card_text(), parse_mode=ParseMode.HTML, reply_markup=card_keyboard()
        )
    elif action == "id":
        await q.message.reply_text(
            f"user_id: {q.from_user.id}\nchat_id: {q.message.chat.id}"
        )
    elif action == "help":
        text = HELP_TEXT + (ADMIN_HELP if q.from_user.id in ADMIN_IDS else "")
        await q.message.reply_text(text)


async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подсказка «Отправить: @user текст» для кнопки «Написать»."""
    q = update.inline_query
    text = q.query.strip()
    if not text.startswith("@"):
        await q.answer([], cache_time=0, is_personal=True)
        return
    result = InlineQueryResultArticle(
        id="msg",
        title=f"Отправить: {text[:60]}",
        description="Допишите сообщение и нажмите сюда",
        input_message_content=InputTextMessageContent(text),
    )
    await q.answer([result], cache_time=0, is_personal=True)


# =============================================================================
# АДМИН-КОМАНДЫ
# =============================================================================
def _is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS


async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.effective_message.reply_text("Использование: /allow <user_id> [заметка]")
        return
    wl: Whitelist = context.bot_data["wl"]
    wl.add(int(args[0]), " ".join(args[1:]))
    text = f"Добавлен: {args[0]}"
    if os.getenv("RENDER") and not os.getenv("DB_PERSISTENT"):
        text += (
            "\n\n⚠️ На бесплатном Render файл bot.db сбрасывается при перезапуске. "
            "Для постоянного доступа добавьте ID в переменную ALLOWED_USER_IDS."
        )
    await update.effective_message.reply_text(text)


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.effective_message.reply_text("Использование: /deny <user_id>")
        return
    wl: Whitelist = context.bot_data["wl"]
    uid = int(args[0])
    if wl.remove(uid):
        text = "Удалён"
        if uid in wl.static:
            text += "\n⚠️ Этот ID также прописан в ALLOWED_USER_IDS — уберите его и оттуда."
    else:
        text = "Такого ID нет в списке bot.db"
        if uid in wl.static:
            text += "\nОн прописан в ALLOWED_USER_IDS — уберите его в настройках сервиса."
    await update.effective_message.reply_text(text)


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        return
    wl: Whitelist = context.bot_data["wl"]
    dynamic = "\n".join(f"{uid}  {note}".rstrip() for uid, note in wl.rows()) or "—"
    await update.effective_message.reply_text(
        f"Админы: {sorted(wl.admins)}\n\n"
        f"ALLOWED_USER_IDS: {sorted(wl.static) or '—'}\n\n"
        f"Добавлены командой (bot.db):\n{dynamic}"
    )


# =============================================================================
# HEALTH-СЕРВЕР ДЛЯ RENDER
# =============================================================================
class _Health(BaseHTTPRequestHandler):
    def _ok(self, body: bytes = b"") -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._ok(b"ok")

    def do_HEAD(self) -> None:  # noqa: N802  (UptimeRobot по умолчанию шлёт HEAD)
        self._ok()

    def log_message(self, *args) -> None:  # не засоряем логи пингами
        pass


def start_health_server() -> None:
    server = HTTPServer(("0.0.0.0", PORT), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health-сервер слушает порт %s", PORT)


# =============================================================================
# ЗАПУСК
# =============================================================================
async def post_init(app: Application) -> None:
    user_cmds = [
        BotCommand("start", "Начать работу"),
        BotCommand("menu", "Показать меню"),
        BotCommand("project", "Карточка проекта и QA"),
        BotCommand("id", "Показать мой ID"),
    ]
    admin_cmds = user_cmds + [
        BotCommand("allow", "Добавить пользователя"),
        BotCommand("deny", "Удалить пользователя"),
        BotCommand("users", "Список доступа"),
    ]
    await app.bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
    await app.bot.set_my_commands(user_cmds, scope=BotCommandScopeAllGroupChats())
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(admin_id))
        except Exception as e:  # админ ещё не писал боту в личку
            log.warning("Команды для админа %s не заданы: %s", admin_id, e)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Задайте переменную окружения BOT_TOKEN.")
    if not ADMIN_IDS:
        raise SystemExit("Задайте переменную окружения ADMIN_IDS (Telegram ID администратора).")

    wl = Whitelist(DB_PATH, ADMIN_IDS, STATIC_ALLOWED)
    start_health_server()

    builder = Application.builder().token(BOT_TOKEN).post_init(post_init)
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
    app = builder.build()
    app.bot_data["wl"] = wl

    # Проверка доступа — первой (group=-1), дальше пройдут только свои
    app.add_handler(TypeHandler(Update, access_guard), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("allow", cmd_allow))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    app.add_handler(InlineQueryHandler(on_inline))

    log.info("Бот запущен. БД: %s. Админы: %s", DB_PATH, sorted(ADMIN_IDS))
    app.run_polling(
        allowed_updates=["message", "callback_query", "inline_query", "my_chat_member"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
