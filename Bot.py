"""
Telegram-бот: белый список, заявки на доступ, проекты и команда проекта. Тест

Переменные окружения (Render -> Environment):
    BOT_TOKEN   - токен от @BotFather                       (обязательно)
    ADMIN_IDS   - Telegram ID администратора(ов) через запятую (обязательно)
    MENU_IMAGE  - URL картинки для меню                      (необязательно)
    DB_PATH     - путь к файлу базы, например /data/bot.db    (необязательно)
    AUTO_BACKUP - 0 отключает автокопии базы админу (по умолчанию включены)
    DOCS_URL    - ссылка на Confluence для кнопки «Документация по проектам»
                  по умолчанию (у проекта можно задать свою)     (необязательно)
    HURMA_URL   - адрес API «Отсутствующие сегодня» в Hurma     (необязательно)

Картинку меню можно также задать прямо в боте: Управление -> Картинка меню,
либо положить файл menu.jpg рядом с bot.py.

Токены Hurma (x-csrf-token, x-xsrf-token, Cookie) задаёт только администратор:
Управление -> Отсутствующие.

Расписание автообновления «Отсутствующих» (сколько раз в день и в какое время)
задаёт администратор: Управление -> Отсутствующие (настройки) -> Расписание.
По умолчанию — 09:00, 16:00, 23:00 (Киев).

Групповые чаты: если бота добавить в группу, он отвечает на сообщения, в которых
тегнули сотрудника из таблицы «Отсутствующие» («Сотрудник отсутствует» + даты).
Для этого у бота должен быть выключен Group Privacy (@BotFather -> Bot Settings ->
Group Privacy -> Turn off; после этого бота нужно удалить из группы и добавить заново)
либо бот должен быть администратором группы.
"""

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, TelegramError
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

# Резервные копии. Бот присылает копию базы админам и закрепляет её в их личном чате.
# Если на Render файловая система сбросилась и база пустая — при старте бот сам
# достаёт закреплённую копию и восстанавливает данные.
AUTO_BACKUP = os.environ.get("AUTO_BACKUP", "1") != "0"
BACKUP_QUIET = 30  # секунд без изменений в базе, после которых отправляется автокопия
BACKUP_PREFIX = "bot-backup-"
try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # нет базы часовых поясов — используем время сервера
    TZ = None

# Общая ссылка на документацию (Confluence). У каждого проекта можно задать свою
# прямо в боте (кнопка «Ссылка на документацию»), тогда будет использоваться она.
DOCS_URL = os.environ.get("DOCS_URL", "").strip()
URL_RE = re.compile(r"https?://\S+")

# Название кнопки «Часто задаваемые вопросы» по умолчанию (админ/зам меняет его в боте)
FAQ_DEFAULT_TITLE = "Часто задаваемые вопросы по проектам"

# Ширина колонок таблицы «Ответственные» в символах: Ответственный / Должность / Описание.
# Длинный текст переносится на следующую строку. Сумма + 6 — около 50,
# иначе таблица не поместится в сообщение на телефоне.
TABLE_WIDTHS = (18, 12, 16)

# ── «Отсутствующие» (Hurma) ──
HURMA_URL = os.environ.get(
    "HURMA_URL", "https://alliancedigital.hurma.work/api/v1/dashboard/absent-today"
).strip()
# Расписание по умолчанию (по Киеву). Админ меняет его в боте:
# Отсутствующие (настройки) -> Расписание
ABSENT_TIMES_DEFAULT = ((9, 0), (16, 0), (23, 0))
ABSENT_MAX_TIMES = 24  # максимум опросов в день
ABSENT_PRESETS = {  # готовые варианты: опросов в день -> время
    1: ((9, 0),),
    2: ((9, 0), (16, 0)),
    3: ((9, 0), (16, 0), (23, 0)),
    4: ((8, 0), (12, 0), (16, 0), (20, 0)),
    6: ((8, 0), (10, 0), (12, 0), (14, 0), (16, 0), (18, 0)),
}
ABSENT_PER_PAGE = 100
# Ширина колонок таблицы «Отсутствующие»:
# Проект / Должность / ФИО / Причина / Даты / Телеграм. Длинный текст переносится.
ABSENT_WIDTHS = (9, 9, 13, 10, 9, 13)
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# ── Упоминания отсутствующих в групповых чатах ──
# Не повторять предупреждение об одном и том же сотруднике тому же человеку в том же чате
ABSENT_MENTION_COOLDOWN = 600  # секунд
ABSENT_CACHE_MAX_AGE = 24 * 3600  # данные Hurma старше суток не используем
_mention_seen: dict[tuple, float] = {}

HTML = ParseMode.HTML
esc = html.escape

# ───────────────────────── База данных ─────────────────────────

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys = ON")
def ensure_schema() -> None:
    """Создаёт таблицы и добавляет недостающие колонки (при старте и после восстановления)."""
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
            description TEXT NOT NULL DEFAULT '',
            docs_url    TEXT NOT NULL DEFAULT '',
            faq_title   TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS members(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            username    TEXT NOT NULL,
            position    TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS faq(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title       TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            docs_url    TEXT NOT NULL DEFAULT '',
            parent_id   INTEGER REFERENCES faq(id) ON DELETE CASCADE   -- NULL = верхний уровень
        );
        CREATE TABLE IF NOT EXISTS settings(
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS absent_watch(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            email     TEXT NOT NULL UNIQUE,                  -- хранится в нижнем регистре
            project   TEXT NOT NULL DEFAULT '',
            position  TEXT NOT NULL DEFAULT '',
            telegram  TEXT NOT NULL DEFAULT ''               -- username без @
        );
        """
    )
    db.commit()

    # Миграция: колонка «Описание» для уже существующей базы (старые данные сохраняются)
    if "description" not in {r["name"] for r in db.execute("PRAGMA table_info(members)")}:
        db.execute("ALTER TABLE members ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        db.commit()

    # Миграция: ссылка на документацию у проекта
    if "docs_url" not in {r["name"] for r in db.execute("PRAGMA table_info(projects)")}:
        db.execute("ALTER TABLE projects ADD COLUMN docs_url TEXT NOT NULL DEFAULT ''")
        db.commit()

    # Миграция: название кнопки «Часто задаваемые вопросы» у проекта
    if "faq_title" not in {r["name"] for r in db.execute("PRAGMA table_info(projects)")}:
        db.execute("ALTER TABLE projects ADD COLUMN faq_title TEXT NOT NULL DEFAULT ''")
        db.commit()

    # Миграция: вложенные кнопки (директории) внутри «Часто задаваемых вопросов»
    if "parent_id" not in {r["name"] for r in db.execute("PRAGMA table_info(faq)")}:
        db.execute("ALTER TABLE faq ADD COLUMN parent_id INTEGER REFERENCES faq(id) ON DELETE CASCADE")
        db.commit()


ensure_schema()


_dirty = {"at": 0.0}  # время последнего значимого изменения данных (для автокопии)


def run(sql: str, *args, _quiet: bool = False) -> sqlite3.Cursor:
    cur = db.execute(sql, args)
    db.commit()
    if not _quiet and "notifications" not in sql:
        _dirty["at"] = time.time()
    return cur


def one(sql: str, *args):
    return db.execute(sql, args).fetchone()


def many(sql: str, *args):
    return db.execute(sql, args).fetchall()


def get_setting(key: str, default: str = "") -> str:
    row = one("SELECT value FROM settings WHERE key=?", key)
    return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: str, quiet: bool = False) -> None:
    run(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        key, value, _quiet=quiet,
    )


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
        [
            Btn("💾 Резервная копия", callback_data="bk"),
            Btn("♻️ Восстановить", callback_data="rs"),
        ],
    ]
    if role == "admin":
        rows.append([Btn("🛡 Заместители", callback_data="dep")])
        rows.append([Btn("🖼 Картинка меню", callback_data="setimg")])
        rows.append([Btn("🚫 Отсутствующие (настройки)", callback_data="abs_cfg")])
    rows.append([Btn("⬅️ Меню", callback_data="menu")])
    await render(context, chat_id, "⚙️ <b>Управление</b>", Markup(rows), replace=replace)


def red_btn(text: str, data: str) -> Btn:
    """Красная кнопка (Bot API 9.4: style=danger). В старых клиентах — обычная."""
    try:
        return Btn(text, callback_data=data, style="danger")
    except TypeError:  # старая версия python-telegram-bot
        return Btn(text, callback_data=data, api_kwargs={"style": "danger"})


async def show_projects(context, chat_id: int, uid: int, replace: Message | None = None):
    projects = many("SELECT id, name FROM projects ORDER BY name COLLATE NOCASE")
    rows = [[Btn(f"📁 {p['name']}", callback_data=f"prj:{p['id']}")] for p in projects]
    if get_role(uid) in MANAGERS:
        rows.append([Btn("➕ Добавить проект", callback_data="prj_add")])
    rows.append([red_btn("🚫 Отсутствующие", "abs")])  # всегда последней, после всех проектов
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
        lines += ["", "👥 <b>Ответственные QA на проектах:</b>", f"<pre>{esc(members_table(members))}</pre>"]

    # Кнопка со ссылкой на Confluence (своя у проекта или общая DOCS_URL)
    rows = []
    docs = p["docs_url"] or DOCS_URL
    if docs:
        rows.append([Btn("📚 Документация по проектам", url=docs)])
    rows.append([Btn(f"ℹ️ {faq_title_of(p)}"[:64], callback_data=f"faq:{pid}")])
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
            [Btn("📚 Ссылка на документацию", callback_data=f"prj_docs:{pid}")],
            [Btn("🗑 Удалить проект", callback_data=f"prj_del:{pid}")],
        ]
    rows.append([Btn("⬅️ Проекты", callback_data="prj:list"), Btn("🏠 Меню", callback_data="menu")])
    await render(context, chat_id, "\n".join(lines), Markup(rows), replace=replace)


def faq_title_of(p) -> str:
    return p["faq_title"] or FAQ_DEFAULT_TITLE


def parse_url(text: str) -> str | None:
    """«-» -> "" (убрать ссылку); корректная http(s)-ссылка -> она же; иначе None."""
    if text == "-":
        return ""
    return text if URL_RE.fullmatch(text) and len(text) <= 500 else None


def nav_rows(pid: int, back: str) -> list:
    """Кнопки навигации внизу каждого экрана раздела вопросов."""
    return [
        [Btn("⬅️ Вернуться назад", callback_data=back)],
        [Btn("🏠 Вернуться в главное меню проекта", callback_data=f"prj:{pid}")],
        [Btn("📁 Все проекты", callback_data="prj:list")],
    ]


async def show_faq(context, chat_id: int, uid: int, pid: int, replace: Message | None = None):
    """Список кнопок-вопросов проекта."""
    p = one("SELECT * FROM projects WHERE id=?", pid)
    if not p:
        return await show_projects(context, chat_id, uid, replace)
    items = many(
        "SELECT id, title FROM faq WHERE project_id=? AND parent_id IS NULL ORDER BY id", pid
    )
    is_mgr = get_role(uid) in MANAGERS

    rows = [[Btn(f"ℹ️ {i['title']}"[:60], callback_data=f"fq:{i['id']}")] for i in items]
    if is_mgr:
        rows.append(
            [
                Btn("➕ Добавить кнопку", callback_data=f"faq_add:{pid}:0"),
                Btn("✏️ Название раздела", callback_data=f"faq_title:{pid}"),
            ]
        )
    rows += nav_rows(pid, f"prj:{pid}")

    text = f"ℹ️ <b>{esc(faq_title_of(p))}</b>\n📁 {esc(p['name'])}"
    if not items:
        text += "\n\nПока нет ни одного вопроса."
        if is_mgr:
            text += "\nНажмите «➕ Добавить кнопку»."
    await render(context, chat_id, text, Markup(rows), replace=replace)


async def show_faq_item(context, chat_id: int, uid: int, fid: int, replace: Message | None = None):
    """Страница кнопки: описание, вложенные кнопки, ссылка на документацию, навигация."""
    f = one("SELECT * FROM faq WHERE id=?", fid)
    if not f:
        return await show_projects(context, chat_id, uid, replace)
    pid = f["project_id"]
    p = one("SELECT docs_url FROM projects WHERE id=?", pid)
    children = many("SELECT id, title FROM faq WHERE parent_id=? ORDER BY id", fid)

    if f["description"]:
        body = esc(f["description"])
    elif children:
        body = "<i>Выберите раздел ниже</i>"
    else:
        body = "<i>Описание пока не добавлено</i>"
    text = f"<b>{esc(f['title'])}</b>\n\n{body}"

    rows = [[Btn(f"ℹ️ {c['title']}"[:60], callback_data=f"fq:{c['id']}")] for c in children]
    url = f["docs_url"] or (p["docs_url"] if p else "") or DOCS_URL  # своя -> проекта -> общая
    if url:
        rows.append([Btn("📚 Документация", url=url)])
    if get_role(uid) in MANAGERS:
        rows += [
            [
                Btn("✏️ Название", callback_data=f"fq_name:{fid}"),
                Btn("✏️ Описание", callback_data=f"fq_desc:{fid}"),
            ],
            [Btn("📚 Ссылка на документацию", callback_data=f"fq_url:{fid}")],
            [Btn("➕ Добавить кнопку внутри", callback_data=f"faq_add:{pid}:{fid}")],
            [Btn("🗑 Удалить кнопку", callback_data=f"fq_del:{fid}")],
        ]
    back = f"fq:{f['parent_id']}" if f["parent_id"] else f"faq:{pid}"  # на шаг назад
    rows += nav_rows(pid, back)
    await render(context, chat_id, text, Markup(rows), replace=replace)


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


async def ask(context, chat_id: int, msg: Message, text: str, state: tuple, cancel: str = "menu"):
    """Просит админа/зама прислать текст и запоминает, что именно ждём."""
    context.user_data["state"] = state
    kb = Markup([[Btn("✖️ Отмена", callback_data=cancel)]])
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


# ───────────────────────── Резервные копии и восстановление ─────────────────────────


def stamp() -> str:
    return datetime.now(TZ).strftime("%Y%m%d-%H%M%S")


def pretty(dt) -> str:
    try:
        return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M") if TZ else dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "неизвестная дата"


def db_is_empty() -> bool:
    """Нет ни пользователей, ни проектов, ни ответственных, ни вопросов."""
    return not any(
        one(f"SELECT 1 FROM {t} LIMIT 1") for t in ("users", "projects", "members", "faq")
    )


def make_snapshot() -> bytes:
    """Целостная копия базы (через sqlite backup API) в виде байтов."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "snapshot.db"
        dst = sqlite3.connect(path)
        try:
            db.backup(dst)
        finally:
            dst.close()
        return path.read_bytes()


def validate_backup(path) -> str | None:
    """None — файл годится; иначе текст ошибки."""
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return "Файл повреждён."
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.Error:
        return "Это не резервная копия бота: файл не открывается как база данных."
    if not {"users", "projects"} <= tables:
        return "В файле нет данных бота (нет таблиц users и projects)."
    return None


def backup_summary(path) -> str:
    con = sqlite3.connect(path)
    try:
        def cnt(table: str) -> int:
            try:
                return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                return 0

        return (
            f"проектов — {cnt('projects')}, ответственных — {cnt('members')}, "
            f"кнопок вопросов — {cnt('faq')}, пользователей — {cnt('users')}"
        )
    finally:
        con.close()


def restore_from(path) -> None:
    """Полностью заменяет содержимое рабочей базы данными из файла."""
    src = sqlite3.connect(path)
    try:
        src.backup(db)
    finally:
        src.close()
    db.execute("PRAGMA foreign_keys = ON")
    ensure_schema()  # копия могла быть сделана более старой версией бота


async def send_backup(bot, chat_id: int, caption: str, pin: bool = False):
    """Отправляет файл копии. pin=True — закрепляет его (предыдущую копию открепляет)."""
    msg = await bot.send_document(
        chat_id,
        document=make_snapshot(),
        filename=f"{BACKUP_PREFIX}{stamp()}.db",
        caption=caption,
        disable_notification=pin,
    )
    if pin:
        key = f"backup_msg:{chat_id}"
        old = one("SELECT value FROM settings WHERE key=?", key)
        try:
            await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        except TelegramError as e:
            log.warning("Не удалось закрепить копию у %s: %s", chat_id, e)
        else:
            if old and old["value"]:
                try:
                    await bot.unpin_chat_message(chat_id, int(old["value"]))
                except (TelegramError, ValueError):
                    pass
            run(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                key, str(msg.message_id), _quiet=True,
            )
    return msg


async def fetch_pinned_backup(bot):
    """Ищет закреплённую копию в личных чатах админов. Возвращает (путь к файлу, дата) или None."""
    for admin_id in sorted(ADMIN_IDS):
        try:
            chat = await bot.get_chat(admin_id)
            pm = getattr(chat, "pinned_message", None)
            doc = getattr(pm, "document", None) if pm else None
            if not doc or not (doc.file_name or "").startswith(BACKUP_PREFIX):
                continue
            tg_file = await bot.get_file(doc.file_id)
            path = Path(tempfile.mkdtemp()) / "pinned.db"
            await tg_file.download_to_drive(path)
            return path, pretty(pm.date)
        except TelegramError as e:
            log.warning("Закреплённая копия у %s недоступна: %s", admin_id, e)
    return None


async def backup_tick(bot) -> None:
    """Если данные менялись и наступила тишина — шлёт автокопию админам."""
    at = _dirty["at"]
    if not at or time.time() - at < BACKUP_QUIET:
        return
    _dirty["at"] = 0.0
    if db_is_empty():  # пустую базу не закрепляем, чтобы не затереть хорошую копию
        return
    for admin_id in sorted(ADMIN_IDS):
        try:
            await send_backup(bot, admin_id, f"💾 Автокопия базы · {stamp()}", pin=True)
        except TelegramError as e:
            log.warning("Автокопия не отправлена %s: %s", admin_id, e)


async def backup_loop(bot) -> None:
    while True:
        await asyncio.sleep(15)
        try:
            await backup_tick(bot)
        except Exception:
            log.exception("Ошибка автокопии")


async def restore_on_startup(bot) -> None:
    """Пустая база (сброс на Render) -> пробуем восстановиться из закреплённой копии."""
    if not db_is_empty():
        return
    found = await fetch_pinned_backup(bot)
    if found and validate_backup(found[0]) is None:
        restore_from(found[0])
        text = (
            "♻️ Данные на сервере были сброшены. База автоматически восстановлена "
            f"из закреплённой копии от {found[1]}."
        )
        log.info("База восстановлена из закреплённой копии (%s)", found[1])
    else:
        text = (
            "ℹ️ Бот запущен с пустой базой (первый запуск или сброс данных на сервере). "
            "Закреплённой копии не найдено. Если есть файл копии: "
            "⚙️ Управление → ♻️ Восстановить → 📎 Из файла."
        )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except TelegramError:
            pass


async def show_restore(context, chat_id: int, uid: int, replace: Message | None = None):
    context.user_data.pop("restore", None)
    is_admin = get_role(uid) == "admin"
    rows = [[Btn("⚡ Быстрое восстановление", callback_data="rs_fast")]]
    if is_admin:
        rows.append([Btn("📎 Из файла", callback_data="rs_file")])
    rows.append([Btn("⬅️ Назад", callback_data="adm")])
    text = (
        "♻️ <b>Восстановление данных</b>\n\n"
        "⚡ <b>Быстрое</b> — берёт последнюю автокопию, которую бот прислал администратору "
        "и закрепил в его чате.\n"
        + ("📎 <b>Из файла</b> — вы сами присылаете файл копии (.db).\n" if is_admin else "")
        + "\nТекущие данные будут заменены."
    )
    await render(context, chat_id, text, Markup(rows), replace=replace)


def restore_confirm_text(path, label: str) -> str:
    return (
        "♻️ <b>Восстановление данных</b>\n"
        f"Источник: {esc(label)}\n"
        f"В копии: {backup_summary(path)}\n\n"
        "⚠️ Текущие данные будут заменены"
        + (" (перед этим бот пришлёт вам копию текущих)." if not db_is_empty() else ".")
    )


# ───────────────────────── Отсутствующие (Hurma API) ─────────────────────────

ABSENT_LOCK = asyncio.Lock()
_absent_wakeup: asyncio.Event | None = None  # будит absent_loop при смене расписания


def parse_times(text: str) -> list[tuple[int, int]] | None:
    """«9:00, 13.30 18» -> [(9, 0), (13, 30), (18, 0)]. None — если формат неверный."""
    tokens = [t for t in re.split(r"[\s,;]+", (text or "").strip()) if t]
    if not tokens:
        return None
    out = set()
    for t in tokens:
        m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", t)
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if h > 23 or mi > 59:
            return None
        out.add((h, mi))
    return sorted(out) if len(out) <= ABSENT_MAX_TIMES else None


def fmt_times(times) -> str:
    return ", ".join(f"{h:02d}:{m:02d}" for h, m in times)


def get_absent_times() -> tuple:
    """Текущее расписание из базы (или по умолчанию)."""
    parsed = parse_times(get_setting("absent_times"))
    return tuple(parsed) if parsed else ABSENT_TIMES_DEFAULT


def wake_absent_loop() -> None:
    """Сообщает циклу автообновления, что расписание изменилось."""
    if _absent_wakeup is not None:
        _absent_wakeup.set()


def save_absent_times(times) -> None:
    set_setting("absent_times", fmt_times(times))  # не quiet -> попадёт в автокопию
    wake_absent_loop()


def hurma_headers(csrf: str, xsrf: str, cookie: str) -> dict:
    parts = urllib.parse.urlsplit(HURMA_URL)
    origin = f"{parts.scheme}://{parts.netloc}/"
    h = {
        "accept": "application/json",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7,uk;q=0.6",
        "referer": origin,
        "x-requested-with": "XMLHttpRequest",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
        ),
        "Cookie": cookie,
    }
    if csrf:
        h["x-csrf-token"] = csrf
    if xsrf:
        h["x-xsrf-token"] = xsrf
    return h


def hurma_fetch_all(csrf: str, xsrf: str, cookie: str) -> list[dict]:
    """Синхронно забирает все страницы «absent-today». Бросает RuntimeError с понятным текстом."""
    if not cookie:
        raise RuntimeError("Не задан Cookie Hurma (Управление → Отсутствующие).")
    headers = hurma_headers(csrf, xsrf, cookie)
    items, page = [], 1
    while True:
        url = f"{HURMA_URL}?page={page}&per_page={ABSENT_PER_PAGE}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 419):
                raise RuntimeError(
                    f"HTTP {e.code}: сессия Hurma истекла — обновите токены / Cookie."
                ) from None
            raise RuntimeError(f"Hurma ответила HTTP {e.code}.") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Нет связи с Hurma: {e.reason}") from None
        except TimeoutError:
            raise RuntimeError("Hurma не ответила за 30 секунд.") from None
        try:
            js = json.loads(body)
        except ValueError:
            raise RuntimeError(
                "Hurma вернула не JSON (обычно это страница входа) — обновите токены / Cookie."
            ) from None
        if not isinstance(js, dict) or not isinstance(js.get("data"), list):
            raise RuntimeError("Неожиданный формат ответа Hurma (нет поля data).")
        for it in js["data"]:
            emp = it.get("employee") or {}
            items.append(
                {
                    "email": (emp.get("email") or "").strip().lower(),
                    "name": emp.get("name") or "",
                    "reason": it.get("name") or "",
                    "period": it.get("tooltipPeriod") or it.get("period") or it.get("duration") or "",
                }
            )
        last = (js.get("meta") or {}).get("last_page") or 1
        if page >= int(last) or page >= 50:
            break
        page += 1
    return items


async def refresh_absent() -> str | None:
    """Опрашивает API и сохраняет результат. Возвращает текст ошибки или None."""
    async with ABSENT_LOCK:
        csrf, xsrf, cookie = (get_setting(k) for k in ("hurma_csrf", "hurma_xsrf", "hurma_cookie"))
        try:
            items = await asyncio.to_thread(hurma_fetch_all, csrf, xsrf, cookie)
        except Exception as e:
            err = str(e) or e.__class__.__name__
            set_setting("absent_error", err, quiet=True)
            set_setting("absent_error_at", str(time.time()), quiet=True)
            log.warning("Отсутствующие: обновление не удалось: %s", err)
            return err
        set_setting("absent_cache", json.dumps(items, ensure_ascii=False), quiet=True)
        set_setting("absent_at", str(time.time()), quiet=True)
        set_setting("absent_error", "", quiet=True)
        log.info("Отсутствующие: обновлено, записей в ответе API — %s", len(items))
        return None


async def scheduled_absent_refresh(bot) -> None:
    if not get_setting("hurma_cookie"):
        return  # функция ещё не настроена — молча пропускаем
    err = await refresh_absent()
    if err:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ Не удалось обновить «Отсутствующие»: {err}\n"
                    "⚙️ Управление → 🚫 Отсутствующие (настройки) → обновите токены.",
                )
            except TelegramError:
                pass


def _slot(day, h: int, m: int) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def next_absent_slot(now: datetime) -> datetime:
    times = get_absent_times()
    for d in (0, 1):
        day = (now + timedelta(days=d)).date()
        for h, m in times:
            t = _slot(day, h, m)
            if t > now:
                return t
    return now + timedelta(hours=1)


def prev_absent_slot(now: datetime) -> datetime:
    times = get_absent_times()
    for d in (0, -1):
        day = (now + timedelta(days=d)).date()
        for h, m in reversed(times):
            t = _slot(day, h, m)
            if t <= now:
                return t
    return now - timedelta(days=1)


async def absent_loop(bot) -> None:
    """Автообновление по расписанию из настроек (Киев). После перезапуска — догоняет пропуск."""
    global _absent_wakeup
    _absent_wakeup = asyncio.Event()
    try:
        at = float(get_setting("absent_at") or 0)
        if at < prev_absent_slot(datetime.now(TZ)).timestamp():
            await scheduled_absent_refresh(bot)
    except Exception:
        log.exception("Отсутствующие: ошибка при стартовом обновлении")
    while True:
        now = datetime.now(TZ)
        nxt = next_absent_slot(now)
        _absent_wakeup.clear()
        try:
            await asyncio.wait_for(
                _absent_wakeup.wait(), timeout=max(1.0, (nxt - now).total_seconds())
            )
            log.info("Отсутствующие: расписание изменено -> %s", fmt_times(get_absent_times()))
            continue  # расписание изменилось — пересчитываем следующий слот
        except asyncio.TimeoutError:
            pass
        try:
            await scheduled_absent_refresh(bot)
        except Exception:
            log.exception("Отсутствующие: ошибка автообновления")


def fmt_ts(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts), TZ).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return "—"


def text_table(headers: tuple, data: list[tuple], max_widths: tuple, nowrap: tuple = ()):
    """Моноширинная таблица с переносом.

    Возвращает (строки шапки, блоки строк по записям, ширины). Каждая строка — список
    ячеек, уже дополненных пробелами; None — строка-разделитель.
    Колонки из nowrap не переносятся (ширина = самое длинное значение).
    """
    n = len(headers)
    widths = []
    for i in range(n):
        longest = max(len(r[i]) for r in [headers] + data)
        widths.append(max(1, longest if i in nowrap else min(longest, max_widths[i])))

    def fmt_row(cells) -> list[list[str]]:
        wrapped = [textwrap.wrap(c, widths[i]) or [""] for i, c in enumerate(cells)]
        height = max(len(w) for w in wrapped)
        return [
            [(wrapped[i][k] if k < len(wrapped[i]) else "").ljust(widths[i]) for i in range(n)]
            for k in range(height)
        ]

    head = fmt_row(headers) + [None]
    blocks = []
    for idx, row in enumerate(data):
        block = fmt_row(row)
        if idx < len(data) - 1:
            block.append(None)
        blocks.append(block)
    return head, blocks, widths


def table_line(cells, widths) -> str:
    """Одна строка моноширинной таблицы (None — разделитель)."""
    if cells is None:
        return "┼".join("─" * w for w in widths)
    return "│".join(cells).rstrip()


def copy_btn(nick: str) -> Btn:
    """Кнопка, которая по нажатию копирует ник в буфер обмена (Bot API 7.11+)."""
    try:
        from telegram import CopyTextButton

        return Btn(f"📋 {nick}", copy_text=CopyTextButton(nick))
    except (ImportError, TypeError):  # старая версия python-telegram-bot
        return Btn(f"📋 {nick}", api_kwargs={"copy_text": {"text": nick}})


def absent_rows() -> list[tuple]:
    """Строки таблицы: только сотрудники из списка администратора (полное совпадение email)."""
    watch = {w["email"].strip().lower(): w for w in many("SELECT * FROM absent_watch")}
    try:
        data = json.loads(get_setting("absent_cache") or "[]")
    except ValueError:
        data = []
    rows = []
    for it in data:
        w = watch.get((it.get("email") or "").strip().lower())
        if not w:
            continue
        rows.append(
            (
                w["project"] or "—",
                w["position"] or "—",
                it.get("name") or "—",
                it.get("reason") or "—",
                it.get("period") or "—",
                f"@{w['telegram']}" if w["telegram"] else "—",
            )
        )
    rows.sort(key=lambda r: (r[0].lower(), r[2].lower()))
    return rows


async def render_many(context, chat_id: int, chunks: list[str], kb: Markup, replace: Message | None):
    """Как render, но для длинного текста, разбитого на несколько сообщений."""
    if len(chunks) == 1:
        return await render(context, chat_id, chunks[0], kb, replace=replace)
    if replace is not None:
        try:
            await replace.delete()
        except TelegramError:
            pass
    for c in chunks[:-1]:
        await context.bot.send_message(chat_id, c, parse_mode=HTML)
    await context.bot.send_message(chat_id, chunks[-1], reply_markup=kb, parse_mode=HTML)


async def show_absent(context, chat_id: int, uid: int, replace: Message | None = None, note: str = ""):
    """Экран «Отсутствующие» — видят все пользователи с доступом."""
    is_admin = get_role(uid) == "admin"
    rows_kb = []
    if is_admin:
        rows_kb.append(
            [
                Btn("🔄 Обновить сейчас", callback_data="abs_upd:v"),
                Btn("⚙️ Настройки", callback_data="abs_cfg"),
            ]
        )
    rows_kb.append([Btn("⬅️ Проекты", callback_data="prj:list"), Btn("🏠 Меню", callback_data="menu")])
    kb = Markup(rows_kb)

    head = ["🚫 <b>Отсутствующие сегодня</b>"]
    at = get_setting("absent_at")
    head.append(f"🕒 Обновлено: {fmt_ts(at)}" if at else "🕒 Данные ещё не загружались.")
    err = get_setting("absent_error")
    if err:
        head.append(f"⚠️ Последнее обновление не удалось ({fmt_ts(get_setting('absent_error_at'))}).")
        if is_admin:
            head.append(f"<i>{esc(err)}</i>")
    if note:
        head.append(note)
    title = "\n".join(head)

    if not one("SELECT 1 FROM absent_watch LIMIT 1"):
        msg = "\n\nСписок сотрудников для отслеживания пуст."
        if is_admin:
            msg += "\nДобавьте их: ⚙️ Настройки → 👤 Сотрудники."
        return await render(context, chat_id, title + msg, kb, replace=replace)

    data = absent_rows()
    if not data:
        text = title + ("\n\n✅ Сегодня все на месте." if at else "")
        return await render(context, chat_id, text, kb, replace=replace)

    headers = ("Проект", "Должность", "ФИО", "Причина", "Даты", "Телеграм")
    thead, blocks, widths = text_table(headers, data, ABSENT_WIDTHS, nowrap=(5,))

    def table_html(lines) -> str:
        return "<pre>" + esc("\n".join(table_line(c, widths) for c in thead + lines)) + "</pre>"

    # Ровная таблица в <pre>; копирование ников — отдельными кнопками под ней
    nicks = []
    for r in data:
        if r[5].startswith("@") and r[5] not in nicks:
            nicks.append(r[5])
    if nicks:
        copy_rows = [[copy_btn(n) for n in nicks[i:i + 2]] for i in range(0, len(nicks), 2)]
        kb = Markup(copy_rows + rows_kb)

    # Разбиваем на сообщения (лимит Telegram — 4096 символов)
    limit = 3800
    chunks, cur = [], []
    hint = "\n<i>📋 Нажмите кнопку с ником ниже — он скопируется.</i>" if nicks else ""
    prefix = title + hint + "\n\n"
    for b in blocks:
        if cur and len(prefix) + len(table_html(cur + b)) > limit:
            chunks.append(prefix + table_html(cur))
            prefix, cur = "", []
        cur += b
    chunks.append(prefix + table_html(cur))
    await render_many(context, chat_id, chunks, kb, replace)


def mask(value: str) -> str:
    if not value:
        return "❌ не задан"
    return f"✅ задан (<code>{esc(value[:6])}…</code>, {len(value)} симв.)"


async def show_absent_cfg(context, chat_id: int, replace: Message | None = None, note: str = ""):
    """Настройки «Отсутствующих» — только администратор."""
    n = one("SELECT COUNT(*) AS n FROM absent_watch")["n"]
    at, err = get_setting("absent_at"), get_setting("absent_error")
    sched = get_absent_times()
    lines = [
        "🚫 <b>Отсутствующие — настройки</b>",
        "",
        f"👤 Сотрудников в списке: {n}",
        f"🕒 Последнее успешное обновление: {fmt_ts(at) if at else '—'}",
    ]
    if err:
        lines.append(f"⚠️ Ошибка ({fmt_ts(get_setting('absent_error_at'))}): <i>{esc(err)}</i>")
    lines += [
        f"⏰ Автообновление: {len(sched)} раз(а) в день — {fmt_times(sched)} (Киев)",
        "",
        f"x-csrf-token: {mask(get_setting('hurma_csrf'))}",
        f"x-xsrf-token: {mask(get_setting('hurma_xsrf'))}",
        f"Cookie: {mask(get_setting('hurma_cookie'))}",
        "",
        "<i>Hurma проверяет сессию по Cookie (hurma_session / remember_web_…). "
        "Проще всего: DevTools → Network → absent-today → Copy as cURL (bash) "
        "и отправить боту через «📋 Вставить cURL».</i>",
    ]
    if note:
        lines += ["", note]
    rows = [
        [Btn(f"👤 Сотрудники ({n})", callback_data="abs_list")],
        [Btn("📋 Вставить cURL целиком", callback_data="abs_curl")],
        [
            Btn("🔑 x-csrf-token", callback_data="abs_tok:csrf"),
            Btn("🔑 x-xsrf-token", callback_data="abs_tok:xsrf"),
        ],
        [Btn("🍪 Cookie", callback_data="abs_tok:cookie")],
        [Btn("⏰ Расписание", callback_data="abs_sch")],
        [Btn("🔄 Обновить сейчас", callback_data="abs_upd:c")],
        [red_btn("🚫 Открыть таблицу", "abs")],
        [Btn("⬅️ Управление", callback_data="adm")],
    ]
    await render(context, chat_id, "\n".join(lines), Markup(rows), replace=replace)


async def show_absent_sched(context, chat_id: int, replace: Message | None = None, note: str = ""):
    """Расписание автообновления «Отсутствующих» — только администратор."""
    times = get_absent_times()
    nxt = next_absent_slot(datetime.now(TZ))
    lines = [
        "⏰ <b>Расписание обновления «Отсутствующих»</b>",
        "",
        f"Опросов в день: <b>{len(times)}</b>",
        f"Время (Киев): <b>{fmt_times(times)}</b>",
        f"Следующее обновление: {nxt.strftime('%d.%m %H:%M')}",
        "",
        "<i>Выберите готовый вариант (сколько раз в день) или задайте своё время.</i>",
    ]
    if note:
        lines += ["", note]
    presets = [Btn(f"{k}×", callback_data=f"abs_sch_set:{k}") for k in ABSENT_PRESETS]
    rows = [
        presets,
        [Btn("✏️ Задать своё время", callback_data="abs_sch_edit")],
        [Btn("⬅️ Настройки", callback_data="abs_cfg")],
    ]
    await render(context, chat_id, "\n".join(lines), Markup(rows), replace=replace)


async def show_absent_list(context, chat_id: int, replace: Message | None = None):
    items = many("SELECT * FROM absent_watch ORDER BY project COLLATE NOCASE, email")
    lines = ["👤 <b>Сотрудники для отслеживания</b>", "Поиск — по полному совпадению email в ответе API.", ""]
    for i, w in enumerate(items, 1):
        tg = f"@{w['telegram']}" if w["telegram"] else "—"
        lines.append(
            f"{i}. <code>{esc(w['email'])}</code>\n"
            f"    {esc(w['project'] or '—')} · {esc(w['position'] or '—')} · {esc(tg)}"
        )
    if not items:
        lines.append("Список пуст.")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    rows = [[Btn(f"❌ {w['email']}"[:60], callback_data=f"abs_del:{w['id']}")] for w in items]
    rows.append([Btn("➕ Добавить сотрудника", callback_data="abs_add")])
    rows.append([Btn("⬅️ Назад", callback_data="abs_cfg")])
    await render(context, chat_id, text, Markup(rows), replace=replace)


def clean_header_value(text: str, name: str) -> str:
    """Убирает «--header», кавычки и «name:» — можно вставить строку прямо из cURL."""
    t = text.strip()
    t = re.sub(r"^(?:-H|--header|-b|--cookie)\s+", "", t)
    t = t.strip().lstrip("$").strip("'\"").strip()
    if t.lower().startswith(name.lower() + ":"):
        t = t[len(name) + 1:].strip()
    return t.strip("'\"").strip()


def sync_cookie_xsrf(cookie: str, xsrf: str) -> str:
    """Подставляет новый x-xsrf-token в cookie XSRF-TOKEN (если он там есть)."""
    if not cookie or not xsrf:
        return cookie
    enc = urllib.parse.quote(urllib.parse.unquote(xsrf), safe="")
    return re.sub(r"XSRF-TOKEN=[^;]*", lambda _: f"XSRF-TOKEN={enc}", cookie)


def parse_curl(text: str) -> dict:
    """Достаёт x-csrf-token, x-xsrf-token и Cookie из команды cURL (формат bash)."""
    found = {}
    for m in re.finditer(r"(?:-H|--header)\s+\$?(['\"])(.*?)\1", text, re.S):
        key, _, val = m.group(2).partition(":")
        key = key.strip().lower()
        if key in ("x-csrf-token", "x-xsrf-token", "cookie"):
            found[key] = val.strip()
    m = re.search(r"(?:-b|--cookie)\s+\$?(['\"])(.*?)\1", text, re.S)
    if m and "cookie" not in found:
        found["cookie"] = m.group(2).strip()
    return found


def apply_curl(text: str) -> str:
    """Сохраняет найденные в cURL параметры. Возвращает текст результата."""
    found = parse_curl(text)
    if not found:
        return (
            "❌ В тексте не найдено ни x-csrf-token, ни x-xsrf-token, ни Cookie. "
            "Скопируйте запрос как «Copy as cURL (bash)»."
        )
    names = {"x-csrf-token": "hurma_csrf", "x-xsrf-token": "hurma_xsrf", "cookie": "hurma_cookie"}
    for k, v in found.items():
        set_setting(names[k], v)
    return "✅ Обновлено: " + ", ".join(found) + "."


# ───────────────────────── Упоминания отсутствующих в группах ─────────────────────────


def absent_by_nick() -> dict[str, dict]:
    """{username в нижнем регистре: {nick, name, period}}: кто из списка администратора отсутствует сегодня."""
    try:
        if time.time() - float(get_setting("absent_at") or 0) > ABSENT_CACHE_MAX_AGE:
            return {}
        data = json.loads(get_setting("absent_cache") or "[]")
    except ValueError:
        return {}
    by_email = {(it.get("email") or "").strip().lower(): it for it in data}
    out = {}
    for w in many("SELECT email, telegram FROM absent_watch WHERE telegram != ''"):
        it = by_email.get(w["email"].strip().lower())
        if it:
            out[w["telegram"].lower()] = {
                "nick": w["telegram"],
                "name": it.get("name") or "",
                "period": it.get("period") or "",
            }
    return out


def mentioned_nicks(msg: Message) -> list[str]:
    """Все username, упомянутые в тексте или подписи (@nick и упоминания без username)."""
    types = ["mention", "text_mention"]
    found = {**msg.parse_entities(types), **msg.parse_caption_entities(types)}
    nicks = []
    for ent, txt in found.items():
        if ent.type == "text_mention":
            nick = ent.user.username if ent.user else None
        else:
            nick = txt.lstrip("@")
        if nick and nick.lower() not in nicks:
            nicks.append(nick.lower())
    return nicks


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Групповой чат: тегнули отсутствующего — отвечаем тому, кто тегнул."""
    msg = update.effective_message
    if not msg or not msg.from_user or msg.from_user.is_bot:
        return
    nicks = mentioned_nicks(msg)
    if not nicks:
        return
    absent = absent_by_nick()
    if not absent:
        return

    now = time.time()
    for k, t in list(_mention_seen.items()):  # чистим устаревшие записи антиспама
        if now - t > ABSENT_MENTION_COOLDOWN:
            del _mention_seen[k]

    lines = []
    for n in nicks:
        a = absent.get(n)
        if not a:
            continue
        key = (msg.chat_id, msg.from_user.id, n)
        if key in _mention_seen:
            continue
        _mention_seen[key] = now
        # Ник в <code>, чтобы бот не тегал отсутствующего повторно
        who = f"<code>@{esc(a['nick'])}</code>"
        if a["name"]:
            who += f" ({esc(a['name'])})"
        line = f"🚫 <b>Сотрудник отсутствует</b>: {who}"
        if a["period"]:
            line += f"\n📅 Не будет: {esc(a['period'])}"
        lines.append(line)

    if not lines:
        return
    try:
        await msg.reply_text("\n\n".join(lines), parse_mode=HTML)  # ответ на сообщение с тегом
    except TelegramError as e:
        log.warning("Не удалось ответить в чате %s: %s", msg.chat_id, e)


# ───────────────────────── Обработчики ─────────────────────────

MANAGER_CMDS = {
    "dec", "adm", "prj_add", "prj_name", "prj_desc", "prj_addm", "prj_delm",
    "prj_edm", "mem_pick", "mem_fld", "prj_docs",
    "faq_add", "faq_title", "fq_name", "fq_desc", "fq_url", "fq_del", "fq_del_yes",
    "bk", "rs", "rs_fast", "rs_yes",
    "mdel", "prj_del", "prj_del_yes", "usr", "usr_del", "usr_del_yes",
}
ADMIN_CMDS = {
    "dep", "dep_t", "setimg", "rs_file",
    "abs_cfg", "abs_list", "abs_add", "abs_prj", "abs_del", "abs_del_yes",
    "abs_tok", "abs_curl", "abs_upd", "abs_sch", "abs_sch_set", "abs_sch_edit",
}


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    u = update.effective_user
    run("UPDATE users SET username=?, full_name=? WHERE user_id=?", u.username, u.full_name, u.id, _quiet=True)
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

    elif cmd == "prj_docs":
        await ask(
            context, chat_id, msg,
            "Отправьте <b>ссылку на документацию</b> проекта в Confluence (https://...)\n"
            "или «-», чтобы убрать свою ссылку (тогда будет общая, если она задана).",
            ("edit_docs", int(a[0])),
        )

    # ── часто задаваемые вопросы ──
    elif cmd == "faq":
        await show_faq(context, chat_id, user.id, int(a[0]), replace=msg)
    elif cmd == "fq":
        await show_faq_item(context, chat_id, user.id, int(a[0]), replace=msg)
    elif cmd == "faq_add":
        pid = int(a[0])
        parent = int(a[1]) if len(a) > 1 else 0  # 0 = верхний уровень
        where = ""
        if parent:
            par = one("SELECT title FROM faq WHERE id=?", parent)
            if par:
                where = f" внутри «{esc(par['title'])}»"
        await ask(
            context, chat_id, msg,
            f"Введите <b>название новой кнопки</b>{where} (до 60 символов):",
            ("faq_add_title", pid, parent),
            cancel=f"fq:{parent}" if parent else f"faq:{pid}",
        )
    elif cmd == "faq_title":
        await ask(
            context, chat_id, msg,
            "Введите новое <b>название раздела</b> (кнопки в карточке проекта)\n"
            "или «-», чтобы вернуть стандартное.",
            ("faq_title", int(a[0])), cancel=f"faq:{a[0]}",
        )
    elif cmd == "fq_name":
        await ask(
            context, chat_id, msg, "Введите новое <b>название кнопки</b>:",
            ("faq_edit", int(a[0]), "title"), cancel=f"fq:{a[0]}",
        )
    elif cmd == "fq_desc":
        await ask(
            context, chat_id, msg, "Отправьте новое <b>описание</b> (или «-», чтобы очистить):",
            ("faq_edit", int(a[0]), "description"), cancel=f"fq:{a[0]}",
        )
    elif cmd == "fq_url":
        await ask(
            context, chat_id, msg,
            "Отправьте <b>ссылку на документацию</b> (Confluence, https://...)\n"
            "или «-», чтобы убрать (тогда будет ссылка проекта, если она задана).",
            ("faq_edit", int(a[0]), "docs_url"), cancel=f"fq:{a[0]}",
        )
    elif cmd == "fq_del":
        fid = int(a[0])
        f = one("SELECT title FROM faq WHERE id=?", fid)
        nested = one(
            "WITH RECURSIVE d(id) AS (SELECT id FROM faq WHERE parent_id=? "
            "UNION ALL SELECT x.id FROM faq x JOIN d ON x.parent_id=d.id) "
            "SELECT COUNT(*) AS n FROM d",
            fid,
        )["n"]
        warn = f"\nВместе с ней удалятся вложенные кнопки: {nested}." if nested else ""
        await render(
            context, chat_id,
            f"Удалить кнопку <b>{esc(f['title']) if f else ''}</b>?{warn}",
            confirm_kb(f"fq_del_yes:{fid}", f"fq:{fid}"), replace=msg,
        )
    elif cmd == "fq_del_yes":
        f = one("SELECT project_id, parent_id FROM faq WHERE id=?", int(a[0]))
        run("DELETE FROM faq WHERE id=?", int(a[0]))  # вложенные удаляются каскадом
        if f and f["parent_id"]:
            await show_faq_item(context, chat_id, user.id, f["parent_id"], replace=msg)
        elif f:
            await show_faq(context, chat_id, user.id, f["project_id"], replace=msg)
        else:
            await show_projects(context, chat_id, user.id, replace=msg)

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
        run("DELETE FROM faq WHERE project_id=?", int(a[0]))
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

    # ── резервные копии и восстановление (админ и заместитель) ──
    elif cmd == "bk":
        await send_backup(context.bot, chat_id, f"💾 Резервная копия базы · {stamp()}")
    elif cmd == "rs":
        await show_restore(context, chat_id, user.id, replace=msg)
    elif cmd == "rs_fast":
        back = Markup([[Btn("⬅️ Назад", callback_data="rs")]])
        found = await fetch_pinned_backup(context.bot)
        if not found:
            await render(
                context, chat_id,
                "Закреплённой автокопии не найдено. Администратор может восстановить "
                "данные из файла («📎 Из файла»).",
                back, replace=msg,
            )
        else:
            path, when = found
            err = validate_backup(path)
            if err:
                await render(context, chat_id, f"❌ {esc(err)}", back, replace=msg)
            else:
                label = f"автокопия от {when}"
                context.user_data["restore"] = (str(path), "pinned", label)
                await render(
                    context, chat_id, restore_confirm_text(path, label),
                    confirm_kb("rs_yes", "rs"), replace=msg,
                )
    elif cmd == "rs_file":  # только администратор
        await ask(
            context, chat_id, msg,
            "Отправьте <b>файл резервной копии</b> (.db) как документ.",
            ("restore_file",), cancel="rs",
        )
    elif cmd == "rs_yes":
        info = context.user_data.pop("restore", None)
        back = Markup([[Btn("⬅️ Назад", callback_data="rs")]])
        if not info or not Path(info[0]).exists():
            await render(context, chat_id, "Сессия устарела. Начните восстановление заново.", back, replace=msg)
        else:
            path, source, label = info
            err = validate_backup(path)
            if err:
                await render(context, chat_id, f"❌ {esc(err)}", back, replace=msg)
            else:
                if not db_is_empty():  # страховка: копия текущих данных перед заменой
                    try:
                        await send_backup(
                            context.bot, chat_id, f"🗂 Копия текущих данных перед восстановлением · {stamp()}"
                        )
                    except TelegramError as e:
                        log.warning("Не удалось отправить страховочную копию: %s", e)
                restore_from(path)
                wake_absent_loop()  # в копии может быть другое расписание «Отсутствующих»
                if source == "file":  # закреплённая копия должна совпасть с новым состоянием
                    _dirty["at"] = time.time()
                log.info("Данные восстановлены (%s) пользователем %s", label, user.id)
                await render(
                    context, chat_id, f"✅ Данные восстановлены ({esc(label)}).",
                    Markup([[Btn("⚙️ Управление", callback_data="adm")]]), replace=msg,
                )

    # ── отсутствующие ──
    elif cmd == "abs":  # таблица — всем пользователям с доступом
        await show_absent(context, chat_id, user.id, replace=msg)
    elif cmd == "abs_cfg":
        await show_absent_cfg(context, chat_id, replace=msg)
    elif cmd == "abs_list":
        context.user_data.pop("abs_new", None)
        await show_absent_list(context, chat_id, replace=msg)
    elif cmd == "abs_add":
        context.user_data.pop("abs_new", None)
        await ask(
            context, chat_id, msg,
            "Введите <b>email</b> сотрудника — точно как в Hurma (поле employee.email):",
            ("abs_email",), cancel="abs_list",
        )
    elif cmd == "abs_prj":  # выбор проекта из списка при добавлении сотрудника
        new = context.user_data.get("abs_new")
        p = one("SELECT name FROM projects WHERE id=?", int(a[0]))
        if not new or not p:
            return await show_absent_list(context, chat_id, replace=msg)
        new["project"] = p["name"]
        await ask(
            context, chat_id, msg,
            f"Проект: <b>{esc(p['name'])}</b>\nТеперь введите <b>должность</b> (или «-»):",
            ("abs_pos",), cancel="abs_list",
        )
    elif cmd == "abs_del":
        w = one("SELECT * FROM absent_watch WHERE id=?", int(a[0]))
        if not w:
            return await show_absent_list(context, chat_id, replace=msg)
        await render(
            context, chat_id,
            f"Убрать из отслеживания <code>{esc(w['email'])}</code>?",
            confirm_kb(f"abs_del_yes:{a[0]}", "abs_list"), replace=msg,
        )
    elif cmd == "abs_del_yes":
        run("DELETE FROM absent_watch WHERE id=?", int(a[0]))
        await show_absent_list(context, chat_id, replace=msg)
    elif cmd == "abs_tok":
        prompts = {
            "csrf": "Отправьте новый <b>x-csrf-token</b>\n(можно строкой из cURL: <code>x-csrf-token: …</code>).",
            "xsrf": "Отправьте новый <b>x-xsrf-token</b>\n(XSRF-TOKEN в Cookie обновится автоматически).",
            "cookie": "Отправьте новое значение заголовка <b>Cookie</b> целиком\n"
                      "(должны быть hurma_session и remember_web_…).",
        }
        if a and a[0] in prompts:
            await ask(context, chat_id, msg, prompts[a[0]] + "\n\n🔒 Сообщение с токеном бот удалит.",
                      ("abs_tok", a[0]), cancel="abs_cfg")
        else:
            await show_absent_cfg(context, chat_id, replace=msg)
    elif cmd == "abs_curl":
        await ask(
            context, chat_id, msg,
            "Отправьте запрос <b>absent-today</b> из браузера:\n"
            "DevTools → Network → absent-today → ПКМ → Copy → <b>Copy as cURL (bash)</b>.\n\n"
            "Если текст длиннее 4096 символов — пришлите его файлом <b>.txt</b>.\n"
            "Бот возьмёт x-csrf-token, x-xsrf-token и Cookie.\n\n🔒 Сообщение бот удалит.",
            ("abs_curl",), cancel="abs_cfg",
        )
    elif cmd == "abs_sch":
        await show_absent_sched(context, chat_id, replace=msg)
    elif cmd == "abs_sch_set":
        preset = ABSENT_PRESETS.get(int(a[0])) if a and a[0].isdigit() else None
        note = ""
        if preset:
            save_absent_times(preset)
            note = f"✅ Сохранено: {len(preset)} раз(а) в день — {fmt_times(preset)}."
        await show_absent_sched(context, chat_id, replace=msg, note=note)
    elif cmd == "abs_sch_edit":
        await ask(
            context, chat_id, msg,
            "Отправьте <b>время опросов</b> (по Киеву) через запятую или пробел.\n"
            "Количество опросов в день = количество указанных времён "
            f"(от 1 до {ABSENT_MAX_TIMES}).\n\n"
            "Пример: <code>9:00, 12:30, 18:00</code>\n"
            f"Сейчас: <code>{fmt_times(get_absent_times())}</code>",
            ("abs_times",), cancel="abs_sch",
        )
    elif cmd == "abs_upd":
        await render(context, chat_id, "⏳ Запрашиваю данные в Hurma…", None, replace=msg)
        err = await refresh_absent()
        note = f"❌ {esc(err)}" if err else "✅ Данные обновлены."
        if a and a[0] == "c":
            await show_absent_cfg(context, chat_id, replace=msg, note=note)
        else:
            await show_absent(context, chat_id, user.id, replace=msg, note=note)

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


async def on_absent_text(update: Update, context: ContextTypes.DEFAULT_TYPE, state: tuple, text: str):
    """Ввод текста в разделе «Отсутствующие» (только администратор)."""
    chat_id = update.effective_chat.id
    kind = state[0]
    cancel_kb = Markup([[Btn("✖️ Отмена", callback_data="abs_list")]])

    if kind in ("abs_tok", "abs_curl"):
        try:
            await update.message.delete()  # не оставляем токены в истории чата
        except TelegramError:
            pass
        if kind == "abs_curl":
            note = apply_curl(update.message.text)
        else:
            field = state[1]
            name = {"csrf": "x-csrf-token", "xsrf": "x-xsrf-token", "cookie": "Cookie"}[field]
            value = clean_header_value(update.message.text, name)
            if not value or (field != "cookie" and re.search(r"\s", value)) or (
                field == "cookie" and "=" not in value
            ):
                return await context.bot.send_message(
                    chat_id, f"❌ Не похоже на {name}. Попробуйте ещё раз или нажмите «Отмена».",
                    reply_markup=Markup([[Btn("✖️ Отмена", callback_data="abs_cfg")]]),
                )
            set_setting(f"hurma_{field}", value)
            if field == "xsrf":
                set_setting("hurma_cookie", sync_cookie_xsrf(get_setting("hurma_cookie"), value))
            note = f"✅ {name} обновлён."
        context.user_data.pop("state", None)
        return await show_absent_cfg(context, chat_id, note=note)

    if kind == "abs_times":  # своё расписание автообновления
        times = parse_times(text)
        if times is None:
            return await update.message.reply_text(
                "Не удалось разобрать время. Формат: ЧЧ:ММ через запятую, "
                f"например 9:00, 13:30, 18:00 (от 1 до {ABSENT_MAX_TIMES} значений).",
                reply_markup=Markup([[Btn("✖️ Отмена", callback_data="abs_sch")]]),
            )
        save_absent_times(times)
        context.user_data.pop("state", None)
        return await show_absent_sched(
            context, chat_id,
            note=f"✅ Сохранено: {len(times)} раз(а) в день — {fmt_times(times)}.",
        )

    new = context.user_data.get("abs_new")

    if kind == "abs_email":
        email = text.strip().lower()
        if not EMAIL_RE.fullmatch(email) or len(email) > 200:
            return await update.message.reply_text("Не похоже на email. Попробуйте ещё раз:", reply_markup=cancel_kb)
        if one("SELECT 1 FROM absent_watch WHERE email=?", email):
            return await update.message.reply_text(
                "Этот email уже в списке. Введите другой:", reply_markup=cancel_kb
            )
        context.user_data["abs_new"] = {"email": email}
        context.user_data["state"] = ("abs_project",)
        projects = many("SELECT id, name FROM projects ORDER BY name COLLATE NOCASE")
        rows = [[Btn(f"📁 {p['name']}"[:60], callback_data=f"abs_prj:{p['id']}")] for p in projects]
        rows.append([Btn("✖️ Отмена", callback_data="abs_list")])
        return await update.message.reply_text(
            "Выберите <b>проект</b> кнопкой или введите название вручную:",
            parse_mode=HTML, reply_markup=Markup(rows),
        )

    if not new:  # данные шага потерялись (например, после перезапуска)
        context.user_data.pop("state", None)
        return await show_absent_list(context, chat_id)

    if kind == "abs_project":
        if len(text) > 100:
            return await update.message.reply_text("Слишком длинное название (максимум 100 символов).")
        new["project"] = "" if text == "-" else text
        context.user_data["state"] = ("abs_pos",)
        return await update.message.reply_text(
            "Теперь введите <b>должность</b> (или «-»):", parse_mode=HTML, reply_markup=cancel_kb
        )

    if kind == "abs_pos":
        if len(text) > 100:
            return await update.message.reply_text("Слишком длинная должность (максимум 100 символов).")
        new["position"] = "" if text == "-" else text
        context.user_data["state"] = ("abs_tg",)
        return await update.message.reply_text(
            "Теперь введите <b>адрес в Telegram</b>: @username или ссылку t.me/username (или «-»):",
            parse_mode=HTML, reply_markup=cancel_kb,
        )

    if kind == "abs_tg":
        if text == "-":
            tg = ""
        else:
            parts = text.split()
            tg = extract_username(parts[0]) if len(parts) == 1 else None
            if not tg:
                return await update.message.reply_text(
                    "Не похоже на username. Пришлите @username или ссылку t.me/username:",
                    reply_markup=cancel_kb,
                )
        try:
            run(
                "INSERT INTO absent_watch(email, project, position, telegram) VALUES(?,?,?,?)",
                new["email"], new.get("project", ""), new.get("position", ""), tg,
            )
        except sqlite3.IntegrityError:
            pass  # уже добавлен
        context.user_data.pop("state", None)
        context.user_data.pop("abs_new", None)
        return await show_absent_list(context, chat_id)

    context.user_data.pop("state", None)
    await show_absent_list(context, chat_id)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    state = context.user_data.get("state")

    if not state or get_role(user.id) not in MANAGERS:
        return await show_menu(context, chat_id, user.id)

    text = update.message.text.strip()
    kind = state[0]

    if kind.startswith("abs_"):
        if get_role(user.id) != "admin":
            context.user_data.pop("state", None)
            return await show_menu(context, chat_id, user.id)
        return await on_absent_text(update, context, state, text)

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

    if kind == "edit_docs":
        if text == "-":
            url = ""
        elif URL_RE.fullmatch(text) and len(text) <= 500:
            url = text
        else:
            return await update.message.reply_text(
                "Не похоже на ссылку. Она должна начинаться с https:// (или «-», чтобы убрать)."
            )
        run("UPDATE projects SET docs_url=? WHERE id=?", url, state[1])
        context.user_data.pop("state", None)
        return await show_project(context, chat_id, user.id, state[1])

    # ── часто задаваемые вопросы ──
    if kind == "faq_title":
        if len(text) > 60:
            return await update.message.reply_text("Слишком длинное название (максимум 60 символов).")
        run("UPDATE projects SET faq_title=? WHERE id=?", "" if text == "-" else text, state[1])
        context.user_data.pop("state", None)
        return await show_faq(context, chat_id, user.id, state[1])

    if kind == "faq_add_title":  # шаг 1: название кнопки
        if len(text) > 60:
            return await update.message.reply_text("Слишком длинное название (максимум 60 символов).")
        context.user_data["state"] = ("faq_add_desc", state[1], state[2], text)
        await update.message.reply_text(
            "Теперь отправьте <b>описание</b> (или «-», чтобы оставить пустым):", parse_mode=HTML
        )
        return

    if kind == "faq_add_desc":  # шаг 2: описание
        if len(text) > 3000:
            return await update.message.reply_text("Слишком длинное описание (максимум 3000 символов).")
        context.user_data["state"] = (
            "faq_add_url", state[1], state[2], state[3], "" if text == "-" else text,
        )
        await update.message.reply_text(
            "Теперь отправьте <b>ссылку на документацию</b> (https://...) "
            "или «-», чтобы пропустить:",
            parse_mode=HTML,
        )
        return

    if kind == "faq_add_url":  # шаг 3: ссылка
        url = parse_url(text)
        if url is None:
            return await update.message.reply_text(
                "Не похоже на ссылку. Она должна начинаться с https:// (или «-», чтобы пропустить)."
            )
        _, pid, parent, title, desc = state
        run(
            "INSERT INTO faq(project_id, parent_id, title, description, docs_url) VALUES(?,?,?,?,?)",
            pid, parent or None, title, desc, url,
        )
        context.user_data.pop("state", None)
        if parent:  # добавляли внутрь кнопки — остаёмся на её странице
            return await show_faq_item(context, chat_id, user.id, parent)
        return await show_faq(context, chat_id, user.id, pid)

    if kind == "faq_edit":  # изменение одного поля существующей кнопки
        _, fid, field = state
        if field == "title":
            if len(text) > 60:
                return await update.message.reply_text("Слишком длинное название (максимум 60 символов).")
            run("UPDATE faq SET title=? WHERE id=?", text, fid)
        elif field == "description":
            if len(text) > 3000:
                return await update.message.reply_text("Слишком длинное описание (максимум 3000 символов).")
            run("UPDATE faq SET description=? WHERE id=?", "" if text == "-" else text, fid)
        elif field == "docs_url":
            url = parse_url(text)
            if url is None:
                return await update.message.reply_text(
                    "Не похоже на ссылку. Она должна начинаться с https:// (или «-», чтобы убрать)."
                )
            run("UPDATE faq SET docs_url=? WHERE id=?", url, fid)
        context.user_data.pop("state", None)
        return await show_faq_item(context, chat_id, user.id, fid)

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


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Файл копии от администратора для восстановления."""
    user = update.effective_user
    doc = update.message.document
    if get_role(user.id) == "admin" and context.user_data.get("state") == ("abs_curl",):
        # cURL с токенами Hurma, присланный файлом .txt
        context.user_data.pop("state", None)
        if doc.file_size and doc.file_size > 1024 * 1024:
            return await update.message.reply_text("Файл слишком большой (максимум 1 МБ).")
        tg_file = await context.bot.get_file(doc.file_id)
        raw = bytes(await tg_file.download_as_bytearray())
        try:
            await update.message.delete()
        except TelegramError:
            pass
        note = apply_curl(raw.decode("utf-8", "replace"))
        return await show_absent_cfg(context, update.effective_chat.id, note=note)
    if get_role(user.id) == "admin" and context.user_data.get("state") == ("restore_file",):
        context.user_data.pop("state", None)
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            return await update.message.reply_text("Файл слишком большой (максимум 20 МБ).")
        tg_file = await context.bot.get_file(doc.file_id)
        path = Path(tempfile.mkdtemp()) / "upload.db"
        await tg_file.download_to_drive(path)
        err = validate_backup(path)
        if err:
            return await update.message.reply_text(f"❌ {err}")
        label = f"файл {doc.file_name or 'без имени'}"
        context.user_data["restore"] = (str(path), "file", label)
        return await update.message.reply_text(
            restore_confirm_text(path, label),
            parse_mode=HTML,
            reply_markup=confirm_kb("rs_yes", "rs"),
        )
    await show_menu(context, update.effective_chat.id, user.id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        # Второй экземпляр бота с этим токеном (обычно — старый деплой на пару секунд).
        # Опрос продолжится сам, когда он остановится, поэтому без traceback.
        log.warning("Conflict: с этим токеном ещё работает другой экземпляр бота")
        return
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


async def post_init(app: Application) -> None:
    """Запускается один раз до начала опроса Telegram."""
    try:
        await restore_on_startup(app.bot)
    except Exception:
        log.exception("Автовосстановление не удалось")
    if AUTO_BACKUP:
        app.bot_data["backup_task"] = asyncio.create_task(backup_loop(app.bot))
        log.info("Автокопии включены: после изменений данных копия уходит админам и закрепляется")
    app.bot_data["absent_task"] = asyncio.create_task(absent_loop(app.bot))
    log.info("Отсутствующие: автообновление в %s (Киев)", fmt_times(get_absent_times()))


def main():
    if not ADMIN_IDS:
        raise SystemExit("Задайте переменную окружения ADMIN_IDS (Telegram ID администратора).")
    start_health_server()
    start_keepalive()

    private = filters.ChatType.PRIVATE
    group_mentions = (
        filters.ChatType.GROUPS
        & filters.UpdateType.MESSAGE  # отредактированные сообщения не трогаем
        & (
            filters.Entity("mention") | filters.Entity("text_mention")
            | filters.CaptionEntity("mention") | filters.CaptionEntity("text_mention")
        )
    )
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "menu"], cmd_start, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(group_mentions, on_group_message))
    app.add_handler(MessageHandler(filters.PHOTO & private, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL & private, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, on_text))
    app.add_error_handler(on_error)

    log.info("Бот запущен. БД: %s. Админы: %s", DB_PATH, sorted(ADMIN_IDS))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()