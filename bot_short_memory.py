"""Telegram-бот с короткой памятью диалога.

Бот хранит последние 10 пар сообщений (10 вопросов + 10 ответов) для каждого
пользователя в оперативной памяти и передаёт эту историю модели OpenAI при
каждом новом запросе.

Установка зависимостей:
    pip install "aiogram>=3.0" "openai>=1.0"

Запуск (PowerShell):
    $env:BOT_TOKEN = "123456:ABC..."
    $env:PROXYAPI_API_KEY = "sk-..."
    python bot_short_memory.py

Необязательная переменная OPENAI_MODEL (по умолчанию gpt-4o-mini).

Важно: память хранится только в оперативке и очищается при перезапуске бота.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PROXYAPI_API_KEY = os.getenv("PROXYAPI_API_KEY", "").strip()
PROXYAPI_BASE_URL = os.getenv("PROXYAPI_BASE_URL", "").strip() or None
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

# Сколько пар "пользователь + ассистент" помнить (10 пар = 20 сообщений).
MEMORY_PAIRS = 10
MAX_HISTORY_MESSAGES = MEMORY_PAIRS * 2

# Лимит Telegram — 4096 символов; отправляем кусками чуть меньше лимита.
SEND_CHUNK = 4000

SYSTEM_PROMPT = (
    "Ты дружелюбный и полезный ассистент в Telegram. "
    "Отвечай на языке пользователя, кратко и по делу. "
    "Учитывай контекст предыдущих сообщений диалога."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("short-memory-bot")

router = Router()

# Память: user_id -> очередь последних сообщений диалога.
# defaultdict создаёт deque нужного размера при первом обращении.
history: dict[int, deque[dict[str, str]]] = defaultdict(
    lambda: deque(maxlen=MAX_HISTORY_MESSAGES)
)

# Блокировки на пользователя, чтобы параллельные сообщения не перемешивали
# историю: пока один запрос в работе, следующий ждёт своей очереди.
locks: dict[int, asyncio.Lock] = {}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def get_lock(user_id: int) -> asyncio.Lock:
    """Вернуть (при необходимости создать) блокировку для пользователя."""
    lock = locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[user_id] = lock
    return lock


def remember(user_id: int, role: str, content: str) -> None:
    """Добавить сообщение в память пользователя (старые вытесняются сами)."""
    history[user_id].append({"role": role, "content": content})


def split_text(text: str, limit: int = SEND_CHUNK) -> list[str]:
    """Разбить длинный ответ на части, укладывающиеся в лимит Telegram."""
    parts: list[str] = []
    while len(text) > limit:
        # Стараемся резать по переносу строки, затем по пробелу.
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


async def ask_model(
    client: AsyncOpenAI,
    messages: list[dict[str, str]],
) -> str:
    """Отправить историю и текущее сообщение в OpenAI, вернуть текст ответа."""
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.7,
    )
    content = response.choices[0].message.content
    return content.strip() if content else "Не удалось сформировать ответ."


# ---------------------------------------------------------------------------
# Обработчики команд
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Команда /start — приветствие и краткая справка."""
    await message.answer(
        "Привет! Я помню последние 10 пар сообщений нашего диалога.\n\n"
        "Просто напиши вопрос — и я отвечу с учётом контекста.\n"
        "Команда /reset очистит память."
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    """Команда /reset — очистить историю диалога пользователя."""
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    user_id = message.from_user.id
    async with get_lock(user_id):
        history.pop(user_id, None)

    logger.info("Память очищена для user_id=%s", user_id)
    await message.answer("Память очищена. Начнём диалог заново!")


# ---------------------------------------------------------------------------
# Основной обработчик сообщений
# ---------------------------------------------------------------------------


@router.message(F.text)
async def handle_text(message: Message, openai_client: AsyncOpenAI) -> None:
    """Обработать текстовое сообщение: история + новый вопрос -> ответ модели."""
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    user_id = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришли, пожалуйста, текстовое сообщение.")
        return

    # Показываем пользователю, что бот печатает.
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")

    # Блокируем обработку сообщений этого пользователя на время запроса.
    async with get_lock(user_id):
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
        messages.extend(history[user_id])  # короткая память
        messages.append({"role": "user", "content": text})  # текущее сообщение

        try:
            answer = await ask_model(openai_client, messages)
        except Exception:
            logger.exception("Ошибка OpenAI для user_id=%s", user_id)
            await message.answer(
                "Не получилось получить ответ от модели. "
                "Попробуй ещё раз немного позже."
            )
            return

        # В память попадает только успешный обмен репликами.
        remember(user_id, "user", text)
        remember(user_id, "assistant", answer)

    # Длинный ответ отправляем несколькими сообщениями.
    for chunk in split_text(answer):
        await message.answer(chunk)


@router.message()
async def handle_other(message: Message) -> None:
    """Заглушка для нетекстовых сообщений (стикеры, фото и т.п.)."""
    await message.answer("Пока я умею работать только с текстовыми сообщениями.")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


async def main() -> None:
    """Проверить настройки, создать клиентов и запустить long polling."""
    if not BOT_TOKEN:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN")
    if not PROXYAPI_API_KEY:
        raise SystemExit("Не задана переменная окружения PROXYAPI_API_KEY")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    openai_client = AsyncOpenAI(api_key=PROXYAPI_API_KEY, base_url=PROXYAPI_BASE_URL)

    # Передаём клиент OpenAI в обработчики через dependency injection aiogram.
    dp["openai_client"] = openai_client

    try:
        # Сбрасываем вебхук, чтобы long polling работал без конфликтов.
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот запущен. Модель: %s", OPENAI_MODEL)
        await dp.start_polling(bot)
    finally:
        await openai_client.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            logger.error("Запуск отменён: %s", exc.code)
        else:
            logger.info("Бот остановлен.")
