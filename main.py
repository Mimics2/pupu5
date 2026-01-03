# main.py
import os
import sys
import logging
import traceback
import asyncio
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import aiosqlite

from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    BotCommand,
    ChatMember
)
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes,
    ApplicationBuilder,
    CallbackQueryHandler,
    ConversationHandler
)

# ========== КОНСТАНТЫ И КОНФИГУРАЦИЯ ==========
BOT_TOKEN = "7370973281:AAGdnM2SdekWwSF5alb5vnt0UWAN5QZ1dCQ"
ADMIN_ID = 6646433980

# Состояния для ConversationHandler
SELECT_CHANNEL, SELECT_CONTENT, SELECT_TIME, CONFIRM_POST = range(4)

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== КЛАСС БАЗЫ ДАННЫХ ==========
class Database:
    def __init__(self, db_path: str = "scheduler.db"):
        self.db_path = db_path
        
    async def init_db(self):
        """Инициализация базы данных"""
        async with aiosqlite.connect(self.db_path) as db:
            # Пользователи
            await db.execute('''
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
            await db.execute('''
                CREATE TABLE IF NOT EXISTS user_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    channel_id TEXT UNIQUE,
                    channel_name TEXT,
                    added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # Запланированные посты
            await db.execute('''
                CREATE TABLE IF NOT EXISTS scheduled_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    channel_id TEXT,
                    content_type TEXT,
                    content TEXT,
                    media_id TEXT,
                    scheduled_time DATETIME,
                    status TEXT DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # Платежи
            await db.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    tariff TEXT,
                    amount INTEGER,
                    status TEXT DEFAULT 'pending',
                    payment_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # Настройки тарифов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS tariff_settings (
                    tariff_name TEXT PRIMARY KEY,
                    price INTEGER,
                    channels_limit INTEGER,
                    posts_per_day INTEGER,
                    duration_days INTEGER,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Приватные каналы для тарифов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS private_channels (
                    tariff_name TEXT PRIMARY KEY,
                    channel_id TEXT UNIQUE,
                    invite_link TEXT,
                    added_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Инициализируем тарифы по умолчанию
            default_tariffs = [
                ('basic', 299, 2, 5, 30),
                ('premium', 599, 5, 20, 30),
                ('vip', 999, 10, 50, 30)
            ]
            
            for tariff in default_tariffs:
                await db.execute('''
                    INSERT OR IGNORE INTO tariff_settings 
                    (tariff_name, price, channels_limit, posts_per_day, duration_days)
                    VALUES (?, ?, ?, ?, ?)
                ''', tariff)
            
            await db.commit()
            logger.info("База данных инициализирована")
    
    # ========== ПОЛЬЗОВАТЕЛИ ==========
    async def add_user(self, user_id: int, username: str, first_name: str, last_name: str = ""):
        """Добавление нового пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT OR IGNORE INTO users (user_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
            ''', (user_id, username, first_name, last_name))
            await db.commit()
    
    async def get_user(self, user_id: int) -> Optional[Dict]:
        """Получение информации о пользователе"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None
    
    async def update_user_tariff(self, user_id: int, tariff: str):
        """Обновление тарифа пользователя"""
        tariff_info = await self.get_tariff_info(tariff)
        if not tariff_info:
            return False
        
        subscription_end = datetime.now() + timedelta(days=tariff_info['duration_days'])
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE users 
                SET tariff = ?, subscription_end = ?
                WHERE user_id = ?
            ''', (tariff, subscription_end.isoformat(), user_id))
            await db.commit()
            return True
    
    async def get_user_channels(self, user_id: int) -> List[Dict]:
        """Получение каналов пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                'SELECT * FROM user_channels WHERE user_id = ? ORDER BY added_at DESC',
                (user_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def add_user_channel(self, user_id: int, channel_id: str, channel_name: str) -> Tuple[bool, str]:
        """Добавление канала пользователя"""
        # Проверяем лимит каналов
        user = await self.get_user(user_id)
        if not user:
            return False, "Пользователь не найден"
        
        tariff_info = await self.get_tariff_info(user['tariff'])
        if not tariff_info:
            return False, "Тариф не найден"
        
        current_channels = await self.get_user_channels(user_id)
        if len(current_channels) >= tariff_info['channels_limit']:
            return False, f"Лимит каналов ({tariff_info['channels_limit']}) достигнут"
        
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute('''
                    INSERT INTO user_channels (user_id, channel_id, channel_name)
                    VALUES (?, ?, ?)
                ''', (user_id, channel_id, channel_name))
                
                await db.execute('''
                    UPDATE users SET channels_count = channels_count + 1 
                    WHERE user_id = ?
                ''', (user_id,))
                
                await db.commit()
                return True, "Канал успешно добавлен"
            except aiosqlite.IntegrityError:
                return False, "Этот канал уже добавлен"
    
    # ========== ТАРИФЫ ==========
    async def get_tariff_info(self, tariff_name: str) -> Optional[Dict]:
        """Получение информации о тарифе"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                'SELECT * FROM tariff_settings WHERE tariff_name = ?',
                (tariff_name,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
    
    async def get_all_tariffs(self) -> List[Dict]:
        """Получение всех тарифов"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('SELECT * FROM tariff_settings ORDER BY price')
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def update_tariff_price(self, tariff_name: str, price: int) -> bool:
        """Обновление цены тарифа"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                UPDATE tariff_settings SET price = ?, updated_at = CURRENT_TIMESTAMP
                WHERE tariff_name = ?
            ''', (price, tariff_name))
            await db.commit()
            return cursor.rowcount > 0
    
    async def add_private_channel(self, tariff_name: str, channel_id: str, invite_link: str) -> bool:
        """Добавление приватного канала для тарифа"""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute('''
                    INSERT OR REPLACE INTO private_channels (tariff_name, channel_id, invite_link)
                    VALUES (?, ?, ?)
                ''', (tariff_name, channel_id, invite_link))
                await db.commit()
                return True
            except:
                return False
    
    async def get_private_channel(self, tariff_name: str) -> Optional[Dict]:
        """Получение приватного канала для тарифа"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                'SELECT * FROM private_channels WHERE tariff_name = ?',
                (tariff_name,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
    
    # ========== ПОСТЫ ==========
    async def add_scheduled_post(self, user_id: int, channel_id: str, content_type: str,
                                content: str, media_id: str, scheduled_time: datetime) -> int:
        """Добавление запланированного поста"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                INSERT INTO scheduled_posts 
                (user_id, channel_id, content_type, content, media_id, scheduled_time)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, channel_id, content_type, content, media_id, scheduled_time.isoformat()))
            await db.commit()
            return cursor.lastrowid
    
    async def get_pending_posts(self, limit: int = 100) -> List[Dict]:
        """Получение ожидающих публикаций"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('''
                SELECT * FROM scheduled_posts 
                WHERE status = 'pending' AND scheduled_time <= datetime('now', '+1 hour')
                ORDER BY scheduled_time
                LIMIT ?
            ''', (limit,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def update_post_status(self, post_id: int, status: str):
        """Обновление статуса поста"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE scheduled_posts SET status = ? WHERE id = ?
            ''', (status, post_id))
            await db.commit()
    
    async def get_user_posts(self, user_id: int, limit: int = 50) -> List[Dict]:
        """Получение постов пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('''
                SELECT * FROM scheduled_posts 
                WHERE user_id = ?
                ORDER BY scheduled_time DESC
                LIMIT ?
            ''', (user_id, limit))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def check_post_limit(self, user_id: int) -> Tuple[bool, str]:
        """Проверка лимита постов на сегодня"""
        user = await self.get_user(user_id)
        if not user:
            return False, "Пользователь не найден"
        
        tariff_info = await self.get_tariff_info(user['tariff'])
        if not tariff_info:
            return False, "Тариф не найден"
        
        today = datetime.now().date()
        last_post_date = user.get('last_post_date')
        
        if last_post_date:
            last_post_date = datetime.fromisoformat(last_post_date).date() if isinstance(last_post_date, str) else last_post_date
            if last_post_date == today:
                if user['posts_today'] >= tariff_info['posts_per_day']:
                    return False, f"Лимит постов на сегодня ({tariff_info['posts_per_day']}) достигнут"
        
        return True, ""
    
    async def increment_post_count(self, user_id: int):
        """Увеличение счетчика постов"""
        today = datetime.now().date().isoformat()
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE users 
                SET posts_today = CASE 
                    WHEN last_post_date = date(?) THEN posts_today + 1 
                    ELSE 1 
                END,
                last_post_date = date(?)
                WHERE user_id = ?
            ''', (today, today, user_id))
            await db.commit()
    
    # ========== ПЛАТЕЖИ И СТАТИСТИКА ==========
    async def add_payment(self, user_id: int, tariff: str, amount: int, status: str = 'completed') -> int:
        """Добавление платежа"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                INSERT INTO payments (user_id, tariff, amount, status)
                VALUES (?, ?, ?, ?)
            ''', (user_id, tariff, amount, status))
            await db.commit()
            return cursor.lastrowid
    
    async def get_statistics(self) -> Dict:
        """Получение статистики"""
        async with aiosqlite.connect(self.db_path) as db:
            # Общее количество пользователей
            cursor = await db.execute('SELECT COUNT(*) FROM users')
            total_users = (await cursor.fetchone())[0]
            
            # Пользователи по тарифам
            cursor = await db.execute('''
                SELECT tariff, COUNT(*) as count FROM users GROUP BY tariff
            ''')
            tariff_stats = {row[0]: row[1] for row in await cursor.fetchall()}
            
            # Общая прибыль
            cursor = await db.execute('''
                SELECT SUM(amount) FROM payments WHERE status = 'completed'
            ''')
            total_revenue = (await cursor.fetchone())[0] or 0
            
            # Запланированные публикации
            cursor = await db.execute('''
                SELECT COUNT(*) FROM scheduled_posts WHERE status = 'pending'
            ''')
            pending_posts = (await cursor.fetchone())[0]
            
            return {
                'total_users': total_users,
                'tariff_stats': tariff_stats,
                'total_revenue': total_revenue,
                'pending_posts': pending_posts
            }
    
    async def get_all_users(self, limit: int = 1000) -> List[Dict]:
        """Получение всех пользователей"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('''
                SELECT * FROM users ORDER BY registered_at DESC LIMIT ?
            ''', (limit,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def export_users_csv(self) -> str:
        """Экспорт пользователей в CSV"""
        users = await self.get_all_users()
        
        csv_lines = ["ID,Username,First Name,Last Name,Tariff,Channels,Posts Today,Registered"]
        for user in users:
            csv_lines.append(
                f"{user['user_id']},"
                f"{user['username'] or ''},"
                f"{user['first_name']},"
                f"{user['last_name'] or ''},"
                f"{user['tariff']},"
                f"{user['channels_count']},"
                f"{user['posts_today']},"
                f"{user['registered_at']}"
            )
        
        return "\n".join(csv_lines)

# Инициализируем базу данных
db = Database()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def is_user_admin(chat_id: str, user_id: int, bot) -> bool:
    """Проверяет, является ли пользователь администратором канала"""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except:
        return False

async def check_subscription(user_id: int, bot) -> bool:
    """Проверяет подписку на приватный канал"""
    user = await db.get_user(user_id)
    if not user or user['tariff'] == 'free':
        return False
    
    private_channel = await db.get_private_channel(user['tariff'])
    if not private_channel:
        return True  # Если канал не настроен, пропускаем проверку
    
    try:
        member = await bot.get_chat_member(private_channel['channel_id'], user_id)
        return member.status not in [ChatMember.LEFT, ChatMember.KICKED, ChatMember.BANNED]
    except:
        return False

# ========== ОСНОВНЫЕ КОМАНДЫ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /start"""
    user = update.effective_user
    await db.add_user(user.id, user.username, user.first_name, user.last_name)
    
    welcome_text = f"""
👋 Привет, {user.first_name}!

🤖 Я бот для автоматической публикации контента в телеграм-каналах.

📋 **Основные возможности:**
• Планирование публикаций в каналах
• Поддержка фото, видео и текста
• Несколько тарифов с разными возможностями
• Автоматическая публикация по расписанию

✨ **Команды:**
/plan - Запланировать публикацию
/channels - Мои каналы
/tariffs - Тарифы и цены
/help - Помощь
    """
    
    keyboard = [
        [InlineKeyboardButton("📅 Запланировать пост", callback_data="plan_post")],
        [InlineKeyboardButton("📊 Мои каналы", callback_data="my_channels")],
        [InlineKeyboardButton("💰 Тарифы", callback_data="tariffs")],
        [InlineKeyboardButton("🆘 Помощь", callback_data="help")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает доступные тарифы"""
    tariffs_data = await db.get_all_tariffs()
    
    text = "💰 **Доступные тарифы:**\n\n"
    
    for tariff in tariffs_data:
        price = tariff['price']
        text += f"✨ **{tariff['tariff_name'].upper()}**\n"
        text += f"💵 {price} звезд\n"
        text += f"📊 Каналов: {tariff['channels_limit']}\n"
        text += f"📅 Постов/день: {tariff['posts_per_day']}\n"
        text += f"⏳ Дней: {tariff['duration_days']}\n\n"
    
    text += "⚠️ Для оплаты используйте команду /pay [тариф]\n"
    text += "Пример: /pay basic"
    
    keyboard = [
        [InlineKeyboardButton("💳 Купить BASIC", callback_data="buy_basic")],
        [InlineKeyboardButton("💎 Купить PREMIUM", callback_data="buy_premium")],
        [InlineKeyboardButton("👑 Купить VIP", callback_data="buy_vip")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка оплаты тарифа"""
    if not context.args:
        await update.message.reply_text("❌ Укажите тариф: /pay [basic/premium/vip]")
        return
    
    tariff_name = context.args[0].lower()
    tariff_info = await db.get_tariff_info(tariff_name)
    
    if not tariff_info:
        await update.message.reply_text("❌ Тариф не найден. Доступные: basic, premium, vip")
        return
    
    private_channel = await db.get_private_channel(tariff_name)
    
    if not private_channel:
        # Если канал не настроен, просто активируем тариф
        await db.update_user_tariff(update.effective_user.id, tariff_name)
        await db.add_payment(update.effective_user.id, tariff_name, tariff_info['price'])
        
        await update.message.reply_text(
            f"✅ Тариф {tariff_name.upper()} активирован!\n"
            f"Срок действия: {tariff_info['duration_days']} дней\n"
            f"Лимит каналов: {tariff_info['channels_limit']}\n"
            f"Постов в день: {tariff_info['posts_per_day']}"
        )
        return
    
    text = f"""
💰 **Оплата тарифа {tariff_name.upper()}**

💵 Стоимость: {tariff_info['price']} звезд

📋 Условия:
• Каналов: {tariff_info['channels_limit']}
• Постов в день: {tariff_info['posts_per_day']}
• Дней: {tariff_info['duration_days']}

Для оплаты:
1. Перейдите по ссылке: {private_channel['invite_link']}
2. Подпишитесь на канал
3. Отправьте {tariff_info['price']} звезд в этот чат
4. Бот проверит подписку и активирует тариф

⚠️ Если не подпишетесь в течение 2 часов, доступ будет отозван.
    """
    
    await update.message.reply_text(text)

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавление канала"""
    if not context.args:
        await update.message.reply_text(
            "❌ Укажите ID канала и название\n"
            "Пример: /add_channel -1001234567890 Название канала"
        )
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("❌ Укажите и ID канала и название")
        return
    
    channel_id = context.args[0]
    channel_name = " ".join(context.args[1:])
    
    # Проверяем, что бот админ в канале
    if not await is_user_admin(channel_id, context.bot.id, context.bot):
        await update.message.reply_text(
            "❌ Бот не является администратором этого канала.\n"
            "Добавьте бота как администратора с правом публикации постов."
        )
        return
    
    success, message = await db.add_user_channel(update.effective_user.id, channel_id, channel_name)
    
    if success:
        await update.message.reply_text(f"✅ {message}")
    else:
        await update.message.reply_text(f"❌ {message}")

async def my_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает каналы пользователя"""
    user_id = update.effective_user.id
    channels = await db.get_user_channels(user_id)
    user = await db.get_user(user_id)
    
    if not channels:
        await update.message.reply_text("📭 У вас нет добавленных каналов.\nИспользуйте /add_channel")
        return
    
    text = f"📊 **Ваши каналы** (тариф: {user['tariff']})\n\n"
    
    for i, channel in enumerate(channels, 1):
        text += f"{i}. {channel['channel_name']}\n"
        text += f"   ID: {channel['channel_id']}\n"
        text += f"   Добавлен: {channel['added_at'][:10]}\n\n"
    
    await update.message.reply_text(text)

# ========== ПЛАНИРОВАНИЕ ПОСТОВ ==========
async def plan_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало планирования поста"""
    user_id = update.effective_user.id
    
    # Проверяем лимит постов
    can_post, message = await db.check_post_limit(user_id)
    if not can_post:
        await update.message.reply_text(f"❌ {message}")
        return
    
    # Проверяем подписку
    if not await check_subscription(user_id, context.bot):
        await update.message.reply_text(
            "❌ Ваша подписка не активна или истекла.\n"
            "Проверьте подписку на приватный канал или обновите тариф."
        )
        return
    
    # Получаем каналы пользователя
    channels = await db.get_user_channels(user_id)
    if not channels:
        await update.message.reply_text(
            "❌ У вас нет добавленных каналов.\n"
            "Сначала добавьте канал: /add_channel"
        )
        return ConversationHandler.END
    
    # Создаем клавиатуру с каналами
    keyboard = []
    for channel in channels:
        keyboard.append([InlineKeyboardButton(
            channel['channel_name'], 
            callback_data=f"select_channel_{channel['channel_id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "📋 **Выберите канал для публикации:**",
        reply_markup=reply_markup
    )
    
    return SELECT_CHANNEL

async def select_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора канала"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("select_channel_"):
        channel_id = query.data.split("_")[2]
        context.user_data['channel_id'] = channel_id
        
        await query.edit_message_text(
            "📝 **Отправьте текст поста**\n\n"
            "Вы можете отправить:\n"
            "• Только текст\n"
            "• Текст + фото\n"
            "• Текст + видео\n\n"
            "Или отправьте ❌ для отмены."
        )
        
        return SELECT_CONTENT
    
    elif query.data == "cancel":
        await query.edit_message_text("❌ Планирование отменено.")
        return ConversationHandler.END

async def select_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка контента поста"""
    user_id = update.effective_user.id
    
    if update.message.text and update.message.text == "❌":
        await update.message.reply_text("❌ Планирование отменено.")
        return ConversationHandler.END
    
    context.user_data['text'] = update.message.text or ""
    context.user_data['media_id'] = None
    context.user_data['content_type'] = 'text'
    
    if update.message.photo:
        context.user_data['media_id'] = update.message.photo[-1].file_id
        context.user_data['content_type'] = 'photo'
    elif update.message.video:
        context.user_data['media_id'] = update.message.video.file_id
        context.user_data['content_type'] = 'video'
    
    keyboard = [
        [
            InlineKeyboardButton("⏰ Через 1 час", callback_data="time_1h"),
            InlineKeyboardButton("⏱️ Через 3 часа", callback_data="time_3h"),
        ],
        [
            InlineKeyboardButton("🌅 Завтра утром", callback_data="time_tomorrow_morning"),
            InlineKeyboardButton("🌆 Завтра вечером", callback_data="time_tomorrow_evening"),
        ],
        [
            InlineKeyboardButton("📅 Выбрать дату", callback_data="time_custom"),
            InlineKeyboardButton("⚡ Сейчас", callback_data="time_now"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    preview_text = context.user_data['text'][:100] + "..." if len(context.user_data['text']) > 100 else context.user_data['text']
    media_type = "📷 Фото" if context.user_data['content_type'] == 'photo' else "🎥 Видео" if context.user_data['content_type'] == 'video' else "📝 Текст"
    
    await update.message.reply_text(
        f"✅ Контент получен!\n"
        f"Тип: {media_type}\n"
        f"Текст: {preview_text}\n\n"
        f"⏰ **Выберите время публикации:**",
        reply_markup=reply_markup
    )
    
    return SELECT_TIME

async def select_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора времени"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel":
        await query.edit_message_text("❌ Планирование отменено.")
        return ConversationHandler.END
    
    now = datetime.now()
    
    if query.data == "time_1h":
        scheduled_time = now + timedelta(hours=1)
    elif query.data == "time_3h":
        scheduled_time = now + timedelta(hours=3)
    elif query.data == "time_tomorrow_morning":
        scheduled_time = now.replace(hour=9, minute=0, second=0) + timedelta(days=1)
    elif query.data == "time_tomorrow_evening":
        scheduled_time = now.replace(hour=18, minute=0, second=0) + timedelta(days=1)
    elif query.data == "time_now":
        scheduled_time = now + timedelta(minutes=5)
    elif query.data == "time_custom":
        await query.edit_message_text(
            "📅 **Введите дату и время в формате:**\n"
            "YYYY.MM.DD HH:MM\n\n"
            "Пример: 2025.12.31 18:30\n\n"
            "Или отправьте ❌ для отмены."
        )
        return SELECT_TIME
    
    context.user_data['scheduled_time'] = scheduled_time
    
    # Показываем подтверждение
    channel_id = context.user_data['channel_id']
    text = context.user_data['text']
    media_type = context.user_data['content_type']
    
    confirm_text = f"""
📋 **Подтверждение публикации:**

📢 Канал: {channel_id}
📝 Тип: {media_type}
⏰ Время: {scheduled_time.strftime('%Y.%m.%d %H:%M')}

Текст:
{text[:200]}...

✅ **Подтвердить публикацию?**
    """
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, опубликовать", callback_data="confirm_yes"),
            InlineKeyboardButton("❌ Нет, отменить", callback_data="confirm_no")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(confirm_text, reply_markup=reply_markup)
    
    return CONFIRM_POST

async def custom_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка пользовательского времени"""
    if update.message.text == "❌":
        await update.message.reply_text("❌ Планирование отменено.")
        return ConversationHandler.END
    
    try:
        date_str = update.message.text
        scheduled_time = datetime.strptime(date_str, "%Y.%m.%d %H:%M")
        
        if scheduled_time < datetime.now():
            await update.message.reply_text("❌ Нельзя планировать в прошлом!")
            return SELECT_TIME
        
        context.user_data['scheduled_time'] = scheduled_time
        
        # Показываем подтверждение
        channel_id = context.user_data['channel_id']
        text = context.user_data['text']
        media_type = context.user_data['content_type']
        
        confirm_text = f"""
📋 **Подтверждение публикации:**

📢 Канал: {channel_id}
📝 Тип: {media_type}
⏰ Время: {scheduled_time.strftime('%Y.%m.%d %H:%M')}

Текст:
{text[:200]}...

✅ **Подтвердить публикацию?**
        """
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Да, опубликовать", callback_data="confirm_yes"),
                InlineKeyboardButton("❌ Нет, отменить", callback_data="confirm_no")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(confirm_text, reply_markup=reply_markup)
        
        return CONFIRM_POST
        
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат!\n"
            "Используйте: YYYY.MM.DD HH:MM\n"
            "Пример: 2025.12.31 18:30\n\n"
            "Попробуйте снова или отправьте ❌ для отмены."
        )
        return SELECT_TIME

async def confirm_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение и сохранение поста"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm_no":
        await query.edit_message_text("❌ Планирование отменено.")
        return ConversationHandler.END
    
    # Сохраняем пост в БД
    user_id = update.effective_user.id
    channel_id = context.user_data['channel_id']
    content_type = context.user_data['content_type']
    text = context.user_data['text']
    media_id = context.user_data['media_id']
    scheduled_time = context.user_data['scheduled_time']
    
    post_id = await db.add_scheduled_post(
        user_id, channel_id, content_type, text, media_id, scheduled_time
    )
    
    await db.increment_post_count(user_id)
    
    await query.edit_message_text(
        f"✅ **Пост запланирован!**\n\n"
        f"📝 ID поста: {post_id}\n"
        f"⏰ Время: {scheduled_time.strftime('%Y.%m.%d %H:%M')}\n"
        f"📢 Канал: {channel_id}\n\n"
        f"Пост будет опубликован автоматически в указанное время."
    )
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена диалога"""
    await update.message.reply_text("❌ Операция отменена.")
    return ConversationHandler.END

# ========== АДМИН КОМАНДЫ ==========
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ панель"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Доступ запрещен.")
        return
    
    stats = await db.get_statistics()
    
    text = f"""
🔧 **Админ панель**

👥 Пользователей: {stats['total_users']}
💰 Прибыль: {stats['total_revenue']} звезд
📅 Ожидающих постов: {stats['pending_posts']}

📊 **По тарифам:**
Free: {stats['tariff_stats'].get('free', 0)}
Basic: {stats['tariff_stats'].get('basic', 0)}
Premium: {stats['tariff_stats'].get('premium', 0)}
VIP: {stats['tariff_stats'].get('vip', 0)}
    """
    
    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Все пользователи", callback_data="admin_users")],
        [InlineKeyboardButton("💰 Изменить цены", callback_data="admin_prices")],
        [InlineKeyboardButton("📢 Настройка каналов", callback_data="admin_channels")],
        [InlineKeyboardButton("📁 Экспорт данных", callback_data="admin_export")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка админ колбэков"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "admin_stats":
        stats = await db.get_statistics()
        
        text = f"""
📊 **Подробная статистика**

👥 Всего пользователей: {stats['total_users']}
💰 Общая прибыль: {stats['total_revenue']} звезд
📅 Ожидающих постов: {stats['pending_posts']}

📈 **Распределение по тарифам:**
Free: {stats['tariff_stats'].get('free', 0)}
Basic: {stats['tariff_stats'].get('basic', 0)}
Premium: {stats['tariff_stats'].get('premium', 0)}
VIP: {stats['tariff_stats'].get('vip', 0)}
        """
        
        await query.edit_message_text(text)
        
    elif query.data == "admin_users":
        users = await db.get_all_users(limit=50)
        
        text = "👥 **Последние 50 пользователей:**\n\n"
        for user in users[:20]:  # Показываем первые 20
            text += f"👤 {user['first_name']} (@{user['username'] or 'нет'})\n"
            text += f"   ID: {user['user_id']}\n"
            text += f"   Тариф: {user['tariff']}\n"
            text += f"   Каналов: {user['channels_count']}\n"
            text += f"   Регистрация: {user['registered_at'][:10]}\n\n"
        
        if len(users) > 20:
            text += f"\n... и еще {len(users) - 20} пользователей"
        
        await query.edit_message_text(text)
        
    elif query.data == "admin_export":
        csv_data = await db.export_users_csv()
        
        # Сохраняем во временный файл
        filename = f"users_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(csv_data)
        
        # Отправляем файл
        with open(filename, 'rb') as f:
            await query.message.reply_document(
                document=f,
                filename=filename,
                caption="📁 Экспорт пользователей в CSV"
            )
        
        # Удаляем временный файл
        import os
        os.remove(filename)
        
        await query.edit_message_text("✅ Данные экспортированы!")

async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка цены тарифа"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Доступ запрещен.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Использование: /set_price [тариф] [цена]\n"
            "Пример: /set_price basic 299"
        )
        return
    
    tariff_name = context.args[0].lower()
    try:
        price = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Цена должна быть числом!")
        return
    
    success = await db.update_tariff_price(tariff_name, price)
    
    if success:
        await update.message.reply_text(f"✅ Цена тарифа {tariff_name} установлена: {price} звезд")
    else:
        await update.message.reply_text(f"❌ Тариф {tariff_name} не найден!")

async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка приватного канала для тарифа"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Доступ запрещен.")
        return
    
    if len(context.args) < 3:
        await update.message.reply_text(
            "❌ Использование: /set_channel [тариф] [id_канала] [ссылка]\n"
            "Пример: /set_channel basic -1001234567890 https://t.me/+abc123"
        )
        return
    
    tariff_name = context.args[0].lower()
    channel_id = context.args[1]
    invite_link = context.args[2]
    
    success = await db.add_private_channel(tariff_name, channel_id, invite_link)
    
    if success:
        await update.message.reply_text(
            f"✅ Приватный канал для тарифа {tariff_name} настроен!\n"
            f"ID: {channel_id}\n"
            f"Ссылка: {invite_link}"
        )
    else:
        await update.message.reply_text(f"❌ Ошибка настройки канала!")

async def check_expired(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка истекших подписок"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    users = await db.get_all_users()
    expired_users = []
    
    for user in users:
        if user['tariff'] != 'free' and user['subscript_end']:
            end_date = datetime.fromisoformat(user['subscript_end'])
            if end_date < datetime.now():
                expired_users.append(user['user_id'])
    
    if expired_users:
        for user_id in expired_users:
            # Снимаем пользователя с тарифа
            await db.update_user_tariff(user_id, 'free')
            
            # Пытаемся кикнуть из приватного канала
            user_data = await db.get_user(user_id)
            if user_data:
                private_channel = await db.get_private_channel(user_data['tariff'])
                if private_channel:
                    try:
                        await context.bot.ban_chat_member(
                            private_channel['channel_id'],
                            user_id
                        )
                        await context.bot.unban_chat_member(
                            private_channel['channel_id'],
                            user_id
                        )
                    except:
                        pass
        
        await update.message.reply_text(
            f"✅ Проверка завершена!\n"
            f"Истекшие подписки: {len(expired_users)}\n"
            f"ID пользователей: {', '.join(map(str, expired_users[:10]))}"
            f"{'...' if len(expired_users) > 10 else ''}"
        )
    else:
        await update.message.reply_text("✅ Истекших подписок нет!")

# ========== ФУНКЦИЯ ПУБЛИКАЦИИ ПОСТОВ ==========
async def publish_posts(context: ContextTypes.DEFAULT_TYPE):
    """Публикация запланированных постов"""
    posts = await db.get_pending_posts()
    
    for post in posts:
        try:
            channel_id = post['channel_id']
            text = post['content']
            media_id = post['media_id']
            content_type = post['content_type']
            
            if content_type == 'photo':
                await context.bot.send_photo(
                    chat_id=channel_id,
                    photo=media_id,
                    caption=text
                )
            elif content_type == 'video':
                await context.bot.send_video(
                    chat_id=channel_id,
                    video=media_id,
                    caption=text
                )
            else:
                await context.bot.send_message(
                    chat_id=channel_id,
                    text=text
                )
            
            await db.update_post_status(post['id'], 'published')
            
        except Exception as e:
            logger.error(f"Ошибка публикации поста {post['id']}: {e}")
            await db.update_post_status(post['id'], 'failed')

# ========== КОЛБЭК ОБРАБОТЧИКИ ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий кнопок"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "plan_post":
        await plan_post(update, context)
    elif query.data == "my_channels":
        await my_channels(update, context)
    elif query.data == "tariffs":
        await tariffs(update, context)
    elif query.data == "help":
        await query.edit_message_text(
            "🆘 **Помощь по боту**\n\n"
            "📋 **Основные команды:**\n"
            "/start - Запуск бота\n"
            "/plan - Запланировать публикацию\n"
            "/add_channel - Добавить канал\n"
            "/my_channels - Мои каналы\n"
            "/tariffs - Тарифы\n"
            "/pay - Оплатить тариф\n\n"
            "👨‍💼 **Админ команды:**\n"
            "/admin - Панель администратора\n"
            "/set_price - Изменить цену\n"
            "/set_channel - Настроить канал\n"
            "/check_expired - Проверить подписки\n\n"
            "📞 **Поддержка:** @ваш_username"
        )
    elif query.data.startswith("buy_"):
        tariff = query.data.split("_")[1]
        await query.edit_message_text(
            f"Для покупки тарифа {tariff.upper()} используйте команду:\n"
            f"/pay {tariff}\n\n"
            "Бот пришлет инструкции по оплате."
        )

# ========== ОСНОВНАЯ ФУНКЦИЯ ==========
async def main():
    """Основная функция запуска бота"""
    # Инициализация базы данных
    await db.init_db()
    
    # Создание приложения
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Добавление обработчиков команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("tariffs", tariffs))
    application.add_handler(CommandHandler("pay", pay))
    application.add_handler(CommandHandler("add_channel", add_channel))
    application.add_handler(CommandHandler("my_channels", my_channels))
    
    # Админ команды
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CommandHandler("set_price", set_price))
    application.add_handler(CommandHandler("set_channel", set_channel))
    application.add_handler(CommandHandler("check_expired", check_expired))
    
    # Обработчик планирования постов (ConversationHandler)
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("plan", plan_post)],
        states={
            SELECT_CHANNEL: [CallbackQueryHandler(select_channel)],
            SELECT_CONTENT: [MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO, select_content)],
            SELECT_TIME: [
                CallbackQueryHandler(select_time),
                MessageHandler(filters.TEXT, custom_time)
            ],
            CONFIRM_POST: [CallbackQueryHandler(confirm_post)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(conv_handler)
    
    # Обработчик кнопок
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Периодическая задача для публикации постов
    job_queue = application.job_queue
    job_queue.run_repeating(publish_posts, interval=60, first=10)  # Каждую минуту
    
    # Периодическая проверка подписок (каждые 6 часов)
    async def check_subscriptions_job(context: ContextTypes.DEFAULT_TYPE):
        users = await db.get_all_users()
        for user in users:
            if user['tariff'] != 'free' and user['subscript_end']:
                end_date = datetime.fromisoformat(user['subscript_end'])
                if end_date < datetime.now():
                    # Тариф истек
                    await db.update_user_tariff(user['user_id'], 'free')
    
    job_queue.run_repeating(check_subscriptions_job, interval=21600, first=300)  # Каждые 6 часов
    
    # Запуск бота
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    # Бесконечный цикл
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
