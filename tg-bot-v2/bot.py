import html
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime
from functools import wraps

import telebot
from telebot import types
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────
# Конфигурация и логирование
# ─────────────────────────────────────────────────────────────────────────

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Создайте .env на основе .env.example")

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
if not ADMIN_IDS:
    raise RuntimeError("Не заданы ADMIN_IDS в .env")

DB_PATH = os.getenv("DB_PATH", "real_estate.db")
OBJECTS_PER_PAGE = 3
MAX_TELEGRAM_MESSAGE = 3500  # с запасом от лимита в 4096 символов

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("real_estate_bot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# chat_id -> [message_id, ...] — какие сообщения бота можно потом удалить
bot_messages: dict[int, list[int]] = {}
# in-memory кэш состояния анкеты/добавления объекта (дублируется в БД, см. ниже)
user_states: dict[int, str] = {}
user_data: dict[int, dict] = {}


# ─────────────────────────────────────────────────────────────────────────
# Работа с БД
# ─────────────────────────────────────────────────────────────────────────

import sqlite3


@contextmanager
def get_db():
    """Соединение с БД на каждый вызов — безопасно для многопоточного polling."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")  # меньше блокировок при параллельных запросах
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS properties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                price_per_sqm INTEGER,
                location TEXT,
                area REAL,
                rooms INTEGER,
                floor INTEGER,
                total_floors INTEGER,
                photos TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                phone TEXT,
                contact_method TEXT,
                contact_time TEXT,
                budget TEXT,
                property_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Сохранённое состояние анкеты/добавления объекта — переживает рестарт бота
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                chat_id INTEGER PRIMARY KEY,
                state TEXT,
                data TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def load_sessions():
    """Восстановить незавершённые анкеты после рестарта бота."""
    with get_db() as conn:
        rows = conn.execute("SELECT chat_id, state, data FROM sessions").fetchall()
    for row in rows:
        user_states[row["chat_id"]] = row["state"]
        user_data[row["chat_id"]] = json.loads(row["data"]) if row["data"] else {}
    if rows:
        logger.info("Восстановлено %d незавершённых сессий", len(rows))


def _persist_session(chat_id):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO sessions (chat_id, state, data, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                state = excluded.state,
                data = excluded.data,
                updated_at = CURRENT_TIMESTAMP
            """,
            (chat_id, user_states.get(chat_id), json.dumps(user_data.get(chat_id, {}))),
        )


def set_state(chat_id, state):
    user_states[chat_id] = state
    _persist_session(chat_id)


def set_data(chat_id, key, value):
    user_data.setdefault(chat_id, {})[key] = value
    _persist_session(chat_id)


def append_photo(chat_id, file_id):
    user_data.setdefault(chat_id, {}).setdefault("photos", []).append(file_id)
    _persist_session(chat_id)


def clear_session(chat_id):
    user_states.pop(chat_id, None)
    user_data.pop(chat_id, None)
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))


# ─────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def esc(value) -> str:
    """Экранирование пользовательского текста перед вставкой в HTML-сообщение."""
    return html.escape(str(value)) if value not in (None, "") else ""


def fmt_price(price):
    return f"{price:,} руб/м²".replace(",", " ") if price else "цена не указана"


def delete_all_bot_messages(chat_id):
    for msg_id in bot_messages.get(chat_id, []):
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass  # сообщение могло быть уже удалено пользователем — это нормально
    bot_messages[chat_id] = []


def send_message(chat_id, text, reply_markup=None, photo=None):
    try:
        if photo:
            msg = bot.send_photo(chat_id, photo, caption=text or None, reply_markup=reply_markup)
        else:
            msg = bot.send_message(chat_id, text, reply_markup=reply_markup)
        bot_messages.setdefault(chat_id, []).append(msg.message_id)
        return msg
    except Exception as e:
        logger.exception("Ошибка отправки сообщения в чат %s: %s", chat_id, e)
        return None


def send_long_text(chat_id, header, lines, reply_markup=None):
    """Отправляет длинный список, разбивая его на несколько сообщений по лимиту Telegram."""
    chunks = []
    current = header
    for line in lines:
        if len(current) + len(line) > MAX_TELEGRAM_MESSAGE:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [header]

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        send_message(chat_id, chunk, reply_markup=reply_markup if is_last else None)


def safe_handler(func):
    """Ловит необработанные исключения, чтобы одна сломанная заявка не роняла бота."""

    @wraps(func)
    def wrapper(update, *args, **kwargs):
        chat_id = getattr(getattr(update, "message", update), "chat", None)
        chat_id = chat_id.id if chat_id else getattr(update, "chat", None) and update.chat.id
        try:
            return func(update, *args, **kwargs)
        except Exception as e:
            logger.exception("Ошибка в обработчике %s: %s", func.__name__, e)
            if chat_id:
                try:
                    bot.send_message(
                        chat_id,
                        "⚠️ Произошла ошибка. Попробуйте ещё раз или отправьте /start заново.",
                    )
                except Exception:
                    pass

    return wrapper


def require_admin(func):
    """Проверка прав администратора для callback-обработчиков."""

    @wraps(func)
    def wrapper(call, *args, **kwargs):
        if not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "Недостаточно прав", show_alert=True)
            return
        return func(call, *args, **kwargs)

    return wrapper


TYPE_NAMES = {
    "new_building": "Новостройка",
    "secondary": "Вторичное жильё",
    "house": "Дом",
    "land": "Земельный участок",
    "commercial": "Коммерческая недвижимость",
}
TYPE_NAMES_PLURAL = {
    "new_building": "Новостройки",
    "secondary": "Вторичное жильё",
    "house": "Дома",
    "land": "Земельные участки",
    "commercial": "Коммерческая недвижимость",
}
TYPE_SHORT = {
    "new_building": "newbuilding",
    "secondary": "secondary",
    "house": "house",
    "land": "land",
    "commercial": "commercial",
}
TYPE_SHORT_REVERSE = {v: k for k, v in TYPE_SHORT.items()}


# ─────────────────────────────────────────────────────────────────────────
# Клавиатуры
# ─────────────────────────────────────────────────────────────────────────

def get_main_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("🏠 Каталог недвижимости", callback_data="catalog"),
        types.InlineKeyboardButton("📝 Оставить заявку", callback_data="survey_start"),
        types.InlineKeyboardButton("📞 Связаться с менеджером", callback_data="contact"),
        types.InlineKeyboardButton("ℹ️ О нас", callback_data="about"),
    )
    return keyboard


def get_catalog_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for type_key, name in TYPE_NAMES_PLURAL.items():
        keyboard.add(
            types.InlineKeyboardButton(name, callback_data=f"cat_{TYPE_SHORT[type_key]}_0")
        )
    keyboard.add(types.InlineKeyboardButton("⬅️ В главное меню", callback_data="main_menu"))
    return keyboard


def get_admin_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("➕ Добавить объект", callback_data="admin_add"),
        types.InlineKeyboardButton("📋 Список объектов", callback_data="admin_list"),
        types.InlineKeyboardButton("📨 Заявки", callback_data="admin_requests"),
        types.InlineKeyboardButton("🗑 Удалить объект", callback_data="admin_delete"),
        types.InlineKeyboardButton("⬅️ Выйти из админ-панели", callback_data="main_menu"),
    )
    return keyboard


def cancel_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow"))
    return keyboard


# ─────────────────────────────────────────────────────────────────────────
# Общие команды
# ─────────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
@safe_handler
def cmd_start(message):
    clear_session(message.chat.id)
    delete_all_bot_messages(message.chat.id)
    send_message(
        message.chat.id,
        "Добро пожаловать в агентство недвижимости!\n\n"
        "Здесь вы можете:\n"
        "• Просмотреть каталог объектов\n"
        "• Оставить заявку на подбор\n"
        "• Связаться с менеджером\n\n"
        "Выберите действие:",
        reply_markup=get_main_keyboard(),
    )


@bot.message_handler(commands=["cancel"])
@safe_handler
def cmd_cancel(message):
    had_state = user_states.get(message.chat.id) is not None
    clear_session(message.chat.id)
    delete_all_bot_messages(message.chat.id)
    text = "Действие отменено." if had_state else "Нечего отменять."
    send_message(message.chat.id, f"{text}\nВыберите действие:", reply_markup=get_main_keyboard())


@bot.message_handler(commands=["admin"])
@safe_handler
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "У вас нет доступа к админ-панели")
        return
    delete_all_bot_messages(message.chat.id)
    send_message(message.chat.id, "Админ-панель\nВыберите действие:", reply_markup=get_admin_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "cancel_flow")
@safe_handler
def cancel_flow(call):
    clear_session(call.message.chat.id)
    delete_all_bot_messages(call.message.chat.id)
    send_message(call.message.chat.id, "Действие отменено.\nВыберите действие:", reply_markup=get_main_keyboard())
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "main_menu")
@safe_handler
def back_to_main(call):
    clear_session(call.message.chat.id)
    delete_all_bot_messages(call.message.chat.id)
    send_message(call.message.chat.id, "Главное меню\nВыберите действие:", reply_markup=get_main_keyboard())
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "contact")
@safe_handler
def contact_manager(call):
    delete_all_bot_messages(call.message.chat.id)
    text = (
        "Наши контакты:\n\n"
        "Телефон: +7 (XXX) XXX-XX-XX\n"
        "Email: info@realestate.ru\n"
        "Часы работы: Пн-Пт 9:00-20:00, Сб-Вс 10:00-18:00\n\n"
        "Или оставьте заявку, и мы перезвоним вам!"
    )
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("📝 Оставить заявку", callback_data="survey_start"),
        types.InlineKeyboardButton("⬅️ В главное меню", callback_data="main_menu"),
    )
    send_message(call.message.chat.id, text, reply_markup=keyboard)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "about")
@safe_handler
def about_us(call):
    delete_all_bot_messages(call.message.chat.id)
    text = (
        "<b>Агентство недвижимости</b>\n\n"
        "Мы работаем на рынке недвижимости более 10 лет.\n"
        "Помогаем нашим клиентам найти идеальное жильё!\n\n"
        "Более 1000 успешных сделок\n"
        "Профессиональные риелторы\n"
        "Юридическая поддержка\n"
        "Помощь с ипотекой\n\n"
        "Доверьте нам поиск вашей идеальной недвижимости!"
    )
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("⬅️ В главное меню", callback_data="main_menu"))
    send_message(call.message.chat.id, text, reply_markup=keyboard)
    bot.answer_callback_query(call.id)


# ─────────────────────────────────────────────────────────────────────────
# Каталог
# ─────────────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data == "catalog")
@safe_handler
def show_catalog(call):
    delete_all_bot_messages(call.message.chat.id)
    send_message(call.message.chat.id, "Выберите тип недвижимости:", reply_markup=get_catalog_keyboard())
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_"))
@safe_handler
def show_properties_by_type(call):
    parts = call.data.split("_")
    property_type = TYPE_SHORT_REVERSE.get(parts[1], parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0

    with get_db() as conn:
        total_count = conn.execute(
            "SELECT COUNT(*) FROM properties WHERE type = ? AND is_active = 1", (property_type,)
        ).fetchone()[0]

        offset = page * OBJECTS_PER_PAGE
        properties = conn.execute(
            """SELECT id, title, price_per_sqm, location, area, photos
               FROM properties WHERE type = ? AND is_active = 1
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (property_type, OBJECTS_PER_PAGE, offset),
        ).fetchall()

    delete_all_bot_messages(call.message.chat.id)

    if not properties:
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("⬅️ Назад к каталогу", callback_data="catalog"))
        send_message(
            call.message.chat.id,
            f'В категории "{TYPE_NAMES_PLURAL.get(property_type, property_type)}" пока нет объектов',
            reply_markup=keyboard,
        )
        bot.answer_callback_query(call.id)
        return

    for prop in properties:
        text = f"<b>{esc(prop['title'])}</b>\n"
        text += f"Цена: {fmt_price(prop['price_per_sqm'])}\n"
        if prop["location"]:
            text += f"Локация: {esc(prop['location'])}\n"
        if prop["area"]:
            text += f"Площадь: {prop['area']} м²\n"

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("Подробнее →", callback_data=f"view_{prop['id']}"))

        photo_id = None
        if prop["photos"]:
            photo_id = prop["photos"].split(",")[0]

        if photo_id:
            send_message(call.message.chat.id, text, reply_markup=keyboard, photo=photo_id)
        else:
            send_message(call.message.chat.id, text, reply_markup=keyboard)

    total_pages = max(1, (total_count + OBJECTS_PER_PAGE - 1) // OBJECTS_PER_PAGE)
    type_short = TYPE_SHORT[property_type]
    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(
            types.InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"cat_{type_short}_{page - 1}")
        )
    if page < total_pages - 1:
        pagination_buttons.append(
            types.InlineKeyboardButton("Следующая ➡️", callback_data=f"cat_{type_short}_{page + 1}")
        )
    pagination_keyboard = types.InlineKeyboardMarkup(row_width=2)
    if pagination_buttons:
        pagination_keyboard.add(*pagination_buttons)
    pagination_keyboard.add(types.InlineKeyboardButton("⬅️ К каталогу", callback_data="catalog"))

    send_message(call.message.chat.id, f"Страница {page + 1} из {total_pages}", reply_markup=pagination_keyboard)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("view_"))
@safe_handler
def view_property(call):
    property_id = int(call.data.split("_")[1])

    with get_db() as conn:
        prop = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()

    if not prop:
        bot.answer_callback_query(call.id, "Объект не найден", show_alert=True)
        return

    text = f"{TYPE_NAMES.get(prop['type'], prop['type'])}\n"
    text += f"<b>{esc(prop['title'])}</b>\n\n"
    text += f"Цена: {fmt_price(prop['price_per_sqm'])}\n"
    if prop["price_per_sqm"] and prop["area"]:
        total_price = prop["price_per_sqm"] * prop["area"]
        text += f"Общая стоимость: {total_price:,.0f} руб.\n".replace(",", " ")
    if prop["location"]:
        text += f"Локация: {esc(prop['location'])}\n"
    if prop["area"]:
        text += f"Площадь: {prop['area']} м²\n"
    if prop["rooms"]:
        text += f"Комнат: {prop['rooms']}\n"
    if prop["floor"] and prop["total_floors"]:
        text += f"Этаж: {prop['floor']}/{prop['total_floors']}\n"
    if prop["description"]:
        text += f"\nОписание:\n{esc(prop['description'])}\n"

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("📝 Оставить заявку", callback_data="survey_start"),
        types.InlineKeyboardButton("⬅️ Назад", callback_data="catalog"),
    )

    delete_all_bot_messages(call.message.chat.id)

    photo_ids = prop["photos"].split(",") if prop["photos"] else []
    if photo_ids:
        send_message(call.message.chat.id, text, reply_markup=keyboard, photo=photo_ids[0])
        for extra_id in photo_ids[1:]:
            send_message(call.message.chat.id, "", photo=extra_id)
    else:
        send_message(call.message.chat.id, text, reply_markup=keyboard)

    bot.answer_callback_query(call.id)


# ─────────────────────────────────────────────────────────────────────────
# Анкета клиента
# ─────────────────────────────────────────────────────────────────────────

PHONE_RE = re.compile(r"^[\d\s()+\-]{5,20}$")


@bot.callback_query_handler(func=lambda call: call.data == "survey_start")
@safe_handler
def survey_start(call):
    delete_all_bot_messages(call.message.chat.id)
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for type_key, name in TYPE_NAMES_PLURAL.items():
        keyboard.add(types.InlineKeyboardButton(name, callback_data=f"service_{type_key}"))
    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow"))

    send_message(
        call.message.chat.id,
        "<b>Анкета подбора недвижимости</b>\n\nКакую недвижимость вы хотите приобрести?",
        reply_markup=keyboard,
    )
    set_state(call.message.chat.id, "waiting_for_service")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("service_") and user_states.get(call.message.chat.id) == "waiting_for_service"
)
@safe_handler
def process_service(call):
    type_key = call.data[len("service_"):]
    service = TYPE_NAMES_PLURAL.get(type_key)
    if not service:
        bot.answer_callback_query(call.id, "Неизвестный тип", show_alert=True)
        return

    set_data(call.message.chat.id, "service", service)
    delete_all_bot_messages(call.message.chat.id)
    send_message(
        call.message.chat.id,
        "Пожалуйста, ответьте на несколько вопросов.\nВ любой момент можно отправить /cancel.\n\n<b>1. Ваше ФИО:</b>",
        reply_markup=cancel_keyboard(),
    )
    set_state(call.message.chat.id, "waiting_for_name")
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_for_name")
@safe_handler
def process_name(message):
    name = message.text.strip()
    if not (2 <= len(name) <= 100):
        bot.send_message(message.chat.id, "Пожалуйста, введите корректное ФИО (2–100 символов):")
        return
    set_data(message.chat.id, "full_name", name)
    send_message(message.chat.id, "<b>2. Номер телефона:</b>", reply_markup=cancel_keyboard())
    set_state(message.chat.id, "waiting_for_phone")


@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_for_phone")
@safe_handler
def process_phone(message):
    phone = message.text.strip()
    if not PHONE_RE.match(phone):
        bot.send_message(message.chat.id, "Похоже, номер введён некорректно. Попробуйте ещё раз, например: +7 900 123-45-67")
        return
    set_data(message.chat.id, "phone", phone)

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("WhatsApp", callback_data="contact_whatsapp"),
        types.InlineKeyboardButton("Telegram", callback_data="contact_telegram"),
        types.InlineKeyboardButton("Instagram", callback_data="contact_instagram"),
        types.InlineKeyboardButton("Звонок", callback_data="contact_call"),
    )
    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow"))
    send_message(message.chat.id, "<b>3. Какой способ связи вам удобен?</b>", reply_markup=keyboard)
    set_state(message.chat.id, "waiting_for_contact_method")


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("contact_")
    and user_states.get(call.message.chat.id) == "waiting_for_contact_method"
)
@safe_handler
def process_contact_method(call):
    contact_map = {
        "contact_whatsapp": "WhatsApp",
        "contact_telegram": "Telegram",
        "contact_instagram": "Instagram",
        "contact_call": "Звонок",
    }
    contact_method = contact_map.get(call.data)
    set_data(call.message.chat.id, "contact_method", contact_method)
    delete_all_bot_messages(call.message.chat.id)

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Сейчас", callback_data="time_now"),
        types.InlineKeyboardButton("В течение дня", callback_data="time_day"),
        types.InlineKeyboardButton("Вечером", callback_data="time_evening"),
        types.InlineKeyboardButton("Завтра", callback_data="time_tomorrow"),
        types.InlineKeyboardButton("Укажу своё время", callback_data="time_custom"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow"),
    )
    send_message(call.message.chat.id, "<b>4. Когда вам удобно связаться?</b>", reply_markup=keyboard)
    set_state(call.message.chat.id, "waiting_for_contact_time")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("time_")
    and user_states.get(call.message.chat.id) == "waiting_for_contact_time"
)
@safe_handler
def process_contact_time(call):
    time_map = {
        "time_now": "Сейчас",
        "time_day": "В течение дня",
        "time_evening": "Вечером",
        "time_tomorrow": "Завтра",
    }
    delete_all_bot_messages(call.message.chat.id)

    if call.data == "time_custom":
        send_message(call.message.chat.id, "Пожалуйста, укажите удобное для вас время:", reply_markup=cancel_keyboard())
        set_state(call.message.chat.id, "waiting_for_custom_time")
        bot.answer_callback_query(call.id)
        return

    set_data(call.message.chat.id, "contact_time", time_map.get(call.data))
    _ask_budget(call.message.chat.id)
    bot.answer_callback_query(call.id)


def _ask_budget(chat_id):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("Пропустить", callback_data="skip_budget"))
    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow"))
    send_message(
        chat_id,
        "<b>5. Ваш ориентировочный бюджет / первоначальный взнос (по желанию):</b>\n"
        "Напишите сумму или нажмите кнопку «Пропустить»",
        reply_markup=keyboard,
    )
    set_state(chat_id, "waiting_for_budget")


@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_for_custom_time")
@safe_handler
def process_custom_time(message):
    set_data(message.chat.id, "contact_time", message.text.strip())
    _ask_budget(message.chat.id)


@bot.callback_query_handler(
    func=lambda call: call.data == "skip_budget" and user_states.get(call.message.chat.id) == "waiting_for_budget"
)
@safe_handler
def skip_budget(call):
    set_data(call.message.chat.id, "budget", "Не указан")
    delete_all_bot_messages(call.message.chat.id)
    _ask_property_wishes(call.message.chat.id)
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_for_budget")
@safe_handler
def process_budget(message):
    set_data(message.chat.id, "budget", message.text.strip())
    _ask_property_wishes(message.chat.id)


def _ask_property_wishes(chat_id):
    send_message(
        chat_id,
        "<b>6. Что бы вы хотели приобрести?</b>\nОпишите ваши пожелания (район, площадь, количество комнат и т.д.)",
        reply_markup=cancel_keyboard(),
    )
    set_state(chat_id, "waiting_for_property_type")


@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_for_property_type")
@safe_handler
def process_property_type(message):
    set_data(message.chat.id, "property_type", message.text.strip())
    data = user_data.get(message.chat.id, {})

    with get_db() as conn:
        conn.execute(
            """INSERT INTO requests
               (user_id, username, full_name, phone, contact_method, contact_time, budget, property_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.from_user.id,
                message.from_user.username,
                data.get("full_name"),
                data.get("phone"),
                data.get("contact_method"),
                data.get("contact_time"),
                data.get("budget"),
                data.get("property_type"),
            ),
        )

    admin_text = (
        "<b>Новая заявка!</b>\n\n"
        f"ФИО: {esc(data.get('full_name'))}\n"
        f"Телефон: {esc(data.get('phone'))}\n"
        f"Способ связи: {esc(data.get('contact_method'))}\n"
        f"Время связи: {esc(data.get('contact_time'))}\n"
        f"Бюджет: {esc(data.get('budget'))}\n"
        f"Услуга: {esc(data.get('service'))}\n"
        f"Пожелания: {esc(data.get('property_type'))}\n"
    )
    if message.from_user.username:
        admin_text += f"Telegram: @{esc(message.from_user.username)}"

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, admin_text)
        except Exception as e:
            logger.warning("Не удалось отправить заявку админу %s: %s", admin_id, e)

    delete_all_bot_messages(message.chat.id)
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("⬅️ В главное меню", callback_data="main_menu"))
    send_message(
        message.chat.id,
        "<b>Спасибо! Ваша заявка принята!</b>\n\nНаш менеджер свяжется с вами в ближайшее время.",
        reply_markup=keyboard,
    )
    clear_session(message.chat.id)


# ─────────────────────────────────────────────────────────────────────────
# Админ-панель
# ─────────────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data == "admin_menu")
@require_admin
@safe_handler
def admin_menu(call):
    clear_session(call.message.chat.id)
    delete_all_bot_messages(call.message.chat.id)
    send_message(call.message.chat.id, "Админ-панель\nВыберите действие:", reply_markup=get_admin_keyboard())
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_add")
@require_admin
@safe_handler
def add_property_start(call):
    delete_all_bot_messages(call.message.chat.id)
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for type_key, name in TYPE_NAMES_PLURAL.items():
        keyboard.add(types.InlineKeyboardButton(name, callback_data=f"add_{type_key}"))
    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="admin_menu"))
    send_message(call.message.chat.id, "Выберите тип объекта:", reply_markup=keyboard)
    set_state(call.message.chat.id, "admin_waiting_for_type")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("add_") and user_states.get(call.message.chat.id) == "admin_waiting_for_type"
)
@require_admin
@safe_handler
def process_add_type(call):
    type_key = call.data[len("add_"):]
    if type_key not in TYPE_NAMES:
        bot.answer_callback_query(call.id, "Неизвестный тип", show_alert=True)
        return
    set_data(call.message.chat.id, "type", type_key)
    delete_all_bot_messages(call.message.chat.id)
    send_message(call.message.chat.id, "Введите название объекта:", reply_markup=cancel_keyboard())
    set_state(call.message.chat.id, "admin_waiting_for_title")
    bot.answer_callback_query(call.id)


def _admin_step(state_name):
    """Общий фильтр для шагов админской формы: нужное состояние + права админа."""

    def predicate(message):
        return user_states.get(message.chat.id) == state_name and is_admin(message.from_user.id)

    return predicate


@bot.message_handler(func=_admin_step("admin_waiting_for_title"))
@safe_handler
def process_title(message):
    title = message.text.strip()
    if not title:
        bot.send_message(message.chat.id, "Название не может быть пустым. Введите название объекта:")
        return
    set_data(message.chat.id, "title", title)
    bot.send_message(message.chat.id, "Введите описание объекта:")
    set_state(message.chat.id, "admin_waiting_for_description")


@bot.message_handler(func=_admin_step("admin_waiting_for_description"))
@safe_handler
def process_description(message):
    set_data(message.chat.id, "description", message.text.strip())
    bot.send_message(message.chat.id, "Введите цену за квадратный метр (в рублях, только цифры):")
    set_state(message.chat.id, "admin_waiting_for_price")


@bot.message_handler(func=_admin_step("admin_waiting_for_price"))
@safe_handler
def process_price(message):
    try:
        price_per_sqm = int(message.text.replace(" ", ""))
        if price_per_sqm <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректную цену (положительное число)")
        return
    set_data(message.chat.id, "price_per_sqm", price_per_sqm)
    bot.send_message(message.chat.id, "Введите локацию (адрес, район):")
    set_state(message.chat.id, "admin_waiting_for_location")


@bot.message_handler(func=_admin_step("admin_waiting_for_location"))
@safe_handler
def process_location(message):
    set_data(message.chat.id, "location", message.text.strip())
    bot.send_message(message.chat.id, "Введите площадь (в м²):")
    set_state(message.chat.id, "admin_waiting_for_area")


@bot.message_handler(func=_admin_step("admin_waiting_for_area"))
@safe_handler
def process_area(message):
    try:
        area = float(message.text.replace(",", "."))
        if area <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректную площадь (положительное число)")
        return
    set_data(message.chat.id, "area", area)
    bot.send_message(message.chat.id, "Введите количество комнат (если неприменимо, введите 0):")
    set_state(message.chat.id, "admin_waiting_for_rooms")


@bot.message_handler(func=_admin_step("admin_waiting_for_rooms"))
@safe_handler
def process_rooms(message):
    try:
        rooms = int(message.text)
        if rooms < 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректное число (0 или больше)")
        return
    set_data(message.chat.id, "rooms", rooms)
    bot.send_message(message.chat.id, "Введите этаж (если неприменимо, введите 0):")
    set_state(message.chat.id, "admin_waiting_for_floor")


@bot.message_handler(func=_admin_step("admin_waiting_for_floor"))
@safe_handler
def process_floor(message):
    try:
        floor = int(message.text)
        if floor < 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректное число (0 или больше)")
        return
    set_data(message.chat.id, "floor", floor)
    bot.send_message(message.chat.id, "Введите общее количество этажей (если неприменимо, введите 0):")
    set_state(message.chat.id, "admin_waiting_for_total_floors")


@bot.message_handler(func=_admin_step("admin_waiting_for_total_floors"))
@safe_handler
def process_total_floors(message):
    try:
        total_floors = int(message.text)
        if total_floors < 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректное число (0 или больше)")
        return
    set_data(message.chat.id, "total_floors", total_floors)
    set_data(message.chat.id, "photos", [])

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("✅ Завершить добавление", callback_data="finish_photos"))
    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="admin_menu"))
    bot.send_message(
        message.chat.id,
        "<b>Отправьте фотографии объекта</b>\n\nПросто отправляйте фото по одному.\nКогда закончите, нажмите кнопку ниже.",
        reply_markup=keyboard,
    )
    set_state(message.chat.id, "admin_waiting_for_photos")


@bot.message_handler(content_types=["photo"], func=_admin_step("admin_waiting_for_photos"))
@safe_handler
def process_photo(message):
    # Сохраняем только file_id — Telegram сам хранит файл, локальный диск не нужен
    file_id = message.photo[-1].file_id
    append_photo(message.chat.id, file_id)
    count = len(user_data[message.chat.id]["photos"])
    bot.send_message(message.chat.id, f"Фото {count} добавлено! Отправьте ещё или завершите добавление.")


@bot.callback_query_handler(
    func=lambda call: call.data == "finish_photos" and user_states.get(call.message.chat.id) == "admin_waiting_for_photos"
)
@require_admin
@safe_handler
def finish_photos(call):
    data = user_data.get(call.message.chat.id, {})
    photos = data.get("photos", [])
    photos_str = ",".join(photos) if photos else ""

    with get_db() as conn:
        conn.execute(
            """INSERT INTO properties
               (type, title, description, price_per_sqm, location, area, rooms, floor, total_floors, photos)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("type"), data.get("title"), data.get("description"), data.get("price_per_sqm"),
                data.get("location"), data.get("area"), data.get("rooms"), data.get("floor"),
                data.get("total_floors"), photos_str,
            ),
        )

    delete_all_bot_messages(call.message.chat.id)
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin_menu"))
    send_message(call.message.chat.id, f"Объект успешно добавлен!\nФотографий: {len(photos)}", reply_markup=keyboard)
    clear_session(call.message.chat.id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_list")
@require_admin
@safe_handler
def list_properties(call):
    delete_all_bot_messages(call.message.chat.id)
    with get_db() as conn:
        properties = conn.execute(
            "SELECT id, title, price_per_sqm, is_active FROM properties ORDER BY created_at DESC"
        ).fetchall()

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin_menu"))

    if not properties:
        send_message(call.message.chat.id, "Список объектов пуст", reply_markup=keyboard)
        bot.answer_callback_query(call.id)
        return

    lines = []
    for prop in properties:
        status = "✅ Активен" if prop["is_active"] else "⛔ Неактивен"
        lines.append(f"{status} | ID {prop['id']} | {esc(prop['title'])} | {fmt_price(prop['price_per_sqm'])}\n")

    send_long_text(call.message.chat.id, "<b>Список объектов:</b>\n\n", lines, reply_markup=keyboard)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_requests")
@require_admin
@safe_handler
def show_requests(call):
    delete_all_bot_messages(call.message.chat.id)
    with get_db() as conn:
        requests = conn.execute(
            """SELECT id, full_name, phone, contact_method, contact_time, budget, property_type, created_at
               FROM requests ORDER BY created_at DESC LIMIT 20"""
        ).fetchall()

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin_menu"))

    if not requests:
        send_message(call.message.chat.id, "Заявок пока нет", reply_markup=keyboard)
        bot.answer_callback_query(call.id)
        return

    lines = []
    for req in requests:
        lines.append(
            f"<b>Заявка #{req['id']}</b>\n"
            f"ФИО: {esc(req['full_name'])}\n"
            f"Телефон: {esc(req['phone'])}\n"
            f"Способ связи: {esc(req['contact_method'])}\n"
            f"Время: {esc(req['contact_time'])}\n"
            f"Бюджет: {esc(req['budget'])}\n"
            f"Ищет: {esc(req['property_type'])}\n"
            f"Дата: {req['created_at']}\n\n"
        )

    send_long_text(call.message.chat.id, "<b>Последние заявки:</b>\n\n", lines, reply_markup=keyboard)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_delete")
@require_admin
@safe_handler
def delete_property_start(call):
    delete_all_bot_messages(call.message.chat.id)
    with get_db() as conn:
        properties = conn.execute("SELECT id, title FROM properties WHERE is_active = 1").fetchall()

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin_menu"))

    if not properties:
        send_message(call.message.chat.id, "Нет активных объектов для удаления", reply_markup=keyboard)
        bot.answer_callback_query(call.id)
        return

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for prop in properties[:20]:
        keyboard.add(
            types.InlineKeyboardButton(f"ID {prop['id']}: {prop['title'][:30]}", callback_data=f"delconfirm_{prop['id']}")
        )
    keyboard.add(types.InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin_menu"))

    send_message(call.message.chat.id, "Выберите объект для удаления:", reply_markup=keyboard)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("delconfirm_"))
@require_admin
@safe_handler
def confirm_delete_property(call):
    property_id = int(call.data.split("_")[1])
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"delete_{property_id}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="admin_delete"),
    )
    delete_all_bot_messages(call.message.chat.id)
    send_message(call.message.chat.id, f"Точно деактивировать объект ID {property_id}?", reply_markup=keyboard)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_"))
@require_admin
@safe_handler
def delete_property(call):
    property_id = int(call.data.split("_")[1])
    with get_db() as conn:
        conn.execute("UPDATE properties SET is_active = 0 WHERE id = ?", (property_id,))
    delete_all_bot_messages(call.message.chat.id)
    bot.answer_callback_query(call.id, f"Объект ID {property_id} деактивирован", show_alert=True)
    send_message(call.message.chat.id, "Админ-панель\nВыберите действие:", reply_markup=get_admin_keyboard())


# ─────────────────────────────────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    load_sessions()
    logger.info("Бот запущен")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
