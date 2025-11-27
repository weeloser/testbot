import uvicorn
import asyncio
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager

# --- НАСТРОЙКИ ---
# Получи токен у @BotFather и вставь сюда
BOT_TOKEN = "8312115174:AAEVrID17hc68rmxKtAHEOk4ZYyExEpHfAs" 
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

messages_db = []

# --- HTML ШАБЛОН ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Telegram Live Feed</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <style>
        body { font-family: sans-serif; }
        .fade-in { animation: fadeIn 0.5s ease-in; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
    </style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen p-4">
    <div class="max-w-2xl mx-auto">
        <header class="flex items-center justify-between mb-8 py-4 border-b border-slate-700">
            <h1 class="text-xl font-bold">Мой Канал (Live)</h1>
            <div class="flex items-center gap-2">
                <span class="text-xs text-slate-400">Обновляется каждые 2с</span>
                <div class="w-2 h-2 bg-green-500 rounded-full animate-pulse"></div>
            </div>
        </header>

        <!-- Лента сообщений -->
        <div id="messages-container" 
             hx-get="/updates" 
             hx-trigger="load, every 2s" 
             hx-swap="innerHTML">
             <div class="text-slate-500 text-center mt-10">Загрузка...</div>
        </div>
    </div>
</body>
</html>
"""

# --- ФОНОВАЯ ЗАДАЧА (POLLING) ---
async def telegram_poller():
    """Эта функция постоянно спрашивает Телеграм о новых сообщениях"""
    offset = 0
    print("🚀 Поллинг запущен! Слушаем Телеграм...")
    
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # Ждем 30 секунд новые сообщения (long polling)
                response = await client.get(
                    TELEGRAM_API_URL, 
                    params={"offset": offset, "timeout": 30},
                    timeout=35
                )
                data = response.json()
                
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        
                        # Если это пост в канале
                        if "channel_post" in update:
                            post = update["channel_post"]
                            text = post.get("text", "Фото/Видео (медиа)")
                            date_str = str(post.get("date", ""))
                            
                            print(f"Новое сообщение: {text[:20]}...")
                            
                            # Добавляем в начало списка
                            messages_db.insert(0, {
                                "text": text,
                                "date": date_str
                            })
                            
                            # Храним только последние 20
                            if len(messages_db) > 20:
                                messages_db.pop()
                                
            except Exception as e:
                print(f"Ошибка поллинга: {e}")
                await asyncio.sleep(5)
            
            # Небольшая пауза перед следующим запросом
            await asyncio.sleep(0.1)

# --- ЗАПУСК СЕРВЕРА ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем поллинг при старте
    if "ВАШ_ТОКЕН" not in BOT_TOKEN:
        asyncio.create_task(telegram_poller())
    else:
        print("⚠️ ВНИМАНИЕ: Вставьте токен бота в код!")
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def read_root():
    return HTML_TEMPLATE

@app.get("/updates", response_class=HTMLResponse)
async def get_updates():
    html = ""
    if not messages_db:
        return "<div class='text-center text-slate-600 mt-10'>Пока нет новых сообщений...</div>"
        
    for msg in messages_db:
        html += f"""
        <div class="fade-in bg-slate-800 border border-slate-700 rounded-xl p-4 mb-4 shadow-md">
            <p class="text-slate-200 text-lg leading-relaxed">{msg['text']}</p>
        </div>
        """
    return html

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)