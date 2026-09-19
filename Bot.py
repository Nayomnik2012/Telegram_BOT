"""
Telegram-бот: белый список, заявки на доступ, проекты и команда проекта.1

Переменные окружения (Render -> Environment):
    BOT_TOKEN   - токен от @BotFather                       (обязательно)
    ADMIN_IDS   - Telegram ID администратора(ов) через запятую (обязательно)
    MENU_IMAGE  - URL картинки для меню                      (необязательно)
    DB_PATH     - путь к файлу базы, например /data/bot.db    (необязательно)

Картинку меню можно также задать прямо в боте: Управление -> Картинка меню,
либо положить файл menu.jpg рядом с bot.py.
"""

import html
import logging
import os
import re
import sqlite3
import textwrap
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ───────────────────────── Конфигурация ─────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
MENU_IMAGE = os.environ.get("MENU_IMAGE", "").strip()
LOCAL_IMAGE = Path(__file__).with_name("menu.jpg")
DB_PATH = os.environ.get("DB_PATH") or ("/data/bot.db" if Path("/data").is_dir() else "bot.db")

# Keep-alive: бот сам обращается к своему публичному URL, чтобы бесплатный сервис
# Render не засыпал (засыпает через 15 минут без входящих HTTP-запросов).
# Render сам задаёт RENDER_EXTERNAL_URL; при желании можно указать свой KEEPALIVE_URL.
KEEPALIVE_URL = os.environ.get("KEEPALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
KEEPALIVE_INTERVAL = 300  # секунд (5 минут)

# Ширина колонок таблицы «Ответственные» в символах: Ответственный / Должность / Описание.
# Длинный текст переносится на следующую строку. Сумма + 6 — около 50,
# иначе таблица не поместится в сообщение на телефоне.
TABLE_WIDTHS = (18, 12, 16)

HTML = ParseMode.HTML
esc = html.escape

# ───────────────────────── База данных ─────────────────────────

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys = ON")
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users(
        user_id   INTEGER PRIMARY KEY,
        username  TEXT,
        full_name TEXT,
        role      TEXT NOT NULL DEFAULT 'user'          -- user | deputy
    );
    CREATE TABLE IF NOT EXISTS requests(
        user_id   INTEGER PRIMARY KEY,
        username  TEXT,
        full_name TEXT,
        status    TEXT NOT NULL                          -- pending | approved | rejected
    );
    CREATE TABLE IF NOT EXISTS notifications(
        user_id    INTEGER,
        chat_id    INTEGER,
        message_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS projects(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS members(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        username    TEXT NOT NULL,
        position    TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS settings(
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """
)
db.commit()

# Миграция: колонка «Описание» для уже существующей базы (старые данные сохраняются)
if "description" not in {r["name"] for r in db.execute("PRAGMA table_info(members)")}:
    db.execute("ALTER TABLE members ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    db.commit()


def run(sql: str, *args) -> sqlite3.Cursor:
    cur = db.execute(sql, args)
    db.commit()
    return cur


def one(sql: str, *args):
    return db.execute(sql, args).fetchone()


def many(sql: str, *args):
    return db.execute(sql, args).fetchall()


# ───────────────────────── Роли ─────────────────────────

MANAGERS = ("admin", "deputy")


def get_role(uid: int) -> str:
    if uid in ADMIN_IDS:
        return "admin"
    row = one("SELECT role FROM users WHERE user_id=?", uid)
    return row["role"] if row else "guest"


def manager_ids() -> set[int]:
    return ADMIN_IDS | {r["user_id"] for r in many("SELECT user_id FROM users WHERE role='deputy'")}


# ───────────────────────── Отрисовка экранов ─────────────────────────


def menu_image():
    row = one("SELECT value FROM settings WHERE key='menu_image'")
    if row and row["value"]:
        return row["value"]  # file_id, заданный админом в боте
    if MENU_IMAGE:
        return MENU_IMAGE
    if LOCAL_IMAGE.exists():
        return LOCAL_IMAGE.read_bytes()
    return None


async def render(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    kb: Markup | None = None,
    *,
    photo: bool = False,
    replace: Message | None = None,
) -> None:
    """Показывает экран: редактирует старое сообщение или удаляет его и шлёт новое."""
    if replace is not None:
        if not photo and not replace.photo:
            try:
                await replace.edit_text(text, reply_markup=kb, parse_mode=HTML)
                return
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
        try:
            await replace.delete()
        except TelegramError:
            pass

    if photo:
        src = menu_image()
        if src:
            try:
                await context.bot.send_photo(
                    chat_id, src, caption=text, reply_markup=kb, parse_mode=HTML
                )
                return
            except TelegramError as e:
                log.warning("Не удалось отправить картинку меню: %s", e)
    await context.bot.send_message(chat_id, text, reply_markup=kb, parse_mode=HTML)


async def show_menu(context, chat_id: int, uid: int, replace: Message | None = None):
    role = get_role(uid)
    if role == "guest":
        req = one("SELECT status FROM requests WHERE user_id=?", uid)
        if req and req["status"] == "pending":
            rows = [[Btn("⏳ Запрос отправлен", callback_data="req")]]
        else:
            rows = [[Btn("🔑 Запросить доступ", callback_data="req")]]
        text = (
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Доступ к боту ограничен. Нажмите кнопку ниже, чтобы запросить доступ."
        )
    else:
        rows = [[Btn("📁 Проекты", callback_data="prj:list")]]
        if role in MANAGERS:
            rows.append([Btn("⚙️ Управление", callback_data="adm")])
        text = "🏠 <b>Главное меню</b>"
    await render(context, chat_id, text, Markup(rows), photo=True, replace=replace)


async def show_panel(context, chat_id: int, uid: int, replace: Message | None = None):
    role = get_role(uid)
    rows = [
        [Btn("➕ Добавить проект", callback_data="prj_add")],
        [Btn("👥 Пользователи", callback_data="usr")],
    ]
    if role == "admin":
        rows.append([Btn("🛡 Заместители", callback_data="dep")])
        rows.append([Btn("🖼 Картинка меню", callback_data="setimg")])
    rows.append([Btn("⬅️ Меню", callback_data="menu")])
    await render(context, chat_id, "⚙️ <b>Управление</b>", Markup(rows), replace=replace)


async def show_projects(context, chat_id: int, uid: int, replace: Message | None = None):
    projects = many("SELECT id, name FROM projects ORDER BY name COLLATE NOCASE")
    rows = [[Btn(f"📁 {p['name']}", callback_data=f"prj:{p['id']}")] for p in projects]
    if get_role(uid) in MANAGERS:
        rows.append([Btn("➕ Добавить проект", callback_data="prj_add")])
    rows.append([Btn("⬅️ Меню", callback_data="menu")])
    text = "📁 <b>Проекты</b>" if projects else "📁 Проектов пока нет."
    await render(context, chat_id, text, Markup(rows), replace=replace)


def members_table(members) -> str:
    """Таблица «Ответственный / Должность / Описание» в моноширинном блоке."""
    headers = ("Ответственный", "Должность", "Описание")
    data = [headers] + [
        (f"@{m['username']}", m["position"] or "—", m["description"] or "—") for m in members
    ]
    widths = [min(max(len(row[i]) for row in data), TABLE_WIDTHS[i]) for i in range(3)]

    def fmt_row(cells) -> list[str]:
        wrapped = [textwrap.wrap(c, widths[i]) or [""] for i, c in enumerate(cells)]
        height = max(len(w) for w in wrapped)
        return [
            " │ ".join(
                (wrapped[i][k] if k < len(wrapped[i]) else "").ljust(widths[i])
                for i in range(3)
            ).rstrip()
            for k in range(height)
        ]

    sep = "─┼─".join("─" * w for w in widths)
    out = fmt_row(headers) + [sep]
    for n, row in enumerate(data[1:]):
        out += fmt_row(row)
        if n < len(data) - 2:
            out.append(sep)
    return "\n".join(out)


async def show_project(context, chat_id: int, uid: int, pid: int, replace: Message | None = None):
    p = one("SELECT * FROM projects WHERE id=?", pid)
    if not p:
        return await show_projects(context, chat_id, uid, replace)
    members = many("SELECT * FROM members WHERE project_id=? ORDER BY id", pid)

    lines = [f"📁 <b>{esc(p['name'])}</b>", ""]
    lines.append(esc(p["description"]) if p["description"] else "<i>Описание пока не добавлено</i>")
    if members:
        lines += ["", "👥 <b>Ответственные:</b>", f"<pre>{esc(members_table(members))}</pre>"]

    # URL-кнопка открывает профиль/чат с пользователем
    rows = [
        [Btn(f"💬 Написать @{m['username']}", url=f"https://t.me/{m['username']}")]
        for m in members
    ]
    if get_role(uid) in MANAGERS:
        rows += [
            [
                Btn("✏️ Название", callback_data=f"prj_name:{pid}"),
                Btn("✏️ Описание", callback_data=f"prj_desc:{pid}"),
            ],
            [
                Btn("➕ Участник", callback_data=f"prj_addm:{pid}"),
                Btn("✏️ Участник", callback_data=f"prj_edm:{pid}"),
                Btn("➖ Участник", callback_data=f"prj_delm:{pid}"),
            ],
            [Btn("🗑 Удалить проект", callback_data=f"prj_del:{pid}")],
        ]
    rows.append([Btn("⬅️ Проекты", callback_data="prj:list"), Btn("🏠 Меню", callback_data="menu")])
    await render(context, chat_id, "\n".join(lines), Markup(rows), replace=replace)


async def show_users(context, chat_id: int, uid: int, replace: Message | None = None):
    is_admin = get_role(uid) == "admin"
    users = many(
        "SELECT * FROM users " + ("" if is_admin else "WHERE role='user' ") + "ORDER BY full_name COLLATE NOCASE"
    )
    rows = []
    for u in users:
        label = f"❌ {u['full_name'] or u['user_id']}"
        if u["username"]:
            label += f" (@{u['username']})"
        if u["role"] == "deputy":
            label += " ⭐"
        rows.append([Btn(label[:60], callback_data=f"usr_del:{u['user_id']}")])
    rows.append([Btn("⬅️ Назад", callback_data="adm")])
    text = "👥 <b>Пользователи с доступом</b>\nНажмите на пользователя, чтобы отозвать доступ."
    if not users:
        text = "👥 Пользователей с доступом пока нет."
    await render(context, chat_id, text, Markup(rows), replace=replace)


async def show_deputies(context, chat_id: int, replace: Message | None = None):
    users = many("SELECT * FROM users ORDER BY role DESC, full_name COLLATE NOCASE")
    rows = []
    for u in users:
        name = u["full_name"] or str(u["user_id"])
        if u["role"] == "deputy":
            label = f"🔻 Снять: {name}"
        else:
            label = f"⭐ Назначить: {name}"
        rows.append([Btn(label[:60], callback_data=f"dep_t:{u['user_id']}")])
    rows.append([Btn("⬅️ Назад", callback_data="adm")])
    text = (
        "🛡 <b>Заместители</b>\n"
        "Заместителем можно назначить любого пользователя из белого списка."
    )
    if not users:
        text = "🛡 В белом списке пока никого нет — сначала одобрите чью-нибудь заявку."
    await render(context, chat_id, text, Markup(rows), replace=replace)


def confirm_kb(yes: str, no: str) -> Markup:
    return Markup([[Btn("✅ Да", callback_data=yes), Btn("✖️ Нет", callback_data=no)]])


async def ask(context, chat_id: int, msg: Message, text: str, state: tuple):
    """Просит админа/зама прислать текст и запоминает, что именно ждём."""
    context.user_data["state"] = state
    kb = Markup([[Btn("✖️ Отмена", callback_data="menu")]])
    await render(context, chat_id, text, kb, replace=msg)


# ───────────────────────── Заявки на доступ ─────────────────────────


USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")
LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/([A-Za-z0-9_]{5,32})/?(?:\?\S*)?", re.I
)


def extract_username(token: str) -> str | None:
    """Принимает @nick, nick или ссылку t.me/nick и возвращает username без @."""
    token = token.strip()
    m = LINK_RE.fullmatch(token)
    if m:
        return m.group(1)
    token = token.lstrip("@")
    return token if USERNAME_RE.fullmatch(token) else None


def user_link(uid: int, full_name: str) -> str:
    return f'<a href="tg://user?id={uid}">{esc(full_name)}</a>'


async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user

    if get_role(user.id) != "guest":
        await q.answer("У вас уже есть доступ")
        return await show_menu(context, q.message.chat_id, user.id, replace=q.message)

    req = one("SELECT status FROM requests WHERE user_id=?", user.id)
    if req and req["status"] == "pending":
        await q.answer("Запрос уже отправлен, ожидайте решения.", show_alert=True)
        return

    run(
        """INSERT INTO requests(user_id, username, full_name, status) VALUES(?,?,?, 'pending')
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
               full_name=excluded.full_name, status='pending'""",
        user.id, user.username, user.full_name,
    )
    run("DELETE FROM notifications WHERE user_id=?", user.id)

    uname = f"@{user.username}" if user.username else "нет username"
    text = (
        "🔔 <b>Запрос доступа к боту</b>\n\n"
        f"Пользователь: {user_link(user.id, user.full_name)}\n"
        f"Username: {esc(uname)}\n"
        f"ID: <code>{user.id}</code>"
    )
    kb = Markup(
        [[
            Btn("✅ Одобрить", callback_data=f"dec:{user.id}:1"),
            Btn("❌ Отказать", callback_data=f"dec:{user.id}:0"),
        ]]
    )
    delivered = 0
    for rid in manager_ids():
        try:
            m = await context.bot.send_message(rid, text, reply_markup=kb, parse_mode=HTML)
            run("INSERT INTO notifications VALUES(?,?,?)", user.id, m.chat_id, m.message_id)
            delivered += 1
        except TelegramError as e:
            log.warning("Не удалось уведомить %s: %s (админ должен нажать /start у бота)", rid, e)

    await q.answer("Запрос отправлен ✅" if delivered else "Запрос сохранён, ожидайте решения.")
    await show_menu(context, q.message.chat_id, user.id, replace=q.message)


async def decide(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, approve: bool):
    q = update.callback_query
    actor = q.from_user

    status = "approved" if approve else "rejected"
    cur = run("UPDATE requests SET status=? WHERE user_id=? AND status='pending'", status, uid)
    if cur.rowcount == 0:  # уже обработано другим администратором/замом
        await q.answer("Заявка уже обработана.", show_alert=True)
        try:
            await q.message.edit_reply_markup(None)
        except TelegramError:
            pass
        return

    req = one("SELECT * FROM requests WHERE user_id=?", uid)
    if approve:
        run(
            """INSERT INTO users(user_id, username, full_name, role) VALUES(?,?,?, 'user')
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                   full_name=excluded.full_name""",
            uid, req["username"], req["full_name"],
        )

    await q.answer("Готово")

    # Обновляем уведомления у всех админов/замов, чтобы кнопки не оставались активными
    verdict = "✅ Одобрено" if approve else "❌ Отказано"
    result = (
        f"{verdict}: {user_link(uid, req['full_name'] or str(uid))}\n"
        f"Решение принял(а): {esc(actor.full_name)}"
    )
    for n in many("SELECT chat_id, message_id FROM notifications WHERE user_id=?", uid):
        try:
            await context.bot.edit_message_text(
                result, chat_id=n["chat_id"], message_id=n["message_id"], parse_mode=HTML
            )
        except TelegramError:
            pass
    run("DELETE FROM notifications WHERE user_id=?", uid)

    # Сообщение пользователю
    try:
        await context.bot.send_message(uid, "Доступ одобрен" if approve else "В доступе отказано")
        if approve:
            await show_menu(context, uid, uid)
    except TelegramError as e:
        log.warning("Не удалось написать пользователю %s: %s", uid, e)


# ───────────────────────── Обработчики ─────────────────────────

MANAGER_CMDS = {
    "dec", "adm", "prj_add", "prj_name", "prj_desc", "prj_addm", "prj_delm",
    "prj_edm", "mem_pick", "mem_fld",
    "mdel", "prj_del", "prj_del_yes", "usr", "usr_del", "usr_del_yes",
}
ADMIN_CMDS = {"dep", "dep_t", "setimg"}


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    u = update.effective_user
    run("UPDATE users SET username=?, full_name=? WHERE user_id=?", u.username, u.full_name, u.id)
    await show_menu(context, update.effective_chat.id, u.id)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    await show_menu(context, update.effective_chat.id, update.effective_user.id)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    msg = q.message
    chat_id = msg.chat_id
    role = get_role(user.id)
    cmd, *a = (q.data or "").split(":")
    context.user_data.pop("state", None)  # любая кнопка сбрасывает ожидание текста

    if cmd == "req":
        return await request_access(update, context)

    if role == "guest":
        return await q.answer("⛔ Нет доступа", show_alert=True)
    if (cmd in MANAGER_CMDS and role not in MANAGERS) or (cmd in ADMIN_CMDS and role != "admin"):
        return await q.answer("⛔ Недостаточно прав", show_alert=True)

    if cmd == "dec":
        return await decide(update, context, int(a[0]), a[1] == "1")

    await q.answer()

    if cmd == "menu":
        await show_menu(context, chat_id, user.id, replace=msg)
    elif cmd == "prj":
        if a[0] == "list":
            await show_projects(context, chat_id, user.id, replace=msg)
        else:
            await show_project(context, chat_id, user.id, int(a[0]), replace=msg)
    elif cmd == "adm":
        await show_panel(context, chat_id, user.id, replace=msg)

    # ── проекты ──
    elif cmd == "prj_add":
        await ask(context, chat_id, msg, "Введите <b>название</b> нового проекта:", ("add_prj_name",))
    elif cmd == "prj_name":
        await ask(context, chat_id, msg, "Введите <b>новое название</b> проекта:", ("edit_name", int(a[0])))
    elif cmd == "prj_desc":
        await ask(context, chat_id, msg, "Отправьте <b>новое описание</b> проекта:", ("edit_desc", int(a[0])))

    # ── ответственные (таблица) ──
    elif cmd == "prj_addm":
        await ask(
            context, chat_id, msg,
            "Введите <b>Ответственного</b>: @username или ссылку t.me/username\n"
            "Должность и описание бот спросит на следующих шагах.",
            ("add_member", int(a[0])),
        )
    elif cmd == "prj_edm":
        pid = int(a[0])
        members = many("SELECT * FROM members WHERE project_id=? ORDER BY id", pid)
        rows = [[Btn(f"✏️ @{m['username']}", callback_data=f"mem_pick:{m['id']}:{pid}")] for m in members]
        rows.append([Btn("⬅️ Назад", callback_data=f"prj:{pid}")])
        await render(
            context, chat_id,
            "Чьи данные изменить?" if members else "В проекте пока нет ответственных.",
            Markup(rows), replace=msg,
        )
    elif cmd == "mem_pick":
        mid, pid = int(a[0]), int(a[1])
        m = one("SELECT * FROM members WHERE id=?", mid)
        if not m:
            return await show_project(context, chat_id, user.id, pid, replace=msg)
        rows = [
            [Btn("👤 Ответственный", callback_data=f"mem_fld:{mid}:{pid}:username")],
            [Btn("💼 Должность", callback_data=f"mem_fld:{mid}:{pid}:position")],
            [Btn("📝 Описание", callback_data=f"mem_fld:{mid}:{pid}:description")],
            [Btn("⬅️ Назад", callback_data=f"prj_edm:{pid}")],
        ]
        await render(
            context, chat_id, f"Что изменить у <b>@{esc(m['username'])}</b>?",
            Markup(rows), replace=msg,
        )
    elif cmd == "mem_fld":
        mid, pid, field = int(a[0]), int(a[1]), a[2]
        prompts = {
            "username": "Введите нового <b>Ответственного</b>: @username или ссылку t.me/username",
            "position": "Введите новую <b>должность</b> (или «-», чтобы очистить):",
            "description": "Введите новое <b>описание</b> (или «-», чтобы очистить):",
        }
        if field not in prompts:
            return await show_project(context, chat_id, user.id, pid, replace=msg)
        await ask(context, chat_id, msg, prompts[field], ("edit_member", mid, pid, field))
    elif cmd == "prj_delm":
        pid = int(a[0])
        members = many("SELECT * FROM members WHERE project_id=? ORDER BY id", pid)
        rows = [[Btn(f"❌ @{m['username']}", callback_data=f"mdel:{m['id']}:{pid}")] for m in members]
        rows.append([Btn("⬅️ Назад", callback_data=f"prj:{pid}")])
        await render(
            context, chat_id,
            "Кого убрать из проекта?" if members else "В проекте пока нет участников.",
            Markup(rows), replace=msg,
        )
    elif cmd == "mdel":
        run("DELETE FROM members WHERE id=?", int(a[0]))
        await show_project(context, chat_id, user.id, int(a[1]), replace=msg)
    elif cmd == "prj_del":
        p = one("SELECT name FROM projects WHERE id=?", int(a[0]))
        await render(
            context, chat_id,
            f"Удалить проект <b>{esc(p['name']) if p else ''}</b>? Это действие необратимо.",
            confirm_kb(f"prj_del_yes:{a[0]}", f"prj:{a[0]}"), replace=msg,
        )
    elif cmd == "prj_del_yes":
        run("DELETE FROM members WHERE project_id=?", int(a[0]))
        run("DELETE FROM projects WHERE id=?", int(a[0]))
        await show_projects(context, chat_id, user.id, replace=msg)

    # ── пользователи ──
    elif cmd == "usr":
        await show_users(context, chat_id, user.id, replace=msg)
    elif cmd == "usr_del":
        u = one("SELECT * FROM users WHERE user_id=?", int(a[0]))
        if not u or (u["role"] == "deputy" and role != "admin"):
            return await show_users(context, chat_id, user.id, replace=msg)
        await render(
            context, chat_id,
            f"Отозвать доступ у <b>{esc(u['full_name'] or str(u['user_id']))}</b>?",
            confirm_kb(f"usr_del_yes:{a[0]}", "usr"), replace=msg,
        )
    elif cmd == "usr_del_yes":
        u = one("SELECT * FROM users WHERE user_id=?", int(a[0]))
        if u and not (u["role"] == "deputy" and role != "admin"):
            run("DELETE FROM users WHERE user_id=?", u["user_id"])
            try:
                await context.bot.send_message(u["user_id"], "Ваш доступ к боту отозван.")
            except TelegramError:
                pass
        await show_users(context, chat_id, user.id, replace=msg)

    # ── только администратор ──
    elif cmd == "dep":
        await show_deputies(context, chat_id, replace=msg)
    elif cmd == "dep_t":
        u = one("SELECT * FROM users WHERE user_id=?", int(a[0]))
        if u:
            new_role = "user" if u["role"] == "deputy" else "deputy"
            run("UPDATE users SET role=? WHERE user_id=?", new_role, u["user_id"])
            try:
                await context.bot.send_message(
                    u["user_id"],
                    "⭐ Вы назначены заместителем администратора. Нажмите /menu."
                    if new_role == "deputy"
                    else "Права заместителя сняты.",
                )
            except TelegramError:
                pass
        await show_deputies(context, chat_id, replace=msg)
    elif cmd == "setimg":
        await ask(context, chat_id, msg, "Отправьте картинку для главного меню (как фото):", ("set_image",))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    state = context.user_data.get("state")

    if not state or get_role(user.id) not in MANAGERS:
        return await show_menu(context, chat_id, user.id)

    text = update.message.text.strip()
    kind = state[0]

    if kind == "add_prj_name":
        if len(text) > 100:
            return await update.message.reply_text("Слишком длинное название (максимум 100 символов).")
        context.user_data["state"] = ("add_prj_desc", text)
        await update.message.reply_text(
            "Теперь отправьте <b>описание</b> проекта (или «-», чтобы оставить пустым):",
            parse_mode=HTML,
        )
        return

    if kind in ("add_prj_desc", "edit_desc") and len(text) > 3000:
        return await update.message.reply_text("Слишком длинное описание (максимум 3000 символов).")

    if kind == "add_prj_desc":
        desc = "" if text == "-" else text
        cur = run("INSERT INTO projects(name, description) VALUES(?,?)", state[1], desc)
        context.user_data.pop("state", None)
        return await show_project(context, chat_id, user.id, cur.lastrowid)

    # ── добавление ответственного: три шага, каждый столбец отдельно ──
    if kind == "add_member":  # шаг 1: Ответственный
        parts = text.split()
        username = extract_username(parts[0]) if len(parts) == 1 else None
        if not username:
            return await update.message.reply_text(
                "Пришлите только @username (или ссылку t.me/username) — "
                "должность и описание бот спросит дальше."
            )
        context.user_data["state"] = ("add_member_pos", state[1], username)
        await update.message.reply_text(
            "Теперь отправьте <b>должность</b> (или «-», чтобы оставить пустой):",
            parse_mode=HTML,
        )
        return

    if kind == "add_member_pos":  # шаг 2: Должность
        position = "" if text == "-" else text[:100]
        context.user_data["state"] = ("add_member_desc", state[1], state[2], position)
        await update.message.reply_text(
            "Теперь отправьте <b>описание</b> (или «-», чтобы оставить пустым):",
            parse_mode=HTML,
        )
        return

    if kind == "add_member_desc":  # шаг 3: Описание
        if len(text) > 200:
            return await update.message.reply_text("Слишком длинное описание (максимум 200 символов).")
        run(
            "INSERT INTO members(project_id, username, position, description) VALUES(?,?,?,?)",
            state[1], state[2], state[3], "" if text == "-" else text,
        )
        context.user_data.pop("state", None)
        return await show_project(context, chat_id, user.id, state[1])

    # ── изменение одного столбца ответственного ──
    if kind == "edit_member":
        _, mid, pid, field = state
        if field == "username":
            parts = text.split()
            username = extract_username(parts[0]) if len(parts) == 1 else None
            if not username:
                return await update.message.reply_text(
                    "Не похоже на username. Пришлите @username или ссылку t.me/username."
                )
            run("UPDATE members SET username=? WHERE id=?", username, mid)
        elif field == "position":
            run("UPDATE members SET position=? WHERE id=?", "" if text == "-" else text[:100], mid)
        elif field == "description":
            if len(text) > 200:
                return await update.message.reply_text("Слишком длинное описание (максимум 200 символов).")
            run("UPDATE members SET description=? WHERE id=?", "" if text == "-" else text, mid)
        context.user_data.pop("state", None)
        return await show_project(context, chat_id, user.id, pid)

    if kind == "edit_name":
        if len(text) > 100:
            return await update.message.reply_text("Слишком длинное название (максимум 100 символов).")
        run("UPDATE projects SET name=? WHERE id=?", text, state[1])
    elif kind == "edit_desc":
        run("UPDATE projects SET description=? WHERE id=?", "" if text == "-" else text, state[1])
    else:
        return await show_menu(context, chat_id, user.id)

    context.user_data.pop("state", None)
    await show_project(context, chat_id, user.id, state[1])


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if get_role(user.id) == "admin" and context.user_data.get("state") == ("set_image",):
        file_id = update.message.photo[-1].file_id
        run(
            "INSERT INTO settings(key, value) VALUES('menu_image', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            file_id,
        )
        context.user_data.pop("state", None)
        await update.message.reply_text("Картинка меню обновлена ✅")
    await show_menu(context, update.effective_chat.id, user.id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка при обработке обновления", exc_info=context.error)


# ───────────────────────── Запуск ─────────────────────────


def start_health_server():
    """Если бот запущен как Web Service, Render требует открытый порт."""
    port = os.environ.get("PORT")
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_HEAD(self):  # UptimeRobot и подобные пингеры по умолчанию шлют HEAD
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def start_keepalive():
    """Раз в KEEPALIVE_INTERVAL секунд обращается к собственному публичному URL."""
    if not KEEPALIVE_URL or not os.environ.get("PORT"):
        return

    def loop():
        while True:
            time.sleep(KEEPALIVE_INTERVAL)  # сначала ждём: сервер уже должен быть поднят
            try:
                with urllib.request.urlopen(KEEPALIVE_URL, timeout=20) as resp:
                    resp.read()
            except Exception as e:  # сеть моргнула — просто попробуем в следующий раз
                log.warning("Keep-alive не удался: %s", e)

    threading.Thread(target=loop, daemon=True, name="keepalive").start()
    log.info("Keep-alive включён: %s каждые %s сек.", KEEPALIVE_URL, KEEPALIVE_INTERVAL)


def main():
    if not ADMIN_IDS:
        raise SystemExit("Задайте переменную окружения ADMIN_IDS (Telegram ID администратора).")
    start_health_server()
    start_keepalive()

    private = filters.ChatType.PRIVATE
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "menu"], cmd_start, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO & private, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, on_text))
    app.add_error_handler(on_error)

    log.info("Бот запущен. БД: %s. Админы: %s", DB_PATH, sorted(ADMIN_IDS))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
