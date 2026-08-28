import telebot
from telebot import types
import sqlite3
import os
from datetime import datetime

BOT_TOKEN = "7853881424:AAFuD8muefj3vaCIrajVl37Ge_OFh7tKAgY"
ADMIN_IDS = {8904021325, 7397553026}

PHOTOS_DIR = "photos"
if not os.path.exists(PHOTOS_DIR):
    os.makedirs(PHOTOS_DIR)

OBJECTS_PER_PAGE = 3

bot = telebot.TeleBot(BOT_TOKEN)

bot_messages = {}
user_states = {}
user_data = {}

def delete_all_bot_messages(chat_id):
    try:
        if chat_id in bot_messages:
            for msg_id in bot_messages[chat_id]:
                try:
                    bot.delete_message(chat_id, msg_id)
                except:
                    pass
            bot_messages[chat_id] = []
    except:
        pass

def send_message(chat_id, text, reply_markup=None, photo=None):
    try:
        if photo:
            msg = bot.send_photo(chat_id, photo, caption=text, reply_markup=reply_markup, parse_mode="HTML")
        else:
            msg = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")
        
        if chat_id not in bot_messages:
            bot_messages[chat_id] = []
        bot_messages[chat_id].append(msg.message_id)
        return msg
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        return None

def init_db():
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    cursor.execute('''
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
    ''')
    
    cursor.execute('''
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
    ''')
    
    conn.commit()
    conn.close()

def get_main_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Каталог недвижимости", callback_data="catalog"),
        types.InlineKeyboardButton("Оставить заявку", callback_data="survey_start"),
        types.InlineKeyboardButton("Связаться с менеджером", callback_data="contact"),
        types.InlineKeyboardButton("О нас", callback_data="about")
    )
    return keyboard

def get_catalog_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Новостройки", callback_data="cat_newbuilding_0"),
        types.InlineKeyboardButton("Вторичное жильё", callback_data="cat_secondary_0"),
        types.InlineKeyboardButton("Дома", callback_data="cat_house_0"),
        types.InlineKeyboardButton("Земельные участки", callback_data="cat_land_0"),
        types.InlineKeyboardButton("Коммерческая недвижимость", callback_data="cat_commercial_0"),
        types.InlineKeyboardButton("В главное меню", callback_data="main_menu")
    )
    return keyboard

def get_admin_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Добавить объект", callback_data="admin_add"),
        types.InlineKeyboardButton("Список объектов", callback_data="admin_list"),
        types.InlineKeyboardButton("Заявки", callback_data="admin_requests"),
        types.InlineKeyboardButton("Удалить объект", callback_data="admin_delete"),
        types.InlineKeyboardButton("Выйти из админ-панели", callback_data="main_menu")
    )
    return keyboard

@bot.message_handler(commands=['start'])
def cmd_start(message):
    delete_all_bot_messages(message.chat.id)
    send_message(
        message.chat.id,
        "Добро пожаловать в агентство недвижимости!\n\n"
        "Здесь вы можете:\n"
        "• Просмотреть каталог объектов\n"
        "• Оставить заявку на подбор\n"
        "• Связаться с менеджером\n\n"
        "Выберите действие:",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(commands=['admin'])
def cmd_admin(message):
    if message.from_user.id in ADMIN_IDS:
        delete_all_bot_messages(message.chat.id)
        send_message(
            message.chat.id,
            "Админ-панель\nВыберите действие:",
            reply_markup=get_admin_keyboard()
        )
    else:
        bot.send_message(message.chat.id, "У вас нет доступа к админ-панели")

@bot.callback_query_handler(func=lambda call: call.data == "main_menu")
def back_to_main(call):
    delete_all_bot_messages(call.message.chat.id)
    send_message(
        call.message.chat.id,
        "Главное меню\nВыберите действие:",
        reply_markup=get_main_keyboard()
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "catalog")
def show_catalog(call):
    delete_all_bot_messages(call.message.chat.id)
    send_message(
        call.message.chat.id,
        "Выберите тип недвижимости:",
        reply_markup=get_catalog_keyboard()
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_"))
def show_properties_by_type(call):
    parts = call.data.split("_")
    property_type = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0
    
    type_map = {
        "newbuilding": "new_building",
        "secondary": "secondary",
        "house": "house",
        "land": "land",
        "commercial": "commercial"
    }
    property_type = type_map.get(property_type, property_type)
    
    type_names = {
        "new_building": "Новостройки",
        "secondary": "Вторичное жильё",
        "house": "Дома",
        "land": "Земельные участки",
        "commercial": "Коммерческая недвижимость"
    }
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT COUNT(*) FROM properties WHERE type = ? AND is_active = 1",
        (property_type,)
    )
    total_count = cursor.fetchone()[0]
    
    offset = page * OBJECTS_PER_PAGE
    cursor.execute(
        "SELECT id, title, price_per_sqm, location, area, photos FROM properties WHERE type = ? AND is_active = 1 ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (property_type, OBJECTS_PER_PAGE, offset)
    )
    properties = cursor.fetchall()
    conn.close()
    
    if not properties:
        delete_all_bot_messages(call.message.chat.id)
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("Назад к каталогу", callback_data="catalog"))
        send_message(
            call.message.chat.id,
            f"В категории \"{type_names.get(property_type, property_type)}\" пока нет объектов",
            reply_markup=keyboard
        )
        bot.answer_callback_query(call.id)
        return
    
    delete_all_bot_messages(call.message.chat.id)
    
    for prop in properties:
        prop_id, title, price_per_sqm, location, area, photos = prop
        
        text = f"<b>{title}</b>\n"
        text += f"Цена: {price_per_sqm:,} руб/м²\n" if price_per_sqm else ""
        text += f"Локация: {location}\n" if location else ""
        text += f"Площадь: {area} м²\n" if area else ""
        
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton(f"{price_per_sqm:,} руб/м²", callback_data=f"view_{prop_id}"),
            types.InlineKeyboardButton("Выбрать объект", callback_data=f"view_{prop_id}")
        )
        
        if photos:
            photo_list = photos.split(',')
            if photo_list and os.path.exists(photo_list[0]):
                try:
                    with open(photo_list[0], 'rb') as photo_file:
                        send_message(
                            call.message.chat.id,
                            text,
                            reply_markup=keyboard,
                            photo=photo_file
                        )
                except:
                    send_message(call.message.chat.id, text, reply_markup=keyboard)
            else:
                send_message(call.message.chat.id, text, reply_markup=keyboard)
        else:
            send_message(call.message.chat.id, text, reply_markup=keyboard)
    
    total_pages = (total_count + OBJECTS_PER_PAGE - 1) // OBJECTS_PER_PAGE
    pagination_keyboard = types.InlineKeyboardMarkup(row_width=2)
    pagination_buttons = []
    
    type_short = "newbuilding" if property_type == "new_building" else property_type
    
    if page > 0:
        pagination_buttons.append(
            types.InlineKeyboardButton("Предыдущая", callback_data=f"cat_{type_short}_{page - 1}")
        )
    
    if page < total_pages - 1:
        pagination_buttons.append(
            types.InlineKeyboardButton("Следующая", callback_data=f"cat_{type_short}_{page + 1}")
        )
    
    if pagination_buttons:
        pagination_keyboard.add(*pagination_buttons)
    
    pagination_keyboard.add(types.InlineKeyboardButton("К каталогу", callback_data="catalog"))
    
    send_message(
        call.message.chat.id,
        f"Страница {page + 1} из {total_pages}",
        reply_markup=pagination_keyboard
    )
    
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_"))
def view_property(call):
    property_id = int(call.data.split("_")[1])
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM properties WHERE id = ?", (property_id,))
    prop = cursor.fetchone()
    conn.close()
    
    if not prop:
        bot.answer_callback_query(call.id, "Объект не найден", show_alert=True)
        return
    
    prop_id, prop_type, title, description, price_per_sqm, location, area, rooms, floor, total_floors, photos, created_at, is_active = prop
    
    type_names = {
        "new_building": "Новостройка",
        "secondary": "Вторичное жильё",
        "house": "Дом",
        "land": "Земельный участок",
        "commercial": "Коммерческая недвижимость"
    }
    
    text = f"{type_names.get(prop_type, prop_type)}\n"
    text += f"<b>{title}</b>\n\n"
    text += f"Цена: {price_per_sqm:,} руб/м²\n" if price_per_sqm else ""
    if price_per_sqm and area:
        total_price = price_per_sqm * area
        text += f"Общая стоимость: {total_price:,.0f} руб.\n"
    text += f"Локация: {location}\n" if location else ""
    text += f"Площадь: {area} м²\n" if area else ""
    text += f"Комнат: {rooms}\n" if rooms else ""
    if floor and total_floors:
        text += f"Этаж: {floor}/{total_floors}\n"
    text += f"\nОписание:\n{description}\n" if description else ""
    
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Оставить заявку", callback_data="survey_start"),
        types.InlineKeyboardButton("Назад", callback_data="catalog")
    )
    
    delete_all_bot_messages(call.message.chat.id)
    
    if photos:
        photo_list = photos.split(',')
        for i, photo_path in enumerate(photo_list):
            try:
                if os.path.exists(photo_path):
                    with open(photo_path, 'rb') as photo_file:
                        if i == 0:
                            send_message(
                                call.message.chat.id,
                                text,
                                reply_markup=keyboard,
                                photo=photo_file
                            )
                        else:
                            send_message(
                                call.message.chat.id,
                                "",
                                photo=photo_file
                            )
            except Exception as e:
                print(f"Ошибка отправки фото: {e}")
    else:
        send_message(call.message.chat.id, text, reply_markup=keyboard)
    
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "survey_start")
def survey_start(call):
    delete_all_bot_messages(call.message.chat.id)
    
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Новостройки", callback_data="service_new_building"),
        types.InlineKeyboardButton("Вторичное жильё", callback_data="service_secondary"),
        types.InlineKeyboardButton("Дома", callback_data="service_house"),
        types.InlineKeyboardButton("Земельные участки", callback_data="service_land"),
        types.InlineKeyboardButton("Коммерческая недвижимость", callback_data="service_commercial")
    )
    
    send_message(
        call.message.chat.id,
        "<b>Анкета подбора недвижимости</b>\n\n"
        "Какую недвижимость вы хотите приобрести?",
        reply_markup=keyboard
    )
    user_states[call.message.chat.id] = "waiting_for_service"
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("service_"))
def process_service(call):
    service_map = {
        "service_new_building": "Новостройки",
        "service_secondary": "Вторичное жильё",
        "service_house": "Дома",
        "service_land": "Земельные участки",
        "service_commercial": "Коммерческая недвижимость"
    }
    
    service = service_map.get(call.data)
    if call.message.chat.id not in user_data:
        user_data[call.message.chat.id] = {}
    user_data[call.message.chat.id]['service'] = service
    
    delete_all_bot_messages(call.message.chat.id)
    
    send_message(
        call.message.chat.id,
        "Пожалуйста, ответьте на несколько вопросов.\n\n"
        "<b>1. Ваше ФИО:</b>"
    )
    user_states[call.message.chat.id] = "waiting_for_name"
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_for_name")
def process_name(message):
    if message.chat.id not in user_data:
        user_data[message.chat.id] = {}
    user_data[message.chat.id]['full_name'] = message.text
    bot.send_message(message.chat.id, "<b>2. Номер телефона:</b>", parse_mode="HTML")
    user_states[message.chat.id] = "waiting_for_phone"

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_for_phone")
def process_phone(message):
    user_data[message.chat.id]['phone'] = message.text
    
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("WhatsApp", callback_data="contact_whatsapp"),
        types.InlineKeyboardButton("Telegram", callback_data="contact_telegram"),
        types.InlineKeyboardButton("Instagram", callback_data="contact_instagram"),
        types.InlineKeyboardButton("Звонок", callback_data="contact_call")
    )
    
    bot.send_message(
        message.chat.id,
        "<b>3. Какой способ связи вам удобен?</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    user_states[message.chat.id] = "waiting_for_contact_method"

@bot.callback_query_handler(func=lambda call: call.data.startswith("contact_"))
def process_contact_method(call):
    contact_map = {
        "contact_whatsapp": "WhatsApp",
        "contact_telegram": "Telegram",
        "contact_instagram": "Instagram",
        "contact_call": "Звонок"
    }
    
    contact_method = contact_map.get(call.data)
    user_data[call.message.chat.id]['contact_method'] = contact_method
    
    delete_all_bot_messages(call.message.chat.id)
    
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Сейчас", callback_data="time_now"),
        types.InlineKeyboardButton("В течение дня", callback_data="time_day"),
        types.InlineKeyboardButton("Вечером", callback_data="time_evening"),
        types.InlineKeyboardButton("Завтра", callback_data="time_tomorrow"),
        types.InlineKeyboardButton("Укажу своё время", callback_data="time_custom")
    )
    
    send_message(
        call.message.chat.id,
        "<b>4. Когда вам удобно связаться?</b>",
        reply_markup=keyboard
    )
    user_states[call.message.chat.id] = "waiting_for_contact_time"
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("time_"))
def process_contact_time(call):
    time_map = {
        "time_now": "Сейчас",
        "time_day": "В течение дня",
        "time_evening": "Вечером",
        "time_tomorrow": "Завтра"
    }
    
    delete_all_bot_messages(call.message.chat.id)
    
    if call.data == "time_custom":
        send_message(call.message.chat.id, "Пожалуйста, укажите удобное для вас время:")
        user_states[call.message.chat.id] = "waiting_for_custom_time"
        bot.answer_callback_query(call.id)
        return
    
    contact_time = time_map.get(call.data)
    user_data[call.message.chat.id]['contact_time'] = contact_time
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("Пропустить", callback_data="skip_budget"))
    
    send_message(
        call.message.chat.id,
        "<b>5. Ваш ориентировочный бюджет / первоначальный взнос (по желанию):</b>\n"
        "Напишите сумму или нажмите кнопку пропустить",
        reply_markup=keyboard
    )
    user_states[call.message.chat.id] = "waiting_for_budget"
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_for_custom_time")
def process_custom_time(message):
    user_data[message.chat.id]['contact_time'] = message.text
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("Пропустить", callback_data="skip_budget"))
    
    bot.send_message(
        message.chat.id,
        "<b>5. Ваш ориентировочный бюджет / первоначальный взнос (по желанию):</b>\n"
        "Напишите сумму или нажмите кнопку пропустить",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    user_states[message.chat.id] = "waiting_for_budget"

@bot.callback_query_handler(func=lambda call: call.data == "skip_budget")
def skip_budget(call):
    user_data[call.message.chat.id]['budget'] = "Не указан"
    
    delete_all_bot_messages(call.message.chat.id)
    
    send_message(
        call.message.chat.id,
        "<b>6. Что бы вы хотели приобрести?</b>\n"
        "Опишите ваши пожелания (район, площадь, количество комнат и т.д.)"
    )
    user_states[call.message.chat.id] = "waiting_for_property_type"
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_for_budget")
def process_budget(message):
    user_data[message.chat.id]['budget'] = message.text
    
    bot.send_message(
        message.chat.id,
        "<b>6. Что бы вы хотели приобрести?</b>\n"
        "Опишите ваши пожелания (район, площадь, количество комнат и т.д.)",
        parse_mode="HTML"
    )
    user_states[message.chat.id] = "waiting_for_property_type"

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "waiting_for_property_type")
def process_property_type(message):
    user_data[message.chat.id]['property_type'] = message.text
    data = user_data[message.chat.id]
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO requests 
           (user_id, username, full_name, phone, contact_method, contact_time, budget, property_type) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message.from_user.id,
            message.from_user.username,
            data.get('full_name'),
            data.get('phone'),
            data.get('contact_method'),
            data.get('contact_time'),
            data.get('budget'),
            data.get('property_type')
        )
    )
    conn.commit()
    conn.close()
    
    admin_text = "<b>Новая заявка!</b>\n\n"
    admin_text += f"ФИО: {data.get('full_name')}\n"
    admin_text += f"Телефон: {data.get('phone')}\n"
    admin_text += f"Способ связи: {data.get('contact_method')}\n"
    admin_text += f"Время связи: {data.get('contact_time')}\n"
    admin_text += f"Бюджет: {data.get('budget')}\n"
    admin_text += f"Услуга: {data.get('service')}\n"
    admin_text += f"Пожелания: {data.get('property_type')}\n"
    if message.from_user.username:
        admin_text += f"Telegram: @{message.from_user.username}"

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, admin_text, parse_mode="HTML")
        except Exception as e:
            print(f"Ошибка отправки админу {admin_id}: {e}")
            
    delete_all_bot_messages(message.chat.id)
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("В главное меню", callback_data="main_menu"))
    
    send_message(
        message.chat.id,
        "<b>Спасибо! Ваша заявка принята!</b>\n\n"
        "Наш менеджер свяжется с вами в ближайшее время.",
        reply_markup=keyboard
    )
    user_states.pop(message.chat.id, None)
    user_data.pop(message.chat.id, None)

@bot.callback_query_handler(func=lambda call: call.data == "contact")
def contact_manager(call):
    delete_all_bot_messages(call.message.chat.id)
    
    text = "Наши контакты:\n\n"
    text += "Телефон: +7 (XXX) XXX-XX-XX\n"
    text += "Email: info@realestate.ru\n"
    text += "Часы работы: Пн-Пт 9:00-20:00, Сб-Вс 10:00-18:00\n\n"
    text += "Или оставьте заявку, и мы перезвоним вам!"
    
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Оставить заявку", callback_data="survey_start"),
        types.InlineKeyboardButton("В главное меню", callback_data="main_menu")
    )
    
    send_message(call.message.chat.id, text, reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "about")
def about_us(call):
    delete_all_bot_messages(call.message.chat.id)
    
    text = "<b>Агентство недвижимости</b>\n\n"
    text += "Мы работаем на рынке недвижимости более 10 лет.\n"
    text += "Помогаем нашим клиентам найти идеальное жилье!\n\n"
    text += "Более 1000 успешных сделок\n"
    text += "Профессиональные риелторы\n"
    text += "Юридическая поддержка\n"
    text += "Помощь с ипотекой\n\n"
    text += "Доверьте нам поиск вашей идеальной недвижимости!"
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("В главное меню", callback_data="main_menu"))
    
    send_message(call.message.chat.id, text, reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_add")
def add_property_start(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Недостаточно прав", show_alert=True)
        return
    
    delete_all_bot_messages(call.message.chat.id)
    
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Новостройки", callback_data="add_new_building"),
        types.InlineKeyboardButton("Вторичное жильё", callback_data="add_secondary"),
        types.InlineKeyboardButton("Дома", callback_data="add_house"),
        types.InlineKeyboardButton("Земельные участки", callback_data="add_land"),
        types.InlineKeyboardButton("Коммерческая недвижимость", callback_data="add_commercial"),
        types.InlineKeyboardButton("Отмена", callback_data="admin_menu")
    )
    
    send_message(call.message.chat.id, "Выберите тип объекта:", reply_markup=keyboard)
    user_states[call.message.chat.id] = "admin_waiting_for_type"
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("add_"))
def process_add_type(call):
    type_map = {
        "add_new_building": "new_building",
        "add_secondary": "secondary",
        "add_house": "house",
        "add_land": "land",
        "add_commercial": "commercial"
    }
    
    property_type = type_map.get(call.data)
    if call.message.chat.id not in user_data:
        user_data[call.message.chat.id] = {}
    user_data[call.message.chat.id]['type'] = property_type
    
    delete_all_bot_messages(call.message.chat.id)
    
    send_message(call.message.chat.id, "Введите название объекта:")
    user_states[call.message.chat.id] = "admin_waiting_for_title"
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "admin_waiting_for_title")
def process_title(message):
    user_data[message.chat.id]['title'] = message.text
    bot.send_message(message.chat.id, "Введите описание объекта:")
    user_states[message.chat.id] = "admin_waiting_for_description"

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "admin_waiting_for_description")
def process_description(message):
    user_data[message.chat.id]['description'] = message.text
    bot.send_message(message.chat.id, "Введите цену за квадратный метр (в рублях, только цифры):")
    user_states[message.chat.id] = "admin_waiting_for_price"

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "admin_waiting_for_price")
def process_price(message):
    try:
        price_per_sqm = int(message.text.replace(" ", ""))
        user_data[message.chat.id]['price_per_sqm'] = price_per_sqm
        bot.send_message(message.chat.id, "Введите локацию (адрес, район):")
        user_states[message.chat.id] = "admin_waiting_for_location"
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректную цену (только цифры)")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "admin_waiting_for_location")
def process_location(message):
    user_data[message.chat.id]['location'] = message.text
    bot.send_message(message.chat.id, "Введите площадь (в м²):")
    user_states[message.chat.id] = "admin_waiting_for_area"

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "admin_waiting_for_area")
def process_area(message):
    try:
        area = float(message.text.replace(",", "."))
        user_data[message.chat.id]['area'] = area
        bot.send_message(message.chat.id, "Введите количество комнат (если применимо, иначе 0):")
        user_states[message.chat.id] = "admin_waiting_for_rooms"
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректную площадь")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "admin_waiting_for_rooms")
def process_rooms(message):
    try:
        rooms = int(message.text)
        user_data[message.chat.id]['rooms'] = rooms
        bot.send_message(message.chat.id, "Введите этаж (если применимо, иначе 0):")
        user_states[message.chat.id] = "admin_waiting_for_floor"
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректное число")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "admin_waiting_for_floor")
def process_floor(message):
    try:
        floor = int(message.text)
        user_data[message.chat.id]['floor'] = floor
        bot.send_message(message.chat.id, "Введите общее количество этажей (если применимо, иначе 0):")
        user_states[message.chat.id] = "admin_waiting_for_total_floors"
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректное число")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == "admin_waiting_for_total_floors")
def process_total_floors(message):
    try:
        total_floors = int(message.text)
        user_data[message.chat.id]['total_floors'] = total_floors
        user_data[message.chat.id]['photos'] = []
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("Завершить добавление", callback_data="finish_photos"))
        
        bot.send_message(
            message.chat.id,
            "<b>Отправьте фотографии объекта</b>\n\n"
            "Просто отправляйте фото по одному.\n"
            "Когда закончите, нажмите кнопку ниже.",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        user_states[message.chat.id] = "admin_waiting_for_photos"
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректное число")

@bot.message_handler(content_types=['photo'], func=lambda message: user_states.get(message.chat.id) == "admin_waiting_for_photos")
def process_photo(message):
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        filename = f"{PHOTOS_DIR}/property_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{message.from_user.id}_{len(os.listdir(PHOTOS_DIR))}.jpg"
        
        with open(filename, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        user_data[message.chat.id]['photos'].append(filename)
        
        bot.send_message(message.chat.id, f"Фото {len(user_data[message.chat.id]['photos'])} добавлено! Отправьте ещё или завершите добавление.")
    except Exception as e:
        print(f"Ошибка сохранения фото: {e}")
        bot.send_message(message.chat.id, "Ошибка сохранения фото. Попробуйте ещё раз.")

@bot.callback_query_handler(func=lambda call: call.data == "finish_photos")
def finish_photos(call):
    data = user_data.get(call.message.chat.id, {})
    photos = data.get('photos', [])
    photos_str = ','.join(photos) if photos else ""
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO properties 
           (type, title, description, price_per_sqm, location, area, rooms, floor, total_floors, photos) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get('type'), data.get('title'), data.get('description'), data.get('price_per_sqm'),
            data.get('location'), data.get('area'), data.get('rooms'), data.get('floor'),
            data.get('total_floors'), photos_str
        )
    )
    conn.commit()
    conn.close()
    
    delete_all_bot_messages(call.message.chat.id)
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("В админ-панель", callback_data="admin_menu"))
    
    send_message(
        call.message.chat.id,
        f"Объект успешно добавлен!\nФотографий: {len(photos)}",
        reply_markup=keyboard
    )
    user_states.pop(call.message.chat.id, None)
    user_data.pop(call.message.chat.id, None)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_menu")
def admin_menu(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Недостаточно прав", show_alert=True)
        return
    
    delete_all_bot_messages(call.message.chat.id)
    
    send_message(
        call.message.chat.id,
        "Админ-панель\nВыберите действие:",
        reply_markup=get_admin_keyboard()
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_list")
def list_properties(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Недостаточно прав", show_alert=True)
        return
    
    delete_all_bot_messages(call.message.chat.id)
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, type, title, price_per_sqm, is_active FROM properties ORDER BY created_at DESC")
    properties = cursor.fetchall()
    conn.close()
    
    if not properties:
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("В админ-панель", callback_data="admin_menu"))
        send_message(call.message.chat.id, "Список объектов пуст", reply_markup=keyboard)
        bot.answer_callback_query(call.id)
        return
    
    text = "<b>Список объектов:</b>\n\n"
    for prop in properties:
        prop_id, prop_type, title, price_per_sqm, is_active = prop
        status = "Активен" if is_active else "Неактивен"
        text += f"{status} ID: {prop_id} | {title} | {price_per_sqm:,} руб/м²\n"
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("В админ-панель", callback_data="admin_menu"))
    
    send_message(call.message.chat.id, text[:4000], reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_requests")
def show_requests(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Недостаточно прав", show_alert=True)
        return
    
    delete_all_bot_messages(call.message.chat.id)
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id, full_name, phone, contact_method, contact_time, budget, property_type, created_at 
           FROM requests ORDER BY created_at DESC LIMIT 10"""
    )
    requests = cursor.fetchall()
    conn.close()
    
    if not requests:
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("В админ-панель", callback_data="admin_menu"))
        send_message(call.message.chat.id, "Заявок пока нет", reply_markup=keyboard)
        bot.answer_callback_query(call.id)
        return
    
    text = "<b>Последние заявки:</b>\n\n"
    for req in requests:
        req_id, full_name, phone, contact_method, contact_time, budget, property_type, created_at = req
        text += f"Заявка #{req_id}\n"
        text += f"ФИО: {full_name}\n"
        text += f"Телефон: {phone}\n"
        text += f"Способ связи: {contact_method}\n"
        text += f"Время: {contact_time}\n"
        text += f"Бюджет: {budget}\n"
        text += f"Ищет: {property_type}\n"
        text += f"Дата: {created_at}\n\n"
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("В админ-панель", callback_data="admin_menu"))
    
    send_message(call.message.chat.id, text[:4000], reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_delete")
def delete_property_start(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Недостаточно прав", show_alert=True)
        return
    
    delete_all_bot_messages(call.message.chat.id)
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, title FROM properties WHERE is_active = 1")
    properties = cursor.fetchall()
    conn.close()
    
    if not properties:
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("В админ-панель", callback_data="admin_menu"))
        send_message(call.message.chat.id, "Нет активных объектов для удаления", reply_markup=keyboard)
        bot.answer_callback_query(call.id)
        return
    
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for prop in properties[:10]:
        keyboard.add(
            types.InlineKeyboardButton(
                f"ID {prop[0]}: {prop[1][:30]}",
                callback_data=f"delete_{prop[0]}"
            )
        )
    keyboard.add(types.InlineKeyboardButton("В админ-панель", callback_data="admin_menu"))
    
    send_message(call.message.chat.id, "Выберите объект для удаления:", reply_markup=keyboard)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_"))
def delete_property(call):
    property_id = int(call.data.split("_")[1])
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE properties SET is_active = 0 WHERE id = ?", (property_id,))
    conn.commit()
    conn.close()
    
    bot.answer_callback_query(call.id, f"Объект ID {property_id} деактивирован", show_alert=True)
    admin_menu(call)

if __name__ == "__main__":
    init_db()
    print("Бот запущен!")
    bot.polling(none_stop=True)