import uvicorn
import asyncio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from contextlib import asynccontextmanager
from jinja2 import Template
import time

# --- НАСТРОЙКИ ---
BOT_TOKEN = "ВАШ_ТОКЕН_БОТА_ЗДЕСЬ" 
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
TELEGRAM_FILE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
TELEGRAM_FILE_PATH_URL = f"https://api.telegram.org/file/bot{BOT_TOKEN}/"

# База данных в памяти
messages_db = []
last_update_id = 0

# --- HTML ШАБЛОНЫ (Теперь с логикой Jinja2) ---

# Шаблон 1: Основная страница
PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Telegram Live Feed</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700&display=swap');
        body { font-family: 'Inter', sans-serif; }
        .fade-in { animation: fadeIn 0.5s ease-out; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        /* Стили для опросов */
        .poll-option { background-color: #374151; border: 1px solid #4b5563; }
        .poll-percent { background-color: #2563eb; }
    </style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen p-4">
    <div class="max-w-2xl mx-auto">
        <header class="flex items-center justify-between mb-8 py-4 border-b border-slate-700">
            <h1 class="text-xl font-bold">Мой Канал (Live)</h1>
            <div class="flex items-center gap-2" hx-get="/status" hx-trigger="every 2s" hx-swap="innerHTML">
                <div class="text-xs text-slate-400">Подключение...</div>
                <div class="w-2 h-2 bg-gray-500 rounded-full"></div>
            </div>
        </header>

        <!-- Лента сообщений -->
        <div id="messages-container" 
             hx-get="/updates" 
             hx-trigger="load, every 2s" 
             hx-swap="innerHTML">
             <div class="text-slate-500 text-center mt-10">Загрузка сообщений...</div>
        </div>
    </div>
</body>
</html>
"""

# Шаблон 2: Карточки сообщений (для /updates)
UPDATES_TEMPLATE = """
{% for msg in messages %}
<div class="fade-in bg-slate-800 border border-slate-700 rounded-xl p-4 mb-4 shadow-md">
    
    <!-- ТИП: ТЕКСТ -->
    {% if msg.type == 'text' %}
        <p class="text-slate-200 text-lg leading-relaxed whitespace-pre-wrap">{{ msg.content }}</p>
    
    <!-- ТИП: ФОТО -->
    {% elif msg.type == 'photo' %}
        <div class="rounded-lg overflow-hidden border border-slate-600">
            <img src="/media/{{ msg.content }}" alt="Photo" class="w-full h-auto">
        </div>
        {% if msg.caption %}
            <p class="text-slate-300 text-md mt-3">{{ msg.caption }}</p>
        {% endif %}

    <!-- ТИП: ВИДЕО -->
    {% elif msg.type == 'video' %}
        <div class="rounded-lg overflow-hidden bg-black">
            <video controls preload="metadata" class="w-full h-auto" src="/media/{{ msg.content }}#t=0.1"></video>
        </div>
        {% if msg.caption %}
            <p class="text-slate-300 text-md mt-3">{{ msg.caption }}</p>
        {% endif %}

    <!-- ТИП: ГОЛОСОВОЕ -->
    {% elif msg.type == 'voice' %}
        <div class="flex items-center gap-3">
            <div class="w-10 h-10 bg-blue-500 rounded-full flex-shrink-0 flex items-center justify-center">
                <svg class="w-6 h-6 text-white" fill="currentColor" viewBox="0 0 20 20"><path d="M7 4a3 3 0 016 0v6a3 3 0 11-6 0V4z"></path><path fill-rule="evenodd" d="M5.5 8.5A.5.5 0 016 9v1a4 4 0 008 0V9a.5.5 0 011 0v1a5 5 0 01-4.5 4.975V17h3a.5.5 0 010 1h-7a.5.5 0 010-1h3v-2.025A5 5 0 015 10V9a.5.5 0 01.5-.5z" clip-rule="evenodd"></path></svg>
            </div>
            <audio controls class="w-full" src="/media/{{ msg.content }}"></audio>
        </div>

    <!-- ТИП: ФАЙЛ/ДОКУМЕНТ -->
    {% elif msg.type == 'file' %}
        <a href="/media/{{ msg.content }}" download="{{ msg.filename }}" 
           class="flex items-center gap-4 p-4 bg-slate-700 hover:bg-slate-600 rounded-lg border border-slate-500 transition-colors">
            <div class="flex-shrink-0">
                <svg class="w-8 h-8 text-slate-300" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M4 4a2 2 0 012-2h8a2 2 0 012 2v12a2 2 0 01-2 2H6a2 2 0 01-2-2V4zm2 1a1 1 0 00-1 1v10a1 1 0 001 1h8a1 1 0 001-1V6a1 1 0 00-1-1H6z" clip-rule="evenodd"></path></svg>
            </div>
            <div class="truncate">
                <p class="text-slate-100 font-medium truncate">{{ msg.filename }}</p>
                <span class="text-xs text-blue-400">Нажмите для скачивания</span>
            </div>
        </a>

    <!-- ТИП: ОПРОС -->
    {% elif msg.type == 'poll' %}
        <p class="font-bold text-lg mb-3">{{ msg.question }}</p>
        <div class="space-y-2">
            {% for option in msg.options %}
            <div class="poll-option p-3 rounded-lg text-sm">
                <div class="flex justify-between">
                    <span>{{ option.text }}</span>
                    <span>{{ option.voter_count }}</span>
                </div>
            </div>
            {% endfor %}
        </div>
        <p class="text-xs text-slate-500 mt-3">Всего голосов: {{ msg.total_voters }}</p>
    
    {% else %}
        <p class="text-slate-500 italic">[Неподдерживаемый тип контента]</p>
    {% endif %}

    <!-- РЕАКЦИИ (отображаются для всех типов) -->
    {% if msg.reactions %}
    <div class="flex flex-wrap gap-2 pt-3 mt-3 border-t border-slate-700/50">
        {% for reaction in msg.reactions %}
            <span class="bg-blue-500/20 text-blue-300 text-xs font-medium px-2.5 py-1 rounded-full border border-blue-500/30">
                {{ reaction.emoji }} {{ reaction.count }}
            </span>
        {% endfor %}
    </div>
    {% endif %}
</div>
{% endfor %}
"""

# --- ФОНОВАЯ ЗАДАЧА (POLLING) ---
async def telegram_poller():
    global last_update_id
    print("🚀 Поллинг запущен! Слушаем Телеграм...")
    
    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get(
                    TELEGRAM_API_URL, 
                    params={"offset": last_update_id + 1, "timeout": 30, "allowed_updates": ["channel_post"]},
                    timeout=35
                )
                data = response.json()
                
                if not data.get("ok"):
                    continue

                for update in data.get("result", []):
                    last_update_id = update["update_id"]
                    
                    post = update.get("channel_post")
                    if not post:
                        continue

                    msg = {
                        "id": post.get("message_id"),
                        "date": post.get("date"),
                        "reactions": []
                    }

                    # --- Парсим типы контента ---
                    if "text" in post:
                        msg.update({"type": "text", "content": post["text"]})
                    
                    elif "photo" in post:
                        msg.update({
                            "type": "photo", 
                            "content": post["photo"][-1]["file_id"], # Берем самое большое фото
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
                            "total_voters": post["poll"].get("total_voter_count", 0)
                        })
                    
                    else:
                        msg["type"] = "unsupported" # Пропускаем
                        
                    # Парсим реакции
                    if "reactions" in post:
                        msg["reactions"] = post["reactions"].get("reactions", [])

                    # Добавляем в начало списка
                    if msg["type"] != "unsupported":
                        messages_db.insert(0, msg)
                    
                    # Храним только последние 30
                    if len(messages_db) > 30:
                        messages_db.pop()
                                
            except Exception as e:
                print(f"Ошибка поллинга: {e}")
                await asyncio.sleep(5)
            
            await asyncio.sleep(0.1)

# --- ЗАПУСК СЕРВERA И ФОНОВОЙ ЗАДАЧИ ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if "ВАШ_ТОКЕН" not in BOT_TOKEN:
        asyncio.create_task(telegram_poller())
    else:
        print("⚠️ ВНИМАНИЕ: Вставьте токен бота в код!")
    yield

app = FastAPI(lifespan=lifespan)

# --- ЭНДПОИНТЫ (АДРЕСА САЙТА) ---

@app.get("/", response_class=HTMLResponse)
async def read_root():
    template = Template(PAGE_TEMPLATE)
    return HTMLResponse(template.render())

@app.get("/updates", response_class=HTMLResponse)
async def get_updates():
    template = Template(UPDATES_TEMPLATE)
    return HTMLResponse(template.render(messages=messages_db))

@app.get("/status", response_class=HTMLResponse)
async def get_status():
    # Простой индикатор, что сервер жив
    return """
    <div class="text-xs text-green-400">Online</div>
    <div class="w-2 h-2 bg-green-500 rounded-full animate-pulse"></div>
    """

@app.get("/media/{file_id}", response_class=RedirectResponse)
async def get_media(file_id: str):
    """
    Это прокси-эндпоинт. Он получает file_id,
    запрашивает у Telegram временную ссылку и перенаправляет браузер.
    """
    try:
        async with httpx.AsyncClient() as client:
            # 1. Получаем путь к файлу
            r = await client.get(TELEGRAM_FILE_URL, params={"file_id": file_id}, timeout=10)
            r.raise_for_status() # Вызовет ошибку, если запрос неудачный
            file_path = r.json()["result"]["file_path"]
            
            # 2. Генерируем полную ссылку на файл
            file_url = f"{TELEGRAM_FILE_PATH_URL}{file_path}"
            
            # 3. Перенаправляем пользователя
            return RedirectResponse(url=file_url, status_code=307)
            
    except Exception as e:
        print(f"Ошибка проксирования медиа: {e}")
        # Возвращаем 404, если файл не найден
        return HTMLResponse(status_code=404, content="File not found")

# --- ЗАПУСК ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
