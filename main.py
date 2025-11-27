import uvicorn
import asyncio
import httpx
import time
import secrets
import random
import sqlite3
import hashlib
import pathlib
import aiofiles
from fastapi import FastAPI, Request, Response, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from contextlib import asynccontextmanager
from jinja2 import Template
from typing import Annotated, Dict, List
from fastapi.staticfiles import StaticFiles

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8312115174:AAEVrID17hc68rmxKtAHEOk4ZYyExEpHfAs"
# ВАЖНО: Укажите ID вашего канала (напр., -100123456789) или @username
TELEGRAM_NOTIFY_CHAT_ID = "3406683744" # <--- ОБЯЗАТЕЛЬНО ИЗМЕНИТЕ ЭТО
TELEGRAM_SEND_MESSAGE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# Настройки сайта
DATABASE_FILE = "feed.db"
UPLOADS_DIR = pathlib.Path("uploads")
SESSION_DURATION = 86400 # 1 день
SITE_URL = "https://vlasovbot.onrender.com/" # Используется для уведомлений

# --- БАЗА ДАННЫХ ПОЛЬЗОВАТЕЛЕЙ (ВНУТРИ КОДА) ---
# Добавьте сюда ваши пары логин:пароль
USER_DB = {
    "user": "test",
    "admin": "root",
}

# --- АДМИНИСТРАТОРЫ САЙТА ---
# Логины из USER_DB, которые получат доп. права
ADMIN_USERS = [
    "admin", # Логин admin из USER_DB
]


# --- ХРАНИЛИЩА В ПАМЯТИ ---
# Хранит активные сессии: { "session_token_value": {"id": 1, "username": "user1", "is_admin": 0, "timestamp": 12345} }
active_sessions: Dict[str, dict] = {}
# Хранит решения для капчи: { "captcha_token_value": 8 }
captcha_solutions: Dict[str, int] = {}

# --- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ---
def hash_password(password):
    """Хеширование пароля"""
    return hashlib.sha256(password.encode()).hexdigest()

def init_db():
    """Создает таблицы и заполняет пользователей при запуске"""
    print("Инициализация базы данных...")
    try:
        con = sqlite3.connect(DATABASE_FILE)
        cur = con.cursor()

        # Включаем поддержку внешних ключей
        cur.execute("PRAGMA foreign_keys = ON;")

        # --- Таблица пользователей ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0
        )
        """)
        
        # --- Таблица постов ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author_username TEXT NOT NULL,
            type TEXT NOT NULL, -- 'text', 'photo', 'video', 'file', 'poll'
            content TEXT NOT NULL, -- Текст или ПУТЬ К ФАЙЛУ
            caption TEXT,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (author_username) REFERENCES users (username)
        )
        """)

        # --- Таблица опросов ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL UNIQUE,
            question TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES posts (id) ON DELETE CASCADE
        )
        """)

        # --- Таблица вариантов ответа ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS poll_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            FOREIGN KEY (poll_id) REFERENCES polls (id) ON DELETE CASCADE
        )
        """)
        
        # --- Таблица голосов ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS poll_votes (
            user_id INTEGER NOT NULL,
            option_id INTEGER NOT NULL,
            poll_id INTEGER NOT NULL, -- Добавлено для упрощения подсчета
            PRIMARY KEY (user_id, poll_id), -- Пользователь может голосовать 1 раз в 1 опросе
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (option_id) REFERENCES poll_options (id) ON DELETE CASCADE,
            FOREIGN KEY (poll_id) REFERENCES polls (id) ON DELETE CASCADE
        )
        """)

        # --- Таблица реакций ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS reactions (
            user_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            emoji TEXT NOT NULL,
            PRIMARY KEY (user_id, post_id), -- 1 юзер - 1 реакция (можно изменить, убрав emoji из PK)
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (post_id) REFERENCES posts (id) ON DELETE CASCADE
        )
        """)
        print("Таблицы успешно проверены/созданы.")

        # --- Добавление/Обновление пользователей из словарей ---
        print("Обновление пользователей...")
        for username, password in USER_DB.items():
            pass_hash = hash_password(password)
            is_admin = 1 if username in ADMIN_USERS else 0
            try:
                cur.execute(
                    "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                    (username, pass_hash, is_admin)
                )
                print(f"  > Пользователь '{username}' создан.")
            except sqlite3.IntegrityError:
                # Если пользователь уже существует, обновляем его
                cur.execute(
                    "UPDATE users SET password_hash = ?, is_admin = ? WHERE username = ?",
                    (pass_hash, is_admin, username)
                )
        print(f"  > Пользователи синхронизированы.")

        con.commit()
        print("База данных успешно инициализирована!")

    except Exception as e:
        print(f"ОШИБКА ИНИЦИАЛИЗАЦИИ БД: {e}")
    finally:
        if con:
            con.close()

# --- БАЗА ДАННЫХ (FastAPI Зависимость) ---
def get_db():
    """Подключение к БД для эндпоинтов"""
    try:
        db = sqlite3.connect(DATABASE_FILE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON;")
        yield db
    finally:
        db.close()


# --- ФУНКЦИЯ УВЕДОМЛЕНИЯ В TELEGRAM ---
async def send_telegram_notification(text: str):
    """Отправляет уведомление в TG канал"""
    if "-100123456789" in TELEGRAM_NOTIFY_CHAT_ID or "8312115174" not in BOT_TOKEN:
        print("Уведомление не отправлено: не настроен CHAT_ID или BOT_TOKEN")
        return

    # Экранирование для MarkdownV2
    safe_text = text.translate(str.maketrans({
        "_": r"\_", "*": r"\*", "[": r"\[", "]": r"\]", "(": r"\(", ")": r"\)",
        "~": r"\~", "`": r"\`", ">": r"\>", "#": r"\#", "+": r"\+", "-": r"\-",
        "=": r"\=", "|": r"\|", "{": r"\{", "}": r"\}", ".": r"\.", "!": r"\!"
    }))

    message = f"🔥 *Новый пост на сайте!*\n\n{safe_text}\n\n[Перейти на сайт]({SITE_URL})"
    payload = {
        "chat_id": TELEGRAM_NOTIFY_CHAT_ID,
        "text": message,
        "parse_mode": "MarkdownV2"
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(TELEGRAM_SEND_MESSAGE_URL, json=payload, timeout=10)
            if r.status_code != 200:
                print(f"Ошибка отправки уведомления: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"Исключение при отправке уведомления: {e}")

# --- HTML ШАБЛОНЫ ---

# Шаблон 1: Страница Входа (без изменений)
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Вход</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
        .bg-telegram { background-color: #0E1621; }
        .header-telegram { background-color: #17212B; }
        .bubble-telegram { background-color: #182533; }
        .btn-telegram { background-color: #2B5278; }
    </style>
</head>
<body class="bg-telegram text-slate-100 min-h-screen flex items-center justify-center p-4">
    <div class="max-w-sm w-full bg-telegram rounded-lg p-8">
        <h1 class="text-3xl font-bold text-center mb-6">Вход в Канал</h1>
        
        {% if error %}
        <div class="bg-red-800 border border-red-600 text-red-100 px-4 py-3 rounded-lg mb-4 text-center">
            {{ error }}
        </div>
        {% endif %}

        <form action="/login" method="post">
            <input type="hidden" name="captcha_token" value="{{ captcha_token }}">
            
            <div class="mb-4">
                <label for="username" class="block text-sm font-medium text-slate-300 mb-2">Логин</label>
                <input type="text" id="username" name="username" required
                       class="w-full px-4 py-2 bg-telegram border border-slate-700 rounded-lg focus:outline-none focus:border-blue-500">
            </div>
            
            <div class="mb-4">
                <label for="password" class="block text-sm font-medium text-slate-300 mb-2">Пароль</label>
                <input type="password" id="password" name="password" required
                       class="w-full px-4 py-2 bg-telegram border border-slate-700 rounded-lg focus:outline-none focus:border-blue-500">
            </div>

            <div class="mb-6">
                <label for="captcha" class="block text-sm font-medium text-slate-300 mb-2">
                    Решите пример: {{ num1 }} + {{ num2 }} = ?
                </label>
                <input type="number" id="captcha" name="captcha_answer" required
                       class="w-full px-4 py-2 bg-telegram border border-slate-700 rounded-lg focus:outline-none focus:border-blue-500">
            </div>

            <button type="submit" class="w-full btn-telegram text-white font-bold py-3 px-4 rounded-lg hover:bg-blue-600 transition-colors">
                Войти
            </button>
        </form>
    </div>
</body>
</html>
"""

# Шаблон 2: Основная страница (Лента) + Панель админа
PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Мой Канал</title>
    <link rel="icon" href="https://telegram.org/favicon.ico" type="image/x-icon">
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <script src="https://unpkg.com/htmx.org/dist/ext/morphdom-swap.js"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
        .bg-telegram { background-color: #0E1621; }
        .header-telegram { background-color: #17212B; }
        .footer-telegram { background-color: #17212B; }
        .bubble-telegram { background-color: #182533; }
        .poll-option { background-color: #374151; border: 1px solid #4b5563; }
        .poll-option-voted { background-color: #2B5278; border-color: #4a78a0; }
        #messages-container { scroll-behavior: smooth; }
        video::-webkit-media-controls-enclosure { border-radius: 0; }
        /* Скрытие input[type=file] */
        .file-input { display: none; }
    </style>
</head>
<body class="bg-telegram text-slate-100 min-h-screen" hx-ext="morphdom-swap">

    <div class="max-w-2xl mx-auto flex flex-col h-screen">
        
        <header class="header-telegram flex items-center justify-between p-3 shadow-md sticky top-0 z-10">
            <div class="flex items-center gap-3">
                <img src="https://placehold.co/40x40/2B5278/FFFFFF?text=MC" alt="Avatar" class="w-10 h-10 rounded-full">
                <div>
                    <h1 class="font-bold text-base">Мой Канал</h1>
                    <div id="status-container" 
                         class="text-xs text-green-400">
                         в сети
                    </div>
                </div>
            </div>
            <a href="/logout" class="text-slate-400 hover:text-red-400" title="Выйти">
                <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                </svg>
            </a>
        </header>

        <main id="messages-container" 
              class="flex-1 overflow-y-auto p-3 space-y-3" 
              hx-get="/updates" 
              hx-trigger="load, every 5s" 
              hx-swap="morphdom">
            <div class="text-slate-500 text-center mt-10">Загрузка сообщений...</div>
        </main>
        
        <!-- ===== ПАНЕЛЬ АДМИНИСТРАТОРА ===== -->
        {% if is_admin %}
        <footer id="admin-panel" class="footer-telegram p-2 sticky bottom-0 z-10" hx-ext="morphdom-swap">
            
            <!-- Форма по умолчанию: Текст -->
            <form id="post-form" 
                  hx-post="/post/text" 
                  hx-swap="none" 
                  hx-on::after-request="this.reset(); htmx.trigger('#messages-container', 'load');"
                  class="bg-telegram rounded-full flex items-center p-2 gap-2">
                
                <label for="file-upload" class="text-slate-400 px-2 cursor-pointer hover:text-blue-400">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.414a4 4 0 00-5.656-5.656l-6.415 6.415a6 6 0 108.486 8.486L20.5 13" /></svg>
                </label>
                <!-- Скрытый инпут для файла -->
                <input type="file" id="file-upload" name="file" class="file-input" onchange="showFileForm(this)">
                
                <input type="text" name="content" class="bg-transparent text-lg flex-1 px-2 focus:outline-none" placeholder="Сообщение" required>
                
                <button type="button" hx-get="/form/poll" hx-target="#admin-panel" class="text-slate-400 px-2 hover:text-blue-400" title="Создать опрос">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M16 8v8m-4-5v5m-4-2v2m-2 4h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>
                </button>
                <button type="submit" class="text-blue-500 px-2">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="currentColor" viewBox="0 0 20 20"><path d="M10.894 2.553a1 1 0 00-1.788 0l-7 14a1 1 0 001.169 1.409l5-1.429A1 1 0 009 15.571V11a1 1 0 112 0v4.571a1 1 0 00.725.962l5 1.428a1 1 0 001.17-1.408l-7-14z" /></svg>
                </button>
            </form>

        </footer>
        {% endif %}
    </div>

<script>
    // Показать форму загрузки файла, когда файл выбран
    function showFileForm(input) {
        if (input.files && input.files[0]) {
            const fileName = input.files[0].name;
            const fileForm = `
                <form id="post-form" 
                      enctype="multipart/form-data" 
                      hx-post="/post/file" 
                      hx-swap="none" 
                      hx-on::after-request="htmx.trigger('#admin-panel', 'load'); htmx.trigger('#messages-container', 'load');"
                      class="bg-telegram rounded-lg flex flex-col p-4 gap-2">
                    
                    <p class="text-sm text-slate-300">Файл: <span class="font-medium text-white">${fileName}</span></p>
                    
                    <input type="text" name="caption" class="bg-slate-800 border border-slate-700 text-base w-full p-2 rounded-lg" placeholder="Подпись (необязательно)">
                    
                    <div class="flex gap-2 mt-2">
                        <button type="button" hx-get="/form/text" hx-target="#admin-panel" class="flex-1 bg-slate-700 text-white px-3 py-2 rounded-lg text-sm">Отмена</button>
                        <button type="submit" class="flex-1 bg-blue-600 text-white px-3 py-2 rounded-lg text-sm font-medium">Отправить файл</button>
                    </div>
                </form>
            `;
            // Важно: нужно сохранить файл, поэтому мы не можем просто поменять hx-post
            // Мы должны воссоздать DOM и сохранить ссылку на файл
            const panel = document.getElementById('admin-panel');
            const oldForm = document.getElementById('post-form');
            const fileInput = oldForm.querySelector('#file-upload');
            
            panel.innerHTML = fileForm;
            const newForm = panel.querySelector('form');
            // Перемещаем элемент input[type=file] в новую форму, чтобы не потерять файл
            newForm.prepend(fileInput); 
            
            htmx.process(panel);
        }
    }
</script>
</body>
</html>
"""

# Шаблон 3: Карточки сообщений (Читают из новой БД)
UPDATES_TEMPLATE = """
{% for msg in posts %}
<div class="message-bubble-wrapper flex justify-start" data-message-id="{{ msg.id }}">
    <div class="bubble-telegram rounded-xl rounded-bl-none shadow-md max-w-lg">
        
        <!-- ФОТО/ВИДЕО/ФАЙЛ -->
        {% if msg.type in ['photo', 'video', 'file'] %}
            {% if msg.type == 'photo' %}
                <img src="/uploads/{{ msg.content }}" 
                     class="w-full {% if msg.caption %}rounded-t-xl{% else %}rounded-xl{% endif %}" 
                     alt="Photo">
            {% elif msg.type == 'video' %}
                <video controls preload="metadata" 
                       class="w-full {% if msg.caption %}rounded-t-xl{% else %}rounded-xl{% endif %}" 
                       src="/uploads/{{ msg.content }}#t=0.1">
                </video>
            {% elif msg.type == 'file' %}
                <a href="/uploads/{{ msg.content }}" download 
                   class="flex items-center gap-3 p-3 hover:bg-slate-700/50 rounded-xl transition-colors">
                    <div class="flex-shrink-0 w-10 h-10 bg-blue-500 rounded-full flex items-center justify-center">
                        <svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
                    </div>
                    <div class="truncate flex-1">
                        <p class="text-slate-100 font-medium truncate">{{ msg.content.split('__', 1)[1] if '__' in msg.content else msg.content }}</p>
                        <span class="text-xs text-blue-400">Скачать</span>
                    </div>
                </a>
            {% endif %}
            
            {% if msg.caption %}
                <p class="p-3 pt-2 text-slate-200 text-base whitespace-pre-wrap">{{ msg.caption }}</p>
            {% endif %}
        
        <!-- ТЕКСТ -->
        {% elif msg.type == 'text' %}
            <p class="p-3 text-slate-200 text-base whitespace-pre-wrap">{{ msg.content }}</p>
            
        <!-- ОПРОС -->
        {% elif msg.type == 'poll' %}
            <div hx-get="/poll/{{ msg.poll.id }}" hx-trigger="load, every 5s" hx-swap="innerHTML">
                <!-- Сюда загрузится poll_template -->
                <div class="p-3">
                    <p class="font-bold text-base mb-3">{{ msg.poll.question }}</p>
                    <div class="space-y-2 animate-pulse">
                        <div class="h-8 bg-slate-700 rounded-lg"></div>
                        <div class="h-8 bg-slate-700 rounded-lg"></div>
                    </div>
                </div>
            </div>
        
        {% endif %}

        <div class="flex justify-end items-center px-3 {% if msg.type != 'text' and msg.type != 'poll' and msg.caption is none %}pb-2{% else %}pb-1{% endif %} {% if msg.type == 'photo' or msg.type == 'video' or msg.type == 'file' %}pt-1{% endif %}">
            <span class="text-xs text-slate-400">{{ time.strftime('%H:%M', time.localtime(msg.created_at)) }}</span>
        </div>

        <!-- Секция реакций (отображение + отправка) -->
        <div class="flex flex-wrap items-center gap-1 p-2 border-t border-slate-700/50" 
             hx-get="/react/{{ msg.id }}" 
             hx-trigger="load, every 5s" 
             hx-swap="innerHTML">
            <!-- Сюда загрузится reaction_template -->
            <div class="h-5 w-full animate-pulse bg-slate-700 rounded-full"></div>
        </div>
    </div>
</div>
{% endfor %}
"""

# Шаблон 4: Форма создания опроса
POLL_FORM_TEMPLATE = """
<form id="post-form" 
      hx-post="/post/poll" 
      hx-swap="none" 
      hx-on::after-request="htmx.trigger('#admin-panel', 'load'); htmx.trigger('#messages-container', 'load');"
      class="bg-telegram rounded-lg flex flex-col p-4 gap-2">
    
    <input type="text" name="question" class="bg-slate-800 border border-slate-700 text-base w-full p-2 rounded-lg" placeholder="Вопрос опроса" required>
    <input type="text" name="option1" class="bg-slate-800 border border-slate-700 text-sm w-full p-2 rounded-lg" placeholder="Вариант 1" required>
    <input type="text" name="option2" class="bg-slate-800 border border-slate-700 text-sm w-full p-2 rounded-lg" placeholder="Вариант 2" required>
    <input type="text" name="option3" class="bg-slate-800 border border-slate-700 text-sm w-full p-2 rounded-lg" placeholder="Вариант 3 (необязательно)">
    <input type="text" name="option4" class="bg-slate-800 border border-slate-700 text-sm w-full p-2 rounded-lg" placeholder="Вариант 4 (необязательно)">
    
    <div class="flex gap-2 mt-2">
        <button type="button" hx-get="/form/text" hx-target="#admin-panel" class="flex-1 bg-slate-700 text-white px-3 py-2 rounded-lg text-sm">Отмена</button>
        <button type="submit" class="flex-1 bg-blue-600 text-white px-3 py-2 rounded-lg text-sm font-medium">Создать опрос</button>
    </div>
</form>
"""

# Шаблон 5: Форма текста (для отмены)
TEXT_FORM_TEMPLATE = """
<form id="post-form" 
      hx-post="/post/text" 
      hx-swap="none" 
      hx-on::after-request="this.reset(); htmx.trigger('#messages-container', 'load');"
      class="bg-telegram rounded-full flex items-center p-2 gap-2"
      hx-trigger="load" hx-target="#admin-panel" hx-swap="outerHTML">
    
    <label for="file-upload" class="text-slate-400 px-2 cursor-pointer hover:text-blue-400">
        <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.414a4 4 0 00-5.656-5.656l-6.415 6.415a6 6 0 108.486 8.486L20.5 13" /></svg>
    </label>
    <input type="file" id="file-upload" name="file" class="file-input" onchange="showFileForm(this)">
    
    <input type="text" name="content" class="bg-transparent text-lg flex-1 px-2 focus:outline-none" placeholder="Сообщение" required>
    
    <button type="button" hx-get="/form/poll" hx-target="#admin-panel" class="text-slate-400 px-2 hover:text-blue-400" title="Создать опрос">
        <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M16 8v8m-4-5v5m-4-2v2m-2 4h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>
    </button>
    <button type="submit" class="text-blue-500 px-2">
        <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="currentColor" viewBox="0 0 20 20"><path d="M10.894 2.553a1 1 0 00-1.788 0l-7 14a1 1 0 001.169 1.409l5-1.429A1 1 0 009 15.571V11a1 1 0 112 0v4.571a1 1 0 00.725.962l5 1.428a1 1 0 001.17-1.408l-7-14z" /></svg>
    </button>
</form>
"""

# Шаблон 6: Только секция реакций (для htmx)
REACTIONS_TEMPLATE = """
<!-- Отображение существующих реакций -->
{% for emoji, count in reactions.items() %}
    <span class="bg-blue-500/20 text-blue-300 text-xs font-medium px-2 py-0.5 rounded-full border border-blue-500/30
                 {% if emoji == my_reaction %} ring-2 ring-blue-400 {% endif %}">
        {{ emoji }} {{ count }}
    </span>
{% endfor %}

<!-- Кнопки для добавления новых реакций -->
<div class="ml-auto flex gap-1 items-center">
    {% for emoji in allowed_emojis %}
    <button hx-post="/react/{{ post_id }}" hx-vals='{"emoji": "{{ emoji }}"}' 
            hx-target="closest .message-bubble-wrapper" hx-swap="none"
            hx-on::after-request="htmx.trigger(closest('[data-message-id]'), 'loadReactions')"
            class="px-1.5 py-0.5 rounded-full text-sm opacity-60 hover:opacity-100 hover:bg-slate-700 transition
                   {% if emoji == my_reaction %} bg-blue-500/30 opacity-100 {% endif %}">
        {{ emoji }}
    </button>
    {% endfor %}
    
    <!-- Кнопка удаления (только для админов) -->
    {% if is_admin %}
    <button hx-post="/message/delete/{{ post_id }}"
            hx-target="closest .message-bubble-wrapper"
            hx-swap="outerHTML"
            class="ml-2 px-1.5 py-0.5 rounded-full text-sm text-red-500 opacity-60 hover:opacity-100 hover:bg-slate-700 transition"
            title="Удалить пост">
        &#x2715; <!-- Крестик -->
    </button>
    {% endif %}
</div>
"""

# Шаблон 7: Только секция опроса (для htmx)
POLL_TEMPLATE = """
<div class="p-3">
    <p class="font-bold text-base mb-3">{{ poll.question }}</p>
    <div class="space-y-2">
        {% for option in poll.options %}
        {% set percent = (option.votes / poll.total_votes * 100) | round(0) if poll.total_votes > 0 else 0 %}
        <button hx-post="/poll/vote/{{ option.id }}"
                hx-target="closest [data-message-id]" hx-swap="none"
                hx-on::after-request="htmx.trigger(closest('[data-message-id]'), 'loadPoll')"
                class="poll-option w-full p-3 rounded-lg text-sm overflow-hidden relative text-left
                       {% if option.id == my_vote %} poll-option-voted {% endif %}
                       {% if my_vote is not none %} cursor-default {% else %} hover:bg-slate-600 {% endif %}"
                {% if my_vote is not none %} disabled {% endif %}>
            
            <!-- Progress bar -->
            {% if my_vote is not none %}
            <div class="absolute top-0 left-0 h-full bg-blue-500/30" style="width: {{ percent }}%;"></div>
            {% endif %}
            
            <!-- Content -->
            <div class="relative flex justify-between">
                <span>{{ loop.index }}. {{ option.text }}</span>
                {% if my_vote is not none %}
                <span class="font-medium">x{{ option.votes }} | {{ percent }}%</span>
                {% endif %}
            </div>
        </button>
        {% endfor %}
    </div>
    <p class="text-xs text-slate-500 mt-3">Всего голосов: {{ poll.total_votes }}</p>
</div>
"""


# --- ЗАПУСК СЕРВЕРА ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создаем папку для загрузок, если ее нет
    UPLOADS_DIR.mkdir(exist_ok=True)
    print(f"--- Папка для загрузок готова: {UPLOADS_DIR.resolve()} ---")
    
    # Инициализируем БД при запуске
    init_db()
    
    print(f"--- ВНИМАНИЕ: Не забудьте указать TELEGRAM_NOTIFY_CHAT_ID в main.py! ---")
    yield

app = FastAPI(lifespan=lifespan)

# --- АУТЕНТИФИКАЦИЯ И ЗАВИСИМОСТИ ---

async def get_current_session(request: Request) -> dict | None:
    """Проверяет токен сессии из cookie и возвращает данные сессии."""
    token = request.cookies.get("session-token")
    if not token:
        return None
    
    session_data = active_sessions.get(token)
    if not session_data:
        return None
        
    # Проверка срока действия сессии
    if (time.time() - session_data.get("timestamp", 0)) > SESSION_DURATION:
        active_sessions.pop(token, None) # Удаляем просроченную сессию
        return None
        
    return session_data # Токен валиден, возвращаем данные

# --- ЭНДПОИНТЫ (АДРЕСА САЙТА) ---

@app.get("/", response_class=RedirectResponse)
async def read_root(session: Annotated[dict | None, Depends(get_current_session)]):
    """Корневой адрес."""
    if session:
        return RedirectResponse(url="/feed", status_code=303)
    return RedirectResponse(url="/login", status_code=307)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    """Показывает страницу входа с капчей."""
    if len(captcha_solutions) > 1000: captcha_solutions.clear()

    num1 = random.randint(1, 10)
    num2 = random.randint(1, 10)
    captcha_token = secrets.token_hex(16)
    captcha_solutions[captcha_token] = num1 + num2
    
    error_message = {
        "auth": "Неверный логин или пароль.",
        "captcha": "Неверный ответ на капчу.",
        "expired": "Сессия истекла, войдите снова."
    }.get(error)

    template = Template(LOGIN_TEMPLATE)
    return HTMLResponse(template.render(
        num1=num1, num2=num2, captcha_token=captcha_token, error=error_message
    ))

@app.post("/login")
async def login_process(
    response: Response,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    captcha_answer: Annotated[str, Form()],
    captcha_token: Annotated[str, Form()]
):
    """Обрабатывает форму входа."""
    
    # 1. Проверка капчи
    expected_answer = captcha_solutions.pop(captcha_token, None)
    try:
        if not expected_answer or int(captcha_answer) != expected_answer:
            return RedirectResponse(url="/login?error=captcha", status_code=303)
    except (ValueError, TypeError):
        return RedirectResponse(url="/login?error=captcha", status_code=303)

    # 2. Проверка пользователя в БД
    password_hash = hash_password(password)
    user = db.execute(
        "SELECT id, username, is_admin FROM users WHERE username = ? AND password_hash = ?",
        (username, password_hash)
    ).fetchone()

    if not user:
        return RedirectResponse(url="/login?error=auth", status_code=303)

    # 3. Успешный вход: Управление сессией (1 аккаунт - 1 сессия)
    old_token = next((token for token, data in active_sessions.items() if data["username"] == username), None)
    if old_token:
        active_sessions.pop(old_token, None)

    # Создаем новую сессию
    session_token = secrets.token_hex(32)
    active_sessions[session_token] = {
        "id": user["id"],
        "username": user["username"],
        "is_admin": user["is_admin"],
        "timestamp": time.time()
    }

    # Устанавливаем cookie и перенаправляем
    response = RedirectResponse(url="/feed", status_code=303)
    response.set_cookie(
        key="session-token", value=session_token, httponly=True, 
        max_age=SESSION_DURATION, samesite="Lax"
    )
    return response

@app.get("/logout", response_class=RedirectResponse)
async def logout(request: Request, response: Response):
    """Выход из системы."""
    token = request.cookies.get("session-token")
    if token:
        active_sessions.pop(token, None)
    
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session-token")
    return response

# --- ЗАЩИЩЕННЫЕ ЭНДПОИНТЫ ---

# Подача статических файлов (загрузок)
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")

@app.get("/feed", response_class=HTMLResponse)
async def read_feed(session: Annotated[dict | None, Depends(get_current_session)]):
    """Основная страница ленты."""
    if not session:
        return RedirectResponse(url="/login?error=expired", status_code=307)
        
    template = Template(PAGE_TEMPLATE)
    return HTMLResponse(template.render(is_admin=session.get("is_admin", 0)))

@app.get("/updates", response_class=HTMLResponse)
async def get_updates(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    session: Annotated[dict | None, Depends(get_current_session)]
):
    """HTMX эндпоинт для обновления ленты."""
    if not session:
        return HTMLResponse('<div class="text-red-500 p-4">Сессия истекла. Обновите страницу.</div>', status_code=401)
    
    # 1. Получаем все посты
    posts_raw = db.execute("SELECT * FROM posts ORDER BY created_at DESC LIMIT 30").fetchall()
    
    posts_list = []
    for post in posts_raw:
        post_dict = dict(post)
        
        # 3. Получаем данные опроса, если это опрос
        if post_dict["type"] == 'poll':
            poll = db.execute("SELECT id, question FROM polls WHERE post_id = ?", (post_dict["id"],)).fetchone()
            if poll:
                post_dict["poll"] = dict(poll)
        
        posts_list.append(post_dict)
        
    template = Template(UPDATES_TEMPLATE)
    return HTMLResponse(template.render(
        posts=posts_list, 
        time=time, 
        is_admin=session.get("is_admin", 0)
    ))

# --- ЭНДПОИНТЫ АДМИН-ПАНЕЛИ (СОЗДАНИЕ ПОСТОВ) ---

@app.post("/post/text")
async def create_text_post(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    session: Annotated[dict | None, Depends(get_current_session)],
    content: Annotated[str, Form()]
):
    """Админ создает текстовый пост."""
    if not session or not session.get("is_admin"):
        return HTMLResponse("Доступ запрещен", status_code=403)

    try:
        db.execute(
            "INSERT INTO posts (author_username, type, content, created_at) VALUES (?, ?, ?, ?)",
            (session["username"], 'text', content, int(time.time()))
        )
        db.commit()
        # Отправляем уведомление
        asyncio.create_task(send_telegram_notification(f"Текст: {content[:100]}..."))
        return Response(status_code=204)
    except Exception as e:
        return HTMLResponse(f"Ошибка БД: {e}", status_code=500)

@app.post("/post/file")
async def create_file_post(
    request: Request,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    session: Annotated[dict | None, Depends(get_current_session)]
):
    """Админ загружает файл (фото, видео, документ)."""
    if not session or not session.get("is_admin"):
        return HTMLResponse("Доступ запрещен", status_code=403)

    form_data = await request.form()
    file: UploadFile = form_data.get("file")
    caption: str = form_data.get("caption", "")

    if not file or not file.filename:
        return HTMLResponse("Файл не найден", status_code=400)

    # Определяем тип файла
    mime = file.content_type
    if mime.startswith("image"): post_type = "photo"
    elif mime.startswith("video"): post_type = "video"
    else: post_type = "file"
    
    # Генерируем уникальное имя файла, сохраняя оригинал
    original_filename = pathlib.Path(file.filename).name
    unique_prefix = secrets.token_hex(8)
    unique_filename = f"{unique_prefix}__{original_filename}"
    save_path = UPLOADS_DIR / unique_filename
    
    try:
        # Сохраняем файл на диск
        async with aiofiles.open(save_path, 'wb') as f:
            while chunk := await file.read(1024 * 1024): # Читаем по 1MB
                await f.write(chunk)
        
        # Сохраняем в БД
        db.execute(
            "INSERT INTO posts (author_username, type, content, caption, created_at) VALUES (?, ?, ?, ?, ?)",
            (session["username"], post_type, unique_filename, caption, int(time.time()))
        )
        db.commit()
        
        # Уведомление
        notification_text = f"{post_type.capitalize()}: {caption}" if caption else f"Новый файл: {original_filename}"
        asyncio.create_task(send_telegram_notification(notification_text))
        return Response(status_code=204)
        
    except Exception as e:
        print(f"Ошибка сохранения файла: {e}")
        return HTMLResponse(f"Ошибка сохранения файла: {e}", status_code=500)

@app.post("/post/poll")
async def create_poll_post(
    request: Request,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    session: Annotated[dict | None, Depends(get_current_session)]
):
    """Админ создает опрос."""
    if not session or not session.get("is_admin"):
        return HTMLResponse("Доступ запрещен", status_code=403)
        
    form_data = await request.form()
    question = form_data.get("question")
    options = [v for k, v in form_data.items() if k.startswith("option") and v]

    if not question or len(options) < 2:
        return HTMLResponse("Опрос должен иметь вопрос и минимум 2 варианта", status_code=400)

    try:
        cursor = db.cursor()
        # 1. Создаем пост
        cursor.execute(
            "INSERT INTO posts (author_username, type, content, created_at) VALUES (?, ?, ?, ?)",
            (session["username"], 'poll', question, int(time.time()))
        )
        post_id = cursor.lastrowid
        
        # 2. Создаем опрос
        cursor.execute("INSERT INTO polls (post_id, question) VALUES (?, ?)", (post_id, question))
        poll_id = cursor.lastrowid
        
        # 3. Добавляем варианты
        for option_text in options:
            cursor.execute("INSERT INTO poll_options (poll_id, text) VALUES (?, ?)", (poll_id, option_text))
            
        db.commit()
        asyncio.create_task(send_telegram_notification(f"Опрос: {question}"))
        return Response(status_code=204)
        
    except Exception as e:
        db.rollback()
        print(f"Ошибка создания опроса: {e}")
        return HTMLResponse(f"Ошибка БД: {e}", status_code=500)

# --- ЭНДПОИНТЫ ДЛЯ СМЕНЫ ФОРМ АДМИНА ---
@app.get("/form/text", response_class=HTMLResponse)
async def get_text_form(session: Annotated[dict | None, Depends(get_current_session)]):
    if not session or not session.get("is_admin"): return Response(status_code=403)
    return Template(TEXT_FORM_TEMPLATE).render()

@app.get("/form/poll", response_class=HTMLResponse)
async def get_poll_form(session: Annotated[dict | None, Depends(get_current_session)]):
    if not session or not session.get("is_admin"): return Response(status_code=403)
    return Template(POLL_FORM_TEMPLATE).render()

# --- ЭНДПОИНТЫ РЕАКЦИЙ, ОПРОСОВ И УДАЛЕНИЯ ---

@app.get("/react/{post_id}", response_class=HTMLResponse)
async def get_reactions(
    post_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    session: Annotated[dict | None, Depends(get_current_session)]
):
    """Получает секцию реакций для поста."""
    if not session: return Response(status_code=401)
    
    user_id = session["id"]
    is_admin = session["is_admin"]
    
    # 1. Считаем реакции
    reactions_raw = db.execute(
        "SELECT emoji, COUNT(user_id) as count FROM reactions WHERE post_id = ? GROUP BY emoji",
        (post_id,)
    ).fetchall()
    reactions = {r["emoji"]: r["count"] for r in reactions_raw}
    
    # 2. Получаем реакцию текущего пользователя
    my_reaction_raw = db.execute(
        "SELECT emoji FROM reactions WHERE user_id = ? AND post_id = ?",
        (user_id, post_id)
    ).fetchone()
    my_reaction = my_reaction_raw["emoji"] if my_reaction_raw else None
    
    allowed_emojis = ["👍", "❤️", "🔥", "🎉"]
    
    return Template(REACTIONS_TEMPLATE).render(
        reactions=reactions,
        my_reaction=my_reaction,
        post_id=post_id,
        is_admin=is_admin,
        allowed_emojis=allowed_emojis
    )

@app.post("/react/{post_id}")
async def set_reaction(
    post_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    session: Annotated[dict | None, Depends(get_current_session)],
    emoji: Annotated[str, Form()]
):
    """Пользователь ставит/снимает реакцию."""
    if not session: return Response(status_code=401)
    
    allowed_emojis = ["👍", "❤️", "🔥", "🎉"]
    if emoji not in allowed_emojis:
        return HTMLResponse("Недопустимый эмодзи", status_code=400)
    
    user_id = session["id"]
    
    try:
        # Пытаемся удалить - если пользователь уже ставил эту реакцию
        res = db.execute(
            "DELETE FROM reactions WHERE user_id = ? AND post_id = ? AND emoji = ?",
            (user_id, post_id, emoji)
        )
        if res.rowcount == 0:
            # Если ничего не удалено, значит, реакции не было.
            # Удаляем старую реакцию (если есть) и ставим новую
            db.execute(
                "INSERT OR REPLACE INTO reactions (user_id, post_id, emoji) VALUES (?, ?, ?)",
                (user_id, post_id, emoji)
            )
        db.commit()
        return Response(status_code=204) # OK, нет контента
        
    except Exception as e:
        db.rollback()
        print(f"Ошибка реакции: {e}")
        return HTMLResponse(f"Ошибка БД: {e}", status_code=500)

@app.get("/poll/{poll_id}", response_class=HTMLResponse)
async def get_poll(
    poll_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    session: Annotated[dict | None, Depends(get_current_session)]
):
    """Получает секцию опроса."""
    if not session: return Response(status_code=401)
    
    user_id = session["id"]

    poll = db.execute("SELECT id, question FROM polls WHERE id = ?", (poll_id,)).fetchone()
    if not poll:
        return HTMLResponse("Опрос не найден", status_code=404)
        
    options_raw = db.execute(
        """
        SELECT po.id, po.text, COUNT(pv.user_id) as votes
        FROM poll_options po
        LEFT JOIN poll_votes pv ON po.id = pv.option_id
        WHERE po.poll_id = ?
        GROUP BY po.id, po.text
        ORDER BY po.id
        """,
        (poll_id,)
    ).fetchall()
    
    my_vote_raw = db.execute(
        "SELECT option_id FROM poll_votes WHERE user_id = ? AND poll_id = ?",
        (user_id, poll_id)
    ).fetchone()
    
    poll_data = {
        "id": poll["id"],
        "question": poll["question"],
        "options": [dict(o) for o in options_raw],
        "total_votes": sum(o["votes"] for o in options_raw),
        "my_vote": my_vote_raw["option_id"] if my_vote_raw else None
    }

    return Template(POLL_TEMPLATE).render(poll=poll_data)

@app.post("/poll/vote/{option_id}")
async def vote_poll(
    option_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    session: Annotated[dict | None, Depends(get_current_session)]
):
    """Пользователь голосует в опросе."""
    if not session: return Response(status_code=401)
    
    user_id = session["id"]

    try:
        # Получаем poll_id из option_id
        poll_id_raw = db.execute("SELECT poll_id FROM poll_options WHERE id = ?", (option_id,)).fetchone()
        if not poll_id_raw:
            return HTMLResponse("Вариант не найден", status_code=404)
        poll_id = poll_id_raw["poll_id"]

        # Пытаемся вставить голос.
        # Ограничение PRIMARY KEY (user_id, poll_id) не даст проголосовать дважды.
        db.execute(
            "INSERT INTO poll_votes (user_id, option_id, poll_id) VALUES (?, ?, ?)",
            (user_id, option_id, poll_id)
        )
        db.commit()
        return Response(status_code=204)
        
    except sqlite3.IntegrityError:
        # Пользователь уже голосовал
        return Response(status_code=204) # Все равно OK, просто ничего не делаем
    except Exception as e:
        db.rollback()
        print(f"Ошибка голосования: {e}")
        return HTMLResponse(f"Ошибка БД: {e}", status_code=500)

@app.post("/message/delete/{post_id}")
async def delete_message(
    post_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    session: Annotated[dict | None, Depends(get_current_session)]
):
    """Админ удаляет пост."""
    if not session or not session.get("is_admin"):
        return HTMLResponse("Доступ запрещен", status_code=403)
    
    try:
        # Находим файл, если он есть, чтобы удалить
        post = db.execute("SELECT type, content FROM posts WHERE id = ?", (post_id,)).fetchone()
        
        # 1. Удаляем пост из БД (каскадное удаление удалит реакции/опросы)
        db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        db.commit()
        
        # 2. Удаляем связанный файл с диска
        if post and post["type"] in ['photo', 'video', 'file']:
            file_path = UPLOADS_DIR / post["content"]
            if file_path.exists():
                try:
                    file_path.unlink()
                    print(f"Файл {file_path} удален.")
                except Exception as e:
                    print(f"Ошибка удаления файла {file_path}: {e}")
            else:
                print(f"Файл {file_path} не найден для удаления.")
                
        return Response(status_code=200, content="") # OK, htmx удалит элемент
        
    except Exception as e:
        db.rollback()
        print(f"Ошибка удаления: {e}")
        return HTMLResponse(f"Ошибка БД: {e}", status_code=500)


# --- ЗАПУСК ---
if __name__ == "__main__":
    print("--- ЗАПУСК СЕРВЕРА (CMS-РЕЖИМ) ---")
    print(f"Сервер будет доступен по адресу http://0.0.0.0:8000")
    print(f"Убедитесь, что TELEGRAM_NOTIFY_CHAT_ID ('{TELEGRAM_NOTIFY_CHAT_ID}') указан верно.")
    uvicorn.run(app, host="0.0.0.0", port=8000)
