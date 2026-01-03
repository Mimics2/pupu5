# main.py
import os
import sys
import logging
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import json
from pathlib import Path

from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    BotCommand,
    ChatMember,
    LabeledPrice
)
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    PreCheckoutQueryHandler
)
from telegram.request import HTTPXRequest

# ========== КОНФИГУРАЦИЯ ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7370973281:AAGdnM2SdekWwSF5alb5vnt0UWAN5QZ1dCQ")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6646433980"))
PORT = int(os.environ.get("PORT", 8443))
WEBHOOK_URL = os.environ.get("RAILWAY_STATIC_URL", "")
if WEBHOOK_URL:
    WEBHOOK_URL = f"https://{WEBHOOK_URL}/webhook"

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== БАЗА ДАННЫХ ==========
class Database:
    def __init__(self, db_path: str = "scheduler.db"):
        self.db_path = db_path
        self.connection = None
        
    async def connect(self):
        """Устанавливаем соединение с базой данных"""
        if self.connection is None:
            self.connection = await aiosqlite.connect(self.db_path)
            self.connection.row_factory = aiosqlite.Row
        return self.connection
    
    async def init_db(self):
        """Инициализация базы данных"""
        conn = await self.connect()
        
        # Пользователи
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                tariff TEXT DEFAULT 'free',
                subscription_end DATETIME,
                channels_count INTEGER DEFAULT 0,
                posts_today INTEGER DEFAULT 0,
                last_post_date DATE,
                registered_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Каналы пользователей
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_id TEXT,
                channel_name TEXT,
                added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, channel_id)
            )
        ''')
        
        # Запланированные посты
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_id TEXT,
                content_type TEXT,
                content TEXT,
                media_id TEXT,
                scheduled_time DATETIME,
                status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Платежи
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                tariff TEXT,
                amount INTEGER,
                status TEXT DEFAULT 'pending',
                payment_date DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Настройки тарифов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tariff_settings (
                tariff_name TEXT PRIMARY KEY,
                price INTEGER,
                channels_limit INTEGER,
                posts_per_day INTEGER,
                duration_days INTEGER
            )
        ''')
        
        # Приватные каналы
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS private_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tariff_name TEXT,
                channel_id TEXT,
                invite_link TEXT,
                UNIQUE(tariff_name)
            )
        ''')
        
        # Инициализируем базовый тариф
        await conn.execute('''
            INSERT OR REPLACE INTO tariff_settings 
            (tariff_name, price, channels_limit, posts_per_day, duration_days)
            VALUES ('basic', 100, 2, 5, 30)
        ''')
        
        await conn.commit()
        logger.info("База данных инициализирована")
    
    async def close(self):
        """Закрываем соединение"""
        if self.connection:
            await self.connection.close()
    
    # ========== ПОЛЬЗОВАТЕЛИ ==========
    async def add_user(self, user_id: int, username: str, first_name: str, last_name: str = ""):
        """Добавление пользователя"""
        conn = await self.connect()
        await conn.execute('''
            INSERT OR IGNORE INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name))
        await conn.commit()
    
    async def get_user(self, user_id: int) -> Optional[Dict]:
        """Получение информации о пользователе"""
        conn = await self.connect()
        async with conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None
    
    async def update_user_tariff(self, user_id: int, tariff: str, duration_days: int = 30):
        """Обновление тарифа пользователя"""
        subscription_end = datetime.now() + timedelta(days=duration_days)
        conn = await self.connect()
        await conn.execute('''
            UPDATE users 
            SET tariff = ?, subscription_end = ?
            WHERE user_id = ?
        ''', (tariff, subscription_end.isoformat(), user_id))
        await conn.commit()
    
    # ========== КАНАЛЫ ==========
    async def add_user_channel(self, user_id: int, channel_id: str, channel_name: str) -> Tuple[bool, str]:
        """Добавление канала пользователя"""
        conn = await self.connect()
        
        # Проверяем лимит каналов
        user = await self.get_user(user_id)
        tariff = await self.get_tariff_info(user['tariff'])
        
        async with conn.execute('SELECT COUNT(*) FROM user_channels WHERE user_id = ?', (user_id,)) as cursor:
            count = (await cursor.fetchone())[0]
            
        if count >= tariff['channels_limit']:
            return False, f"Лимит каналов ({tariff['channels_limit']}) достигнут"
        
        try:
            await conn.execute('''
                INSERT INTO user_channels (user_id, channel_id, channel_name)
                VALUES (?, ?, ?)
            ''', (user_id, channel_id, channel_name))
            await conn.commit()
            return True, "Канал успешно добавлен"
        except aiosqlite.IntegrityError:
            return False, "Этот канал уже добавлен"
    
    async def get_user_channels(self, user_id: int) -> List[Dict]:
        """Получение каналов пользователя"""
        conn = await self.connect()
        async with conn.execute(
            'SELECT * FROM user_channels WHERE user_id = ? ORDER BY added_at DESC', 
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    # ========== ТАРИФЫ ==========
    async def get_tariff_info(self, tariff_name: str) -> Dict:
        """Получение информации о тарифе"""
        conn = await self.connect()
        async with conn.execute(
            'SELECT * FROM tariff_settings WHERE tariff_name = ?', 
            (tariff_name,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            # Возвращаем тариф free по умолчанию
            return {
                'tariff_name': 'free',
                'price': 0,
                'channels_limit': 1,
                'posts_per_day': 1,
                'duration_days': 0
            }
    
    async def update_tariff_price(self, tariff_name: str, price: int) -> bool:
        """Обновление цены тарифа"""
        conn = await self.connect()
        cursor = await conn.execute(
            'UPDATE tariff_settings SET price = ? WHERE tariff_name = ?',
            (price, tariff_name)
        )
        await conn.commit()
        return cursor.rowcount > 0
    
    async def set_private_channel(self, tariff_name: str, channel_id: str, invite_link: str):
        """Настройка приватного канала"""
        conn = await self.connect()
        await conn.execute('''
            INSERT OR REPLACE INTO private_channels (tariff_name, channel_id, invite_link)
            VALUES (?, ?, ?)
        ''', (tariff_name, channel_id, invite_link))
        await conn.commit()
    
    async def get_private_channel(self, tariff_name: str) -> Optional[Dict]:
        """Получение приватного канала"""
        conn = await self.connect()
        async with conn.execute(
            'SELECT * FROM private_channels WHERE tariff_name = ?',
            (tariff_name,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None
    
    # ========== ПОСТЫ ==========
    async def add_scheduled_post(self, user_id: int, channel_id: str, content_type: str,
                                content: str, media_id: str, scheduled_time: datetime) -> int:
        """Добавление запланированного поста"""
        conn = await self.connect()
        cursor = await conn.execute('''
            INSERT INTO scheduled_posts 
            (user_id, channel_id, content_type, content, media_id, scheduled_time)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, channel_id, content_type, content, media_id, scheduled_time.isoformat()))
        await conn.commit()
        return cursor.lastrowid
    
    async def get_pending_posts(self) -> List[Dict]:
        """Получение ожидающих публикаций"""
        conn = await self.connect()
        async with conn.execute('''
            SELECT * FROM scheduled_posts 
            WHERE status = 'pending' AND scheduled_time <= datetime('now', '+5 minutes')
            ORDER BY scheduled_time
        ''') as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def update_post_status(self, post_id: int, status: str):
        """Обновление статуса поста"""
        conn = await self.connect()
        await conn.execute(
            'UPDATE scheduled_posts SET status = ? WHERE id = ?',
            (status, post_id)
        )
        await conn.commit()
    
    # ========== ПЛАТЕЖИ И СТАТИСТИКА ==========
    async def add_payment(self, user_id: int, tariff: str, amount: int):
        """Добавление платежа"""
        conn = await self.connect()
        await conn.execute('''
            INSERT INTO payments (user_id, tariff, amount, status)
            VALUES (?, ?, ?, 'completed')
        ''', (user_id, tariff, amount))
        await conn.commit()
    
    async def get_statistics(self) -> Dict:
        """Получение статистики"""
        conn = await self.connect()
        
        async with conn.execute('SELECT COUNT(*) FROM users') as cursor:
            total_users = (await cursor.fetchone())[0]
        
        async with conn.execute('SELECT SUM(amount) FROM payments WHERE status = "completed"') as cursor:
            total_revenue = (await cursor.fetchone())[0] or 0
        
        async with conn.execute('SELECT tariff, COUNT(*) FROM users GROUP BY tariff') as cursor:
            tariff_stats = {row[0]: row[1] for row in await cursor.fetchall()}
        
        return {
            'total_users': total_users,
            'total_revenue': total_revenue,
            'tariff_stats': tariff_stats
        }
    
    async def get_all_users(self) -> List[Dict]:
        """Получение всех пользователей"""
        conn = await self.connect()
        async with conn.execute('SELECT * FROM users ORDER BY registered_at DESC') as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

# Инициализируем базу данных
db = Database()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def create_keyboard(buttons: List[List[Dict]]) -> InlineKeyboardMarkup:
    """Создание клавиатуры"""
    keyboard = []
    for row in buttons:
        keyboard.append([
            InlineKeyboardButton(btn['text'], callback_data=btn['callback'])
            for btn in row
        ])
    return InlineKeyboardMarkup(keyboard)

async def check_user_admin(bot, chat_id: str, user_id: int) -> bool:
    """Проверка, является ли пользователь администратором канала"""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except Exception as e:
        logger.error(f"Ошибка проверки администратора: {e}")
        return False

# ========== ОСНОВНЫЕ КОМАНДЫ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user = update.effective_user
    await db.add_user(user.id, user.username, user.first_name, user.last_name)
    
    keyboard = create_keyboard([
        [{'text': '📅 Запланировать пост', 'callback': 'plan_post'}],
        [{'text': '📊 Мои каналы', 'callback': 'my_channels'}],
        [{'text': '💰 Тарифы', 'callback': 'tariffs'}],
        [{'text': '🆘 Помощь', 'callback': 'help'}]
    ])
    
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "🤖 Я бот для автоматической публикации контента в Telegram-каналах.\n\n"
        "📋 **Возможности:**\n"
        "• Планирование публикаций\n"
        "• Поддержка фото/видео/текста\n"
        "• Один платный тариф с особыми возможностями\n\n"
        "✨ **Для начала работы:**\n"
        "1. Добавьте канал (/add_channel)\n"
        "2. Выберите тариф (/tariffs)\n"
        "3. Планируйте посты!",
        reply_markup=keyboard
    )

async def tariffs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tariffs"""
    tariff = await db.get_tariff_info('basic')
    private_channel = await db.get_private_channel('basic')
    
    text = f"""
💰 **Базовый тариф**

💵 Цена: {tariff['price']} звезд
📊 Каналов: {tariff['channels_limit']}
📅 Постов в день: {tariff['posts_per_day']}
⏳ Срок: {tariff['duration_days']} дней

"""
    
    if private_channel:
        text += f"🔗 Приватный канал: {private_channel['invite_link']}\n\n"
    
    text += "💳 **Для покупки:**\nНажмите кнопку ниже или отправьте /buy"
    
    keyboard = create_keyboard([
        [{'text': '💳 Купить тариф', 'callback': 'buy_tariff'}],
        [{'text': '🔙 Назад', 'callback': 'main_menu'}]
    ])
    
    await update.message.reply_text(text, reply_markup=keyboard)

async def buy_tariff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Покупка тарифа"""
    query = update.callback_query
    await query.answer()
    
    tariff = await db.get_tariff_info('basic')
    private_channel = await db.get_private_channel('basic')
    
    if private_channel:
        text = f"""
💳 **Оплата тарифа**

💵 Стоимость: {tariff['price']} звезд

📋 **Условия:**
• Каналов: {tariff['channels_limit']}
• Постов в день: {tariff['posts_per_day']}
• Срок: {tariff['duration_days']} дней

🔗 **Для активации:**
1. Подпишитесь на канал: {private_channel['invite_link']}
2. Отправьте {tariff['price']} звезд в этот чат
3. Я проверю подписку и активирую тариф

⚠️ Если не подпишетесь в течение 2 часов, доступ будет отозван.
"""
    else:
        text = f"""
💳 **Оплата тарифа**

💵 Стоимость: {tariff['price']} звезд
📋 Условия: как в бесплатном тарифе

⚠️ Администратор еще не настроил приватный канал.
Свяжитесь с администратором для активации тарифа.
"""
    
    keyboard = create_keyboard([
        [{'text': '✅ Я подписался, оплатить', 'callback': 'confirm_payment'}],
        [{'text': '🔙 Назад', 'callback': 'tariffs'}]
    ])
    
    await query.edit_message_text(text, reply_markup=keyboard)

async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /add_channel"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Использование: /add_channel [ID_канала] [Название]\n\n"
            "Пример: /add_channel -1001234567890 Мой Канал\n\n"
            "📝 **Как получить ID канала?**\n"
            "1. Добавьте бота @getidsbot в канал\n"
            "2. Напишите любое сообщение\n"
            "3. Бот покажет ID канала"
        )
        return
    
    channel_id = context.args[0]
    channel_name = " ".join(context.args[1:])
    
    # Проверяем, что бот админ в канале
    if not await check_user_admin(context.bot, channel_id, context.bot.id):
        await update.message.reply_text(
            "❌ Я не являюсь администратором этого канала!\n\n"
            "📌 **Добавьте меня как администратора с правами:**\n"
            "• Публикация сообщений\n"
            "• Редактирование сообщений"
        )
        return
    
    success, message = await db.add_user_channel(update.effective_user.id, channel_id, channel_name)
    
    if success:
        await update.message.reply_text(
            f"✅ {message}\n\n"
            f"📝 Канал: {channel_name}\n"
            f"🔗 ID: {channel_id}"
        )
    else:
        await update.message.reply_text(f"❌ {message}")

async def my_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /channels"""
    user_id = update.effective_user.id
    channels = await db.get_user_channels(user_id)
    user = await db.get_user(user_id)
    
    if not channels:
        await update.message.reply_text(
            "📭 У вас нет добавленных каналов.\n\n"
            "✨ **Добавить канал:**\n"
            "/add_channel [ID] [Название]"
        )
        return
    
    text = f"📊 **Ваши каналы** (тариф: {user['tariff']})\n\n"
    for i, channel in enumerate(channels, 1):
        text += f"{i}. {channel['channel_name']}\n"
        text += f"   ID: {channel['channel_id']}\n\n"
    
    await update.message.reply_text(text)

# ========== ПЛАНИРОВАНИЕ ПОСТОВ ==========
async def plan_post_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало планирования поста"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    
    # Проверяем тариф
    if user['tariff'] == 'free':
        tariff = await db.get_tariff_info('free')
        posts_today = user['posts_today']
        
        if posts_today >= tariff['posts_per_day']:
            await query.edit_message_text(
                "❌ Лимит бесплатных постов на сегодня исчерпан!\n\n"
                "💳 **Купите тариф для увеличения лимита:**\n"
                "/tariffs - посмотреть тарифы"
            )
            return
    
    # Получаем каналы пользователя
    channels = await db.get_user_channels(user_id)
    if not channels:
        await query.edit_message_text(
            "❌ У вас нет добавленных каналов!\n\n"
            "✨ **Добавьте канал:**\n"
            "/add_channel [ID] [Название]"
        )
        return
    
    # Создаем клавиатуру с каналами
    keyboard_buttons = []
    for channel in channels:
        keyboard_buttons.append([
            {'text': f"📢 {channel['channel_name']}", 'callback': f"select_channel_{channel['channel_id']}"}
        ])
    keyboard_buttons.append([{'text': '🔙 Назад', 'callback': 'main_menu'}])
    
    keyboard = create_keyboard(keyboard_buttons)
    
    await query.edit_message_text(
        "📋 **Выберите канал для публикации:**",
        reply_markup=keyboard
    )

async def select_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор канала"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split('_')[2]
    context.user_data['channel_id'] = channel_id
    
    await query.edit_message_text(
        "📝 **Отправьте текст поста**\n\n"
        "Можно отправить:\n"
        "• Текст\n"
        "• Текст + фото\n"
        "• Текст + видео\n\n"
        "Или нажмите ❌ для отмены."
    )

async def handle_post_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка контента поста"""
    if update.message.text == '❌':
        await update.message.reply_text("❌ Планирование отменено.")
        return
    
    context.user_data['text'] = update.message.text or ""
    context.user_data['media_id'] = None
    context.user_data['content_type'] = 'text'
    
    if update.message.photo:
        context.user_data['media_id'] = update.message.photo[-1].file_id
        context.user_data['content_type'] = 'photo'
    elif update.message.video:
        context.user_data['media_id'] = update.message.video.file_id
        context.user_data['content_type'] = 'video'
    
    keyboard = create_keyboard([
        [
            {'text': '⏰ Через 1 час', 'callback': 'time_1h'},
            {'text': '⏱️ Через 3 часа', 'callback': 'time_3h'}
        ],
        [
            {'text': '🌅 Завтра утром', 'callback': 'time_tomorrow_9'},
            {'text': '🌆 Завтра вечером', 'callback': 'time_tomorrow_18'}
        ],
        [
            {'text': '📅 Выбрать дату', 'callback': 'time_custom'},
            {'text': '⚡ Сейчас', 'callback': 'time_now'}
        ],
        [{'text': '❌ Отмена', 'callback': 'cancel'}]
    ])
    
    text_preview = context.user_data['text'][:100] + "..." if len(context.user_data['text']) > 100 else context.user_data['text']
    
    await update.message.reply_text(
        f"✅ Контент получен!\n\n"
        f"📝 Текст: {text_preview}\n"
        f"📁 Тип: {context.user_data['content_type']}\n\n"
        f"⏰ **Выберите время публикации:**",
        reply_markup=keyboard
    )

async def select_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор времени публикации"""
    query = update.callback_query
    await query.answer()
    
    now = datetime.now()
    
    if query.data == 'time_1h':
        scheduled_time = now + timedelta(hours=1)
    elif query.data == 'time_3h':
        scheduled_time = now + timedelta(hours=3)
    elif query.data == 'time_tomorrow_9':
        scheduled_time = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0)
    elif query.data == 'time_tomorrow_18':
        scheduled_time = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0)
    elif query.data == 'time_now':
        scheduled_time = now + timedelta(minutes=5)
    elif query.data == 'time_custom':
        await query.edit_message_text(
            "📅 **Введите дату и время в формате:**\n"
            "ГГГГ.ММ.ДД ЧЧ:ММ\n\n"
            "Пример: 2025.12.31 18:30\n\n"
            "Или отправьте ❌ для отмены."
        )
        return
    elif query.data == 'cancel':
        await query.edit_message_text("❌ Планирование отменено.")
        return
    
    context.user_data['scheduled_time'] = scheduled_time
    
    # Показываем подтверждение
    keyboard = create_keyboard([
        [
            {'text': '✅ Да, запланировать', 'callback': 'confirm_post'},
            {'text': '❌ Нет, отменить', 'callback': 'cancel'}
        ]
    ])
    
    await query.edit_message_text(
        f"📋 **Подтверждение публикации**\n\n"
        f"📢 Канал: {context.user_data['channel_id']}\n"
        f"📝 Тип: {context.user_data['content_type']}\n"
        f"⏰ Время: {scheduled_time.strftime('%Y.%m.%d %H:%M')}\n\n"
        f"✅ **Подтвердить публикацию?**",
        reply_markup=keyboard
    )

async def handle_custom_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка пользовательского времени"""
    if update.message.text == '❌':
        await update.message.reply_text("❌ Планирование отменено.")
        return
    
    try:
        scheduled_time = datetime.strptime(update.message.text, "%Y.%m.%d %H:%M")
        
        if scheduled_time < datetime.now():
            await update.message.reply_text("❌ Нельзя планировать в прошлом!")
            return
        
        context.user_data['scheduled_time'] = scheduled_time
        
        keyboard = create_keyboard([
            [
                {'text': '✅ Да, запланировать', 'callback': 'confirm_post'},
                {'text': '❌ Нет, отменить', 'callback': 'cancel'}
            ]
        ])
        
        await update.message.reply_text(
            f"📋 **Подтверждение публикации**\n\n"
            f"📢 Канал: {context.user_data['channel_id']}\n"
            f"📝 Тип: {context.user_data['content_type']}\n"
            f"⏰ Время: {scheduled_time.strftime('%Y.%m.%d %H:%M')}\n\n"
            f"✅ **Подтвердить публикацию?**",
            reply_markup=keyboard
        )
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат!\n"
            "Используйте: ГГГГ.ММ.ДД ЧЧ:ММ\n"
            "Пример: 2025.12.31 18:30\n\n"
            "Попробуйте снова или отправьте ❌ для отмены."
        )

async def confirm_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение поста"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'cancel':
        await query.edit_message_text("❌ Планирование отменено.")
        return
    
    user_id = update.effective_user.id
    
    # Сохраняем пост
    post_id = await db.add_scheduled_post(
        user_id=user_id,
        channel_id=context.user_data['channel_id'],
        content_type=context.user_data['content_type'],
        content=context.user_data['text'],
        media_id=context.user_data['media_id'],
        scheduled_time=context.user_data['scheduled_time']
    )
    
    # Обновляем счетчик постов
    conn = await db.connect()
    today = datetime.now().date().isoformat()
    await conn.execute('''
        UPDATE users 
        SET posts_today = CASE 
            WHEN last_post_date = date(?) THEN posts_today + 1 
            ELSE 1 
        END,
        last_post_date = date(?)
        WHERE user_id = ?
    ''', (today, today, user_id))
    await conn.commit()
    
    await query.edit_message_text(
        f"✅ **Пост запланирован!**\n\n"
        f"📝 ID поста: {post_id}\n"
        f"⏰ Время публикации: {context.user_data['scheduled_time'].strftime('%Y.%m.%d %H:%M')}\n"
        f"📢 Канал: {context.user_data['channel_id']}\n\n"
        f"✨ Пост будет опубликован автоматически."
    )

# ========== АДМИН КОМАНДЫ ==========
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admin"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Доступ запрещен.")
        return
    
    stats = await db.get_statistics()
    tariff = await db.get_tariff_info('basic')
    
    text = f"""
🔧 **Админ панель**

👥 Пользователей: {stats['total_users']}
💰 Прибыль: {stats['total_revenue']} звезд

📊 **По тарифам:**
Free: {stats['tariff_stats'].get('free', 0)}
Basic: {stats['tariff_stats'].get('basic', 0)}

💵 **Текущий тариф Basic:**
Цена: {tariff['price']} звезд
Каналов: {tariff['channels_limit']}
Постов/день: {tariff['posts_per_day']}
    """
    
    keyboard = create_keyboard([
        [{'text': '💰 Изменить цену', 'callback': 'admin_set_price'}],
        [{'text': '🔗 Настроить канал', 'callback': 'admin_set_channel'}],
        [{'text': '📊 Статистика', 'callback': 'admin_stats'}],
        [{'text': '👥 Все пользователи', 'callback': 'admin_users'}]
    ])
    
    await update.message.reply_text(text, reply_markup=keyboard)

async def admin_set_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка цены"""
    query = update.callback_query
    await query.answer()
    
    tariff = await db.get_tariff_info('basic')
    
    await query.edit_message_text(
        f"💰 **Настройка цены тарифа**\n\n"
        f"Текущая цена: {tariff['price']} звезд\n\n"
        f"📝 **Введите новую цену:**\n"
        f"Пример: 150\n\n"
        f"Или отправьте ❌ для отмены."
    )

async def admin_set_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка приватного канала"""
    query = update.callback_query
    await query.answer()
    
    private_channel = await db.get_private_channel('basic')
    
    if private_channel:
        text = f"""
🔗 **Настройка приватного канала**

Текущий канал:
ID: {private_channel['channel_id']}
Ссылка: {private_channel['invite_link']}

📝 **Введите ID канала и ссылку:**
ID_канала ссылка

Пример:
-1001234567890 https://t.me/+abc123def456

Или отправьте ❌ для отмены.
"""
    else:
        text = """
🔗 **Настройка приватного канала**

Приватный канал не настроен.

📝 **Введите ID канала и ссылку:**
ID_канала ссылка

Пример:
-1001234567890 https://t.me/+abc123def456

Или отправьте ❌ для отмены.
"""
    
    await query.edit_message_text(text)

async def handle_admin_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка новой цены"""
    if update.message.text == '❌':
        await update.message.reply_text("❌ Отменено.")
        return
    
    try:
        new_price = int(update.message.text)
        if new_price <= 0:
            raise ValueError
        
        await db.update_tariff_price('basic', new_price)
        await update.message.reply_text(f"✅ Цена тарифа обновлена: {new_price} звезд")
    except ValueError:
        await update.message.reply_text(
            "❌ Неверная цена!\n"
            "Введите положительное число.\n"
            "Пример: 150"
        )

async def handle_admin_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка данных канала"""
    if update.message.text == '❌':
        await update.message.reply_text("❌ Отменено.")
        return
    
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "❌ Неверный формат!\n"
            "Введите: ID_канала ссылка\n"
            "Пример: -1001234567890 https://t.me/+abc123def456"
        )
        return
    
    channel_id = parts[0]
    invite_link = parts[1]
    
    await db.set_private_channel('basic', channel_id, invite_link)
    await update.message.reply_text(
        f"✅ Приватный канал настроен!\n\n"
        f"📢 ID: {channel_id}\n"
        f"🔗 Ссылка: {invite_link}"
    )

async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика"""
    query = update.callback_query
    await query.answer()
    
    stats = await db.get_statistics()
    
    text = f"""
📊 **Подробная статистика**

👥 Всего пользователей: {stats['total_users']}
💰 Общая прибыль: {stats['total_revenue']} звезд

📈 **Распределение по тарифам:**
Free: {stats['tariff_stats'].get('free', 0)}
Basic: {stats['tariff_stats'].get('basic', 0)}
    """
    
    await query.edit_message_text(text)

async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователи"""
    query = update.callback_query
    await query.answer()
    
    users = await db.get_all_users()
    
    if not users:
        await query.edit_message_text("📭 Пользователей нет.")
        return
    
    text = "👥 **Последние пользователи:**\n\n"
    for user in users[:10]:  # Показываем первых 10
        text += f"👤 {user['first_name']} (@{user['username'] or 'нет'})\n"
        text += f"   ID: {user['user_id']}\n"
        text += f"   Тариф: {user['tariff']}\n"
        text += f"   Каналов: {user['channels_count']}\n"
        text += f"   Регистрация: {user['registered_at'][:10]}\n\n"
    
    if len(users) > 10:
        text += f"\n... и еще {len(users) - 10} пользователей"
    
    await query.edit_message_text(text)

# ========== ПУБЛИКАЦИЯ ПОСТОВ ==========
async def publish_scheduled_posts(context: ContextTypes.DEFAULT_TYPE):
    """Публикация запланированных постов"""
    posts = await db.get_pending_posts()
    
    for post in posts:
        try:
            if post['content_type'] == 'photo':
                await context.bot.send_photo(
                    chat_id=post['channel_id'],
                    photo=post['media_id'],
                    caption=post['content']
                )
            elif post['content_type'] == 'video':
                await context.bot.send_video(
                    chat_id=post['channel_id'],
                    video=post['media_id'],
                    caption=post['content']
                )
            else:
                await context.bot.send_message(
                    chat_id=post['channel_id'],
                    text=post['content']
                )
            
            await db.update_post_status(post['id'], 'published')
            logger.info(f"Опубликован пост {post['id']} в канале {post['channel_id']}")
            
        except Exception as e:
            logger.error(f"Ошибка публикации поста {post['id']}: {e}")
            await db.update_post_status(post['id'], 'failed')

# ========== ОБРАБОТЧИК КНОПОК ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий кнопок"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == 'main_menu':
        await start(update, context)
    elif data == 'plan_post':
        await plan_post_start(update, context)
    elif data == 'my_channels':
        await my_channels_command(update, context)
    elif data == 'tariffs':
        await tariffs_command(update, context)
    elif data == 'help':
        await query.edit_message_text(
            "🆘 **Помощь**\n\n"
            "📋 **Основные команды:**\n"
            "/start - Главное меню\n"
            "/add_channel - Добавить канал\n"
            "/channels - Мои каналы\n"
            "/tariffs - Информация о тарифе\n"
            "/buy - Купить тариф\n\n"
            "📅 **Планирование постов:**\n"
            "1. Нажмите 'Запланировать пост'\n"
            "2. Выберите канал\n"
            "3. Отправьте контент\n"
            "4. Выберите время\n"
            "5. Подтвердите\n\n"
            "👨‍💼 **Админ команды:**\n"
            "/admin - Панель администратора\n\n"
            "📞 **Поддержка:** @ваш_username"
        )
    elif data == 'buy_tariff':
        await buy_tariff(update, context)
    elif data.startswith('select_channel_'):
        await select_channel_callback(update, context)
    elif data in ['time_1h', 'time_3h', 'time_tomorrow_9', 'time_tomorrow_18', 'time_now', 'time_custom', 'cancel']:
        await select_time_callback(update, context)
    elif data in ['confirm_post', 'confirm_payment']:
        await confirm_post_callback(update, context)
    elif data == 'admin_set_price':
        await admin_set_price_callback(update, context)
    elif data == 'admin_set_channel':
        await admin_set_channel_callback(update, context)
    elif data == 'admin_stats':
        await admin_stats_callback(update, context)
    elif data == 'admin_users':
        await admin_users_callback(update, context)

# ========== ГЛАВНАЯ ФУНКЦИЯ ==========
async def main():
    """Запуск бота"""
    # Инициализируем базу данных
    await db.init_db()
    
    # Создаем Application с настройками для Railway
    request = HTTPXRequest(connection_pool_size=50)
    
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .concurrent_updates(True)  # Включаем параллельную обработку
        .build()
    )
    
    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("tariffs", tariffs_command))
    application.add_handler(CommandHandler("add_channel", add_channel_command))
    application.add_handler(CommandHandler("channels", my_channels_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("buy", buy_tariff))
    
    # Обработчик контента поста
    application.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.VIDEO,
        handle_post_content
    ))
    
    # Обработчики админских сообщений
    application.add_handler(MessageHandler(
        filters.TEXT & filters.User(user_id=ADMIN_ID),
        handle_admin_price,
        pattern=r'^\d+$'
    ))
    
    application.add_handler(MessageHandler(
        filters.TEXT & filters.User(user_id=ADMIN_ID),
        handle_admin_channel,
        pattern=r'^-100\d+ .+'
    ))
    
    # Обработчик пользовательского времени
    application.add_handler(MessageHandler(
        filters.Regex(r'^\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}$'),
        handle_custom_time
    ))
    
    # Обработчик кнопок
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Периодическая задача для публикации постов
    job_queue = application.job_queue
    job_queue.run_repeating(publish_scheduled_posts, interval=60, first=10)
    
    # Запускаем бота
    if WEBHOOK_URL:
        # Используем webhook на Railway
        await application.initialize()
        await application.bot.set_webhook(WEBHOOK_URL)
        await application.start()
        
        # Создаем простой сервер для Railway
        from aiohttp import web
        
        async def handle_webhook(request):
            """Обработка webhook запросов"""
            if request.method == "POST":
                data = await request.json()
                update = Update.de_json(data, application.bot)
                await application.process_update(update)
            return web.Response(text="OK")
        
        app = web.Application()
        app.router.add_post("/webhook", handle_webhook)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        
        logger.info(f"Бот запущен на Railway с webhook: {WEBHOOK_URL}")
        
        # Бесконечный цикл
        await asyncio.Event().wait()
    else:
        # Используем polling для локального запуска
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        logger.info("Бот запущен с polling")
        
        # Бесконечный цикл
        await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
