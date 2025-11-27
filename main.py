import uvicorn
import asyncio
import httpx
import time
import secrets
import random
from fastapi import FastAPI, Request, Response, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from contextlib import asynccontextmanager
from jinja2 import Template
from typing import Annotated, Dict

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8312115174:AAEVrID17hc68rmxKtAHEOk4ZYyExEpHfAs" 
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
TELEGRAM_FILE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
TELEGRAM_FILE_PATH_URL = f"https://api.telegram.org/file/bot{BOT_TOKEN}/"
TELEGRAM_REACTION_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/setMessageReaction"
TELEGRAM_DELETE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"


# --- БАЗА ДАННЫХ ПОЛЬЗОВАТЕЛЕЙ (ВНУТРИ КОДА) ---
# Добавьте сюда ваши пары логин:пароль
USER_DB = {
    "user": "test",
    "admin": "vlasov",
}

# --- АДМИНИСТРАТОРЫ САЙТА ---
# Логины из USER_DB, которые получат доп. права (например, удаление)
ADMIN_USERS = [
    "admin", # Логин admin из USER_DB
]

# --- ХРАНИЛИЩА В ПАМЯТИ ---
messages_db = []
last_update_id = 0
CHANNEL_ID = None # Будет определен автоматически
# Хранит активные сессии: { "session_token_value": {"username": "user1", "is_admin": 0, "timestamp": 12345} }
active_sessions: Dict[str, dict] = {}
# Хранит решения для капчи: { "captcha_token_value": 8 }
captcha_solutions: Dict[str, int] = {}


# --- HTML ШАБЛОНЫ ---

# Шаблон 1: Страница Входа
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

# Шаблон 2: Основная страница (Лента)
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
        .bubble-telegram { background-color: #182533; }
        .poll-option { background-color: #374151; border: 1px solid #4b5563; }
        #messages-container { scroll-behavior: smooth; }
        video::-webkit-media-controls-enclosure { border-radius: 0; }
    </style>
</head>
<body class="bg-telegram text-slate-100 min-h-screen" hx-ext="morphdom-swap" onload="initNotifications()">

    <div class="max-w-2xl mx-auto flex flex-col h-screen">
        
        <header class="header-telegram flex items-center justify-between p-3 shadow-md sticky top-0 z-10">
            <div class="flex items-center gap-3">
                <img src="https://placehold.co/40x40/2B5278/FFFFFF?text=MC" alt="Avatar" class="w-10 h-10 rounded-full">
                <div>
                    <h1 class="font-bold text-base">Мой Канал</h1>
                    <div id="status-container" 
                         hx-get="/status" 
                         hx-trigger="every 5s" 
                         hx-swap="innerHTML"
                         class="text-xs text-slate-400">
                         Подключение...
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
              hx-trigger="load, every 2s" 
              hx-swap="morphdom">
            <div class="text-slate-500 text-center mt-10">Загрузка сообщений...</div>
        </main>
    </div>

<script>
    let lastKnownMessageId = 0;
    let notificationPermission = false;
    const NOTIFICATION_URL = "https://vlasovbot.onrender.com/";

    function initNotifications() {
        if (!("Notification" in window)) {
            console.log("Этот браузер не поддерживает уведомления.");
        } else if (Notification.permission === "granted") {
            notificationPermission = true;
        } else if (Notification.permission !== "denied") {
            Notification.requestPermission().then(permission => {
                if (permission === "granted") {
                    notificationPermission = true;
                }
            });
        }
    }

    document.body.addEventListener('htmx:afterSwap', function(evt) {
        if (evt.detail.elt.id === 'messages-container') {
            const firstMessage = evt.detail.elt.querySelector('.message-bubble-wrapper');
            if (firstMessage) {
                const newMessageId = firstMessage.dataset.messageId;
                if (newMessageId && parseInt(newMessageId) > lastKnownMessageId) {
                    if (lastKnownMessageId !== 0 && notificationPermission) {
                        try {
                            const notification = new Notification("Новое уведомление доступно!!!", {
                                body: `Нажмите, чтобы перейти на сайт ${NOTIFICATION_URL}`,
                                icon: "https://telegram.org/favicon.ico"
                            });
                            notification.onclick = function() {
                                window.open(NOTIFICATION_URL, '_blank');
                            };
                        } catch (e) {
                            console.error("Ошибка при показе уведомления:", e);
                        }
                    }
                    lastKnownMessageId = parseInt(newMessageId);
                }
            }
        }
    });
</script>
</body>
</html>
"""

# Шаблон 3: Карточки сообщений (С обновлением опросов и реакций)
UPDATES_TEMPLATE = """
{% for msg in messages %}
<div class="message-bubble-wrapper flex justify-start" data-message-id="{{ msg.id }}">
    <div class="bubble-telegram rounded-xl rounded-bl-none shadow-md max-w-lg">
        
        {% if msg.type == 'photo' or msg.type == 'video' %}
            {% if msg.type == 'photo' %}
                <img src="/media/{{ msg.content }}" 
                     class="w-full {% if msg.caption %}rounded-t-xl{% else %}rounded-xl{% endif %}" 
                     alt="Photo">
            {% elif msg.type == 'video' %}
                <video controls preload="metadata" 
                       class="w-full {% if msg.caption %}rounded-t-xl{% else %}rounded-xl{% endif %}" 
                       src="/media/{{ msg.content }}#t=0.1">
                </video>
            {% endif %}
            
            {% if msg.caption %}
                <p class="p-3 pt-2 text-slate-200 text-base whitespace-pre-wrap">{{ msg.caption }}</p>
            {% endif %}
        
        {% elif msg.type == 'text' %}
            <p class="p-3 text-slate-200 text-base whitespace-pre-wrap">{{ msg.content }}</p>

        {% elif msg.type == 'voice' %}
            <div class="p-3 flex items-center gap-2">
                <div class="text-blue-400">
                    <svg class="w-6 h-6" fill="currentColor" viewBox="0 0 20 20"><path d="M7 4a3 3 0 016 0v6a3 3 0 11-6 0V4z"></path><path fill-rule="evenodd" d="M5.5 8.5A.5.5 0 016 9v1a4 4 0 008 0V9a.5.5 0 011 0v1a5 5 0 01-4.5 4.975V17h3a.5.5 0 010 1h-7a.5.5 0 010-1h3v-2.025A5 5 0 015 10V9a.5.5 0 01.5-.5z" clip-rule="evenodd"></path></svg>
                </div>
                <audio controls class="w-full h-10" src="/media/{{ msg.content }}"></audio>
            </div>

        {% elif msg.type == 'file' %}
            <a href="/media/{{ msg.content }}" download="{{ msg.filename }}" 
               class="flex items-center gap-3 p-3 hover:bg-slate-700/50 rounded-xl transition-colors">
                <div class="flex-shrink-0 w-10 h-10 bg-blue-500 rounded-full flex items-center justify-center">
                    <svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
                </div>
                <div class="truncate flex-1">
                    <p class="text-slate-100 font-medium truncate">{{ msg.filename }}</p>
                    <span class="text-xs text-blue-400">Скачать</span>
                </div>
            </a>

        {% elif msg.type == 'poll' %}
            <div class="p-3">
                <p class="font-bold text-base mb-3">{{ msg.question }}</p>
                <div class="space-y-2">
                    {% for option in msg.options %}
                    {% set percent = (option.voter_count / msg.total_voters * 100) | round(0) if msg.total_voters > 0 else 0 %}
                    <div class="poll-option p-3 rounded-lg text-sm overflow-hidden relative">
                        <!-- Progress bar -->
                        <div class="absolute top-0 left-0 h-full bg-blue-500/30" style="width: {{ percent }}%;"></div>
                        <!-- Content -->
                        <div class="relative flex justify-between">
                            <span>{{ loop.index }}. {{ option.text }}</span>
                            <span class="font-medium">x{{ option.voter_count }} | {{ percent }}%</span>
                        </div>
                    </div>
                    {% endfor %}
                </div>
                <p class="text-xs text-slate-500 mt-3">Всего голосов: {{ msg.total_voters }}</p>
            </div>
        
        {% else %}
            <p class="p-3 text-slate-500 italic">[Неподдерживаемый тип контента]</p>
        {% endif %}

        <div class="flex justify-end items-center px-3 {% if msg.type != 'text' and msg.type != 'voice' and msg.type != 'poll' and msg.caption is none %}pb-2{% else %}pb-1{% endif %} {% if msg.type == 'photo' or msg.type == 'video' or msg.type == 'file' %}pt-1{% endif %}">
            <span class="text-xs text-slate-400">{{ time.strftime('%H:%M', time.localtime(msg.date)) }}</span>
        </div>

        <!-- Секция реакций (отображение + отправка) -->
        <div class="flex flex-wrap items-center gap-1 p-2 border-t border-slate-700/50">
            <!-- Отображение существующих реакций -->
            {% if msg.reactions %}
                {% for reaction in msg.reactions %}
                    <span class="bg-blue-500/20 text-blue-300 text-xs font-medium px-2 py-0.5 rounded-full border border-blue-500/30">
                        {{ reaction.emoji }} {{ reaction.count }}
                    </span>
                {% endfor %}
            {% endif %}
            
            <!-- Кнопки для добавления новых реакций -->
            <div class="ml-auto flex gap-1 items-center">
                <button hx-post="/react/{{ msg.id }}" hx-vals='{"emoji": "👍"}' hx-swap="none" class="px-1.5 py-0.5 rounded-full text-sm opacity-60 hover:opacity-100 hover:bg-slate-700 transition">👍</button>
                <button hx-post="/react/{{ msg.id }}" hx-vals='{"emoji": "❤️"}' hx-swap="none" class="px-1.5 py-0.5 rounded-full text-sm opacity-60 hover:opacity-100 hover:bg-slate-700 transition">❤️</button>
                <button hx-post="/react/{{ msg.id }}" hx-vals='{"emoji": "🔥"}' hx-swap="none" class="px-1.5 py-0.5 rounded-full text-sm opacity-60 hover:opacity-100 hover:bg-slate-700 transition">🔥</button>
                <button hx-post="/react/{{ msg.id }}" hx-vals='{"emoji": "🎉"}' hx-swap="none" class="px-1.5 py-0.5 rounded-full text-sm opacity-60 hover:opacity-100 hover:bg-slate-700 transition">🎉</button>
                
                <!-- Кнопка удаления (только для админов) -->
                {% if is_admin %}
                <button hx-post="/message/delete/{{ msg.id }}"
                        hx-target="closest .message-bubble-wrapper"
                        hx-swap="outerHTML"
                        class="ml-2 px-1.5 py-0.5 rounded-full text-sm text-red-500 opacity-60 hover:opacity-100 hover:bg-slate-700 transition"
                        title="Удалить пост">
                    &#x2715; <!-- Крестик -->
                </button>
                {% endif %}
            </div>
        </div>
    </div>
</div>
{% endfor %}
"""

# --- ФОНОВАЯ ЗАДАЧА (POLLING) ---
async def telegram_poller():
    global last_update_id, CHANNEL_ID
    print("🚀 Поллинг запущен! Слушаем Телеграм...")
    
    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get(
                    TELEGRAM_API_URL, 
                    params={"offset": last_update_id + 1, "timeout": 30, "allowed_updates": '["channel_post", "message_reaction", "edited_channel_post"]'},
                    timeout=35
                )
                data = response.json()
                
                if not data.get("ok"):
                    continue

                for update in data.get("result", []):
                    last_update_id = update["update_id"]

                    post = update.get("channel_post")
                    reaction_update = update.get("message_reaction")
                    edited_post = update.get("edited_channel_post")

                    # Определяем ID канала при первом же апдейте
                    if not CHANNEL_ID and (post or reaction_update or edited_post):
                        chat = post.get("chat") if post else (reaction_update.get("chat") if reaction_update else edited_post.get("chat"))
                        if chat:
                            CHANNEL_ID = chat["id"]
                            print(f"--- Канал ID установлен: {CHANNEL_ID} ---")
                    
                    if post:
                        # --- Это новый пост ---
                        msg = {
                            "id": post.get("message_id"),
                            "date": post.get("date"),
                            "reactions": []
                        }

                        if "text" in post:
                            msg.update({"type": "text", "content": post["text"]})
                        elif "photo" in post:
                            msg.update({
                                "type": "photo", 
                                "content": post["photo"][-1]["file_id"],
                                "caption": post.get("caption")
                            })
                        elif "video" in post:
                            msg.update({
                                "type": "video",
                                "content": post["video"]["file_id"],
                                "caption": post.get("caption")
                            })
                        elif "document" in post:
                            msg.update({
                                "type": "file",
                                "content": post["document"]["file_id"],
                                "filename": post["document"].get("file_name", "Скачать файл")
                            })
                        elif "voice" in post:
                            msg.update({
                                "type": "voice",
                                "content": post["voice"]["file_id"]
                            })
                        elif "poll" in post:
                            msg.update({
                                "type": "poll",
                                "question": post["poll"]["question"],
                                "options": post["poll"]["options"],
                                "total_voters": post["poll"].get("total_vvoter_count", 0)
                            })
                        else:
                            msg["type"] = "unsupported"
                            
                        if "reactions" in post:
                            msg["reactions"] = post["reactions"].get("reactions", [])

                        if msg["type"] != "unsupported":
                            messages_db.insert(0, msg)
                        
                        if len(messages_db) > 30:
                            messages_db.pop()
                    
                    elif reaction_update:
                        # --- Это обновление реакции ---
                        msg_id = reaction_update["message_id"]
                        new_reactions = reaction_update.get("new_reaction", [])
                        
                        # Находим сообщение в нашей БД и обновляем его реакции
                        for msg in messages_db:
                            if msg["id"] == msg_id:
                                msg["reactions"] = new_reactions
                                break
                    
                    elif edited_post:
                        # --- Это обновление (редактирование) поста ---
                        msg_id = edited_post["message_id"]
                        for msg in messages_db:
                            if msg["id"] == msg_id:
                                # Обновляем только то, что могло измениться
                                if "text" in edited_post:
                                    msg["content"] = edited_post["text"]
                                if "caption" in edited_post:
                                    msg["caption"] = edited_post.get("caption")
                                print(f"Обновлен (отредактирован) пост {msg_id}")
                                break
                        
                    else:
                        # Неизвестный тип апдейта, пропускаем
                        continue
                                
            except Exception as e:
                print(f"Ошибка поллинга: {e}")
                await asyncio.sleep(5)
            
            await asyncio.sleep(0.1)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if "8312115174" in BOT_TOKEN:
        asyncio.create_task(telegram_poller())
    else:
        print("⚠️ ВНИМАНИЕ: Вставьте токен бота в код!")
    yield

app = FastAPI(lifespan=lifespan)

# --- АУТЕНТИФИКАЦИЯ И ЗАВИСИМОСТИ ---

SESSION_DURATION = 86400 # 1 день

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
    """
    Корневой адрес. Перенаправляет на /feed если
    пользователь вошел, иначе на /login.
    """
    if session:
        return RedirectResponse(url="/feed", status_code=303)
    return RedirectResponse(url="/login", status_code=307)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    """Показывает страницу входа с капчей."""
    if len(captcha_solutions) > 1000: # Очистка старых капч
        captcha_solutions.clear()

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
        num1=num1, 
        num2=num2, 
        captcha_token=captcha_token, 
        error=error_message
    ))

@app.post("/login")
async def login_process(
    response: Response,
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
    except ValueError:
        return RedirectResponse(url="/login?error=captcha", status_code=303)

    # 2. Проверка пользователя
    user_pass = USER_DB.get(username)
    if not user_pass or user_pass != password:
        return RedirectResponse(url="/login?error=auth", status_code=303)

    # 3. Успешный вход: Управление сессией (1 аккаунт - 1 сессия)
    # Удаляем старую сессию, если она есть у этого пользователя
    old_token = next((token for token, data in active_sessions.items() if data["username"] == username), None)
    if old_token:
        active_sessions.pop(old_token, None)

    # Создаем новую сессию
    session_token = secrets.token_hex(32)
    is_admin = 1 if username in ADMIN_USERS else 0
    active_sessions[session_token] = {
        "username": username, 
        "is_admin": is_admin, 
        "timestamp": time.time()
    }

    # Устанавливаем cookie и перенаправляем
    response = RedirectResponse(url="/feed", status_code=303)
    response.set_cookie(
        key="session-token", 
        value=session_token, 
        httponly=True, 
        max_age=SESSION_DURATION,
        samesite="Lax"
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

# --- ЭНДПОИНТ ДЛЯ РЕАКЦИЙ ---

@app.post("/react/{message_id}")
async def set_reaction(
    message_id: int,
    request: Request,
    session: Annotated[dict | None, Depends(get_current_session)],
    emoji: Annotated[str, Form()]
):
    """
    Отправляет реакцию на пост в Telegram. Защищено.
    """
    if not session:
        return HTMLResponse("Ошибка сессии", status_code=401)
    
    if not CHANNEL_ID:
        print("Ошибка: CHANNEL_ID не установлен. Реакция не отправлена.")
        return HTMLResponse("ID канала не установлен", status_code=500)

    # Ограниченный список разрешенных эмодзи
    allowed_emojis = ["👍", "❤️", "🔥", "🎉", "👎", "👏", "😂"]
    if emoji not in allowed_emojis:
        return HTMLResponse("Недопустимый эмодзи", status_code=400)

    try:
        payload = {
            "chat_id": CHANNEL_ID,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}]
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(
                TELEGRAM_REACTION_URL,
                json=payload,
                timeout=10
            )
            r.raise_for_status() # Вызовет ошибку, если запрос неудачный
            
        return Response(status_code=204) 
        
    except Exception as e:
        print(f"Ошибка отправки реакции: {e}")
        return HTMLResponse(f"Ошибка API: {e}", status_code=502)

# --- ЭНДПОИНТ ДЛЯ УДАЛЕНИЯ ---

@app.post("/message/delete/{message_id}")
async def delete_message(
    message_id: int,
    request: Request,
    session: Annotated[dict | None, Depends(get_current_session)]
):
    """
    Удаляет сообщение из Telegram и из локальной БД. Защищено (только админы).
    """
    if not session:
        return HTMLResponse("Ошибка сессии", status_code=401)
        
    # Проверка прав администратора
    if not session.get("is_admin"):
        return HTMLResponse("Доступ запрещен", status_code=403)
    
    if not CHANNEL_ID:
        print("Ошибка: CHANNEL_ID не установлен. Удаление не удалось.")
        return HTMLResponse("ID канала не установлен", status_code=500)

    try:
        # 1. Удаляем из Telegram
        payload = {
            "chat_id": CHANNEL_ID,
            "message_id": message_id,
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(
                TELEGRAM_DELETE_URL,
                json=payload,
                timeout=10
            )
            r.raise_for_status()
            
        # 2. Удаляем из локальной БД
        for i, msg in enumerate(messages_db):
            if msg['id'] == message_id:
                messages_db.pop(i)
                break
                
        return Response(status_code=200, content="") 
        
    except Exception as e:
        print(f"Ошибка удаления сообщения: {e}")
        return HTMLResponse(f"Ошибка API: {e}", status_code=502)


# --- ЗАЩИЩЕННЫЕ ЭНДПОИНТЫ ---

@app.get("/feed", response_class=HTMLResponse)
async def read_feed(session: Annotated[dict | None, Depends(get_current_session)]):
    """
    Основная страница ленты. Защищено.
    """
    if not session:
        return RedirectResponse(url="/login?error=expired", status_code=307)
        
    template = Template(PAGE_TEMPLATE)
    return HTMLResponse(template.render(time=time))

@app.get("/updates", response_class=HTMLResponse)
async def get_updates(session: Annotated[dict | None, Depends(get_current_session)]):
    """
    HTMX эндпоинт для обновления ленты. Защищено.
    """
    if not session:
        return HTMLResponse('<div class="text-red-500 p-4">Сессия истекла. Обновите страницу.</div>', status_code=401)
    
    # Проверяем, админ ли пользователь
    is_admin = bool(session.get("is_admin", 0))
        
    template = Template(UPDATES_TEMPLATE)
    return HTMLResponse(template.render(messages=messages_db, time=time, is_admin=is_admin))

@app.get("/status", response_class=HTMLResponse)
async def get_status(session: Annotated[dict | None, Depends(get_current_session)]):
    """
    HTMX эндпоинт для статуса. Защищено.
    """
    if not session:
        return HTMLResponse('<span class="text-xs text-red-400">ошибка сессии</span>', status_code=401)
        
    return """
    <span class="text-xs text-green-400">в сети</span>
    """

@app.get("/media/{file_id}", response_class=RedirectResponse)
async def get_media(file_id: str, session: Annotated[dict | None, Depends(get_current_session)]):
    """
    Прокси для медиа. Защищено.
    """
    if not session:
        return RedirectResponse(url="/login?error=expired", status_code=307)

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(TELEGRAM_FILE_URL, params={"file_id": file_id}, timeout=10)
            r.raise_for_status()
            file_path = r.json()["result"]["file_path"]
            
            file_url = f"{TELEGRAM_FILE_PATH_URL}{file_path}"
            
            return RedirectResponse(url=file_url, status_code=307)
            
    except Exception as e:
        print(f"Ошибка проксирования медиа: {e}")
        return HTMLResponse(status_code=404, content="File not found")

# --- ЗАПУСК ---
if __name__ == "__main__":
    print("--- ВНИМАНИЕ ---")
    print("Не забудьте заполнить словарь USER_DB в main.py!")
    print("Не забудьте заполнить список ADMIN_USERS в main.py!")
    print("Убедитесь, что у бота есть права администратора в канале для отправки реакций и удаления.")
    print("----------------")
    uvicorn.run(app, host="0.0.0.0", port=8000)
