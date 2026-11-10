"""Telegram-бот с короткой и долгой памятью.

Короткая память: последние 10 пар сообщений (вопрос + ответ) на пользователя
хранятся в оперативной памяти и передаются модели при каждом запросе.

Долгая память: документы PDF/TXT/DOCX индексируются в ChromaDB и используются
для ответов на вопросы (RAG). Личные документы видны только владельцу, общие
(shared) документы доступны всем пользователям.

Запуск:
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import List, Optional

import chromadb
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from openai import AsyncOpenAI

try:
    from aiohttp_socks import ProxyConnectionError, ProxyTimeoutError
except ImportError:
    ProxyConnectionError = ProxyTimeoutError = OSError

NETWORK_ERRORS = (TelegramNetworkError, ProxyConnectionError, ProxyTimeoutError, OSError)

load_dotenv()


def _extend_no_proxy() -> None:
    extra = {"api.proxyapi.ru", "localhost", "127.0.0.1"}
    for key in ("NO_PROXY", "no_proxy"):
        current = {item.strip() for item in os.getenv(key, "").split(",") if item.strip()}
        current.update(extra)
        os.environ[key] = ",".join(sorted(current))


_extend_no_proxy()

# Опциональные зависимости для парсинга документов
try:
    from pypdf import PdfReader  # type: ignore
except Exception:
    PdfReader = None

try:
    import docx  # python-docx
except Exception:
    docx = None


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip() or None
PROXYAPI_API_KEY = os.getenv("PROXYAPI_API_KEY", "").strip()
PROXYAPI_BASE_URL = os.getenv("PROXYAPI_BASE_URL", "").strip() or None

EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small").strip()
CHAT_MODEL = os.getenv("CHAT_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip()

PERSIST_DIR = "./memory"  # папка для ChromaDB
RAG_KNOWLEDGE_DIR = "./rag_knowledge"  # папка для файлов базы знаний
COLLECTION_NAME = "long_memory"
SHARED_USER_ID = "shared"  # документы, доступные всем пользователям

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
TOP_K = 5
SEND_CHUNK = 4000

# Сколько пар "пользователь + ассистент" помнить (10 пар = 20 сообщений).
MEMORY_PAIRS = 10
MAX_HISTORY_MESSAGES = MEMORY_PAIRS * 2

# Список администраторов, имеющих право пополнять базу RAG
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()]

SYSTEM_PROMPT = (
    "Ты дружелюбный и полезный ассистент в Telegram. "
    "Ты предназначен для консультации по компании «Medlife Insurance Ltd.» "
    "Отвечай на языке пользователя, кратко и по делу. "
    "Учитывай контекст предыдущих сообщений диалога. "
    "Если ниже передан контекст из загруженных документов, используй его, "
    "когда он относится к вопросу. Если вопрос явно касается документа, "
    "а ответа в контексте нет, скажи, что не нашёл ответа в документе."
)

logger = logging.getLogger("memory-bot")
router = Router()

# Короткая память: user_id -> очередь последних сообщений диалога.
history: dict[int, deque[dict[str, str]]] = defaultdict(
    lambda: deque(maxlen=MAX_HISTORY_MESSAGES)
)

# Блокировки на пользователя, чтобы параллельные сообщения не перемешивали
# историю: пока один запрос в работе, следующий ждёт своей очереди.
locks: dict[int, asyncio.Lock] = {}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def require_env(var_name: str) -> str:
    value = os.getenv(var_name)
    if not value:
        raise RuntimeError(f"Требуется переменная окружения {var_name}")
    return value


def ensure_dirs() -> None:
    Path(PERSIST_DIR).mkdir(parents=True, exist_ok=True)
    Path(RAG_KNOWLEDGE_DIR).mkdir(parents=True, exist_ok=True)


def get_lock(user_id: int) -> asyncio.Lock:
    lock = locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[user_id] = lock
    return lock


def remember(user_id: int, role: str, content: str) -> None:
    history[user_id].append({"role": role, "content": content})


def clear_history(user_id: int) -> None:
    history.pop(user_id, None)


def split_text(text: str, limit: int = SEND_CHUNK) -> List[str]:
    """Разбить длинный ответ на части, укладывающиеся в лимит Telegram."""
    parts: List[str] = []
    while len(text) > limit:
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


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Разбить текст на пересекающиеся чанки ~chunk_size символов."""
    if not text:
        return []
    chunks: List[str] = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == text_len:
            break
        start = end - overlap
        if start < 0:
            start = 0
    return chunks


def load_document(file_path: str) -> List[str]:
    """Загрузить документ (PDF/TXT/DOCX), извлечь текст и вернуть чанки."""
    path = Path(file_path)
    suffix = path.suffix.lower()
    text = ""

    if suffix == ".txt":
        text = path.read_text(encoding="utf-8", errors="ignore")
    elif suffix == ".pdf":
        if PdfReader is None:
            raise RuntimeError("Для PDF требуется пакет pypdf. Установите: pip install pypdf")
        reader = PdfReader(str(path))
        pages_text = []
        for page in reader.pages:
            try:
                pages_text.append(page.extract_text() or "")
            except Exception:
                pages_text.append("")
        text = "\n".join(pages_text)
    elif suffix == ".docx":
        if docx is None:
            raise RuntimeError("Для DOCX требуется пакет python-docx. Установите: pip install python-docx")
        document = docx.Document(str(path))
        text = "\n".join(p.text for p in document.paragraphs)
    else:
        raise RuntimeError("Поддерживаются только PDF, TXT, DOCX")

    return chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)


def get_chroma_collection() -> "chromadb.api.models.Collection.Collection":
    """Вернуть/создать Chroma-коллекцию с персистентным хранилищем."""
    client = chromadb.PersistentClient(path=PERSIST_DIR)
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


async def embed_texts(client: AsyncOpenAI, texts: List[str]) -> List[List[float]]:
    """Получить эмбеддинги для списка текстов."""
    if not texts:
        return []
    resp = await client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


async def embed_chunks(
    client: AsyncOpenAI,
    doc_id: str,
    chunks: List[str],
) -> int:
    """Вычислить эмбеддинги чанков и сохранить их в Chroma с метаданными (только для общей базы)."""
    if not chunks:
        return 0
    collection = get_chroma_collection()
    embeddings = await embed_texts(client, chunks)

    ids = [f"{SHARED_USER_ID}:{doc_id}:{i}:{uuid.uuid4().hex}" for i in range(len(chunks))]
    metadatas = [
        {"user_id": SHARED_USER_ID, "doc_id": doc_id, "chunk_index": i}
        for i in range(len(chunks))
    ]
    await asyncio.to_thread(
        collection.upsert,
        ids=ids,
        embeddings=embeddings,
        metadatas=metadatas,
        documents=chunks,
    )
    return len(chunks)


async def retrieve_context(
    client: AsyncOpenAI,
    question: str,
    top_k: int = TOP_K,
) -> List[str]:
    """Найти релевантные фрагменты из общей базы знаний."""
    collection = get_chroma_collection()
    total = await asyncio.to_thread(collection.count)
    if total == 0:
        return []

    q_emb = (await embed_texts(client, [question]))[0]
    result = await asyncio.to_thread(
        collection.query,
        query_embeddings=[q_emb],
        n_results=top_k,
        where={"user_id": SHARED_USER_ID},
    )
    docs = result.get("documents") or []
    if not docs:
        return []
    return docs[0]


async def delete_user_documents(user_id: int) -> None:
    """Удалить личные документы пользователя из Chroma."""
    collection = get_chroma_collection()
    await asyncio.to_thread(collection.delete, where={"user_id": str(user_id)})


async def ask_model(client: AsyncOpenAI, messages: List[dict[str, str]]) -> str:
    """Отправить сообщения в модель и вернуть текст ответа."""
    response = await client.chat.completions.create(
        model=CHAT_MODEL,
        messages=messages,
        temperature=0.5,
    )
    content = response.choices[0].message.content
    return content.strip() if content else "Не удалось сформировать ответ."


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Здравствуйте! Я AI-помощник компании Medlife Insurance Ltd.\n"
        "Помогу разобраться в программах страхования жизни и накопления капитала, "
        "расскажу об условиях и подготовлю вас к консультации со специалистом.\n\n"
        "Что для вас сейчас важнее?\n"
        "— Финансовая защита семьи.\n"
        "— Накопления на будущее ребёнка.\n"
        "— Инвестиции и формирование капитала.\n"
        "— Подготовка к пенсии.\n"
        "— Страхование сотрудников компании.\n\n"
        "Команды:\n"
        "/reset — очистить историю диалога\n"
        "/help — краткая справка"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Как пользоваться:\n"
        "1. Просто напишите вопрос или отправьте голосовое сообщение — я отвечу, используя историю диалога и базу знаний компании.\n"
        "2. /reset — очистить короткую память."
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    user_id = message.from_user.id
    async with get_lock(user_id):
        clear_history(user_id)

    logger.info("Короткая память очищена для user_id=%s", user_id)
    await message.answer("История диалога очищена. Начнём заново!")


# ---------------------------------------------------------------------------
# Документы (долгая память)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Документы (долгая память)
# ---------------------------------------------------------------------------


@router.message(F.document)
async def on_document(message: Message, bot: Bot, openai_client: AsyncOpenAI) -> None:
    """Скачать документ, проиндексировать и сохранить в Chroma. Только для админов."""
    if message.from_user is None or message.document is None:
        return

    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("У вас нет прав для загрузки документов в базу знаний.")
        return

    doc = message.document

    ensure_dirs()
    save_dir = Path(RAG_KNOWLEDGE_DIR)
    save_dir.mkdir(parents=True, exist_ok=True)

    file_name = Path(doc.file_name or f"file_{uuid.uuid4().hex}").name
    save_path = save_dir / file_name

    try:
        await bot.download(doc, destination=str(save_path))
    except Exception:
        logger.exception("File download error")
        await message.answer("Не удалось скачать файл. Попробуйте ещё раз.")
        return

    try:
        chunks = await asyncio.to_thread(load_document, str(save_path))
    except Exception as e:
        await message.answer(str(e))
        return

    if not chunks:
        await message.answer("Не удалось извлечь текст из документа.")
        return

    doc_id = uuid.uuid4().hex
    try:
        count = await embed_chunks(openai_client, doc_id, chunks)
    except Exception:
        logger.exception("Embedding/Chroma error")
        await message.answer("Ошибка при сохранении в векторную базу.")
        return

    await message.answer(
        f"Документ «{file_name}» сохранён в общую базу знаний и проиндексирован. "
        f"Чанков: {count}."
    )


# ---------------------------------------------------------------------------
# Текст (короткая и долгая память)
# ---------------------------------------------------------------------------


@router.message(F.text & ~F.via_bot)
async def on_text(message: Message, openai_client: AsyncOpenAI, processed_text: Optional[str] = None) -> None:
    """Ответить с учётом истории диалога и общей базы знаний."""
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    user_id = message.from_user.id
    text = (processed_text or message.text or "").strip()
    if not text:
        await message.answer("Пришлите, пожалуйста, текстовое сообщение.")
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")

    async with get_lock(user_id):
        context_chunks: List[str] = []
        try:
            context_chunks = await retrieve_context(openai_client, text)
        except Exception:
            logger.exception("Retrieve error для user_id=%s", user_id)

        messages: List[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

        if context_chunks:
            context_text = "\n\n".join(
                f"Фрагмент {i + 1}:\n{c}" for i, c in enumerate(context_chunks)
            )
            messages.append(
                {
                    "role": "system",
                    "content": f"Контекст из документов:\n\n{context_text}",
                }
            )

        messages.extend(history[user_id])
        messages.append({"role": "user", "content": text})

        try:
            answer = await ask_model(openai_client, messages)
        except Exception:
            logger.exception("Ошибка OpenAI для user_id=%s", user_id)
            await message.answer(
                "Не получилось получить ответ от модели. Попробуйте ещё раз позже."
            )
            return

        remember(user_id, "user", text)
        remember(user_id, "assistant", answer)

    for chunk in split_text(answer):
        await message.answer(chunk)


@router.message(F.voice)
async def on_voice(message: Message, openai_client: AsyncOpenAI) -> None:
    """Обработка голосовых сообщений через OpenAI Whisper."""
    if message.from_user is None or message.voice is None:
        return

    user_id = message.from_user.id
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        # Скачиваем голосовое сообщение во временный файл
        file_info = await message.voice.get_file()
        file_path = Path(f"voice_{user_id}_{uuid.uuid4().hex}.ogg")
        await message.bot.download_file(file_info.file_path, destination=str(file_path))

        # Распознаем голос через Whisper API
        with open(file_path, "rb") as audio_file:
            transcription = await openai_client.audio.transcriptions.create(
                file=audio_file,
                model="whisper-1",
            )

        text = transcription.text.strip()
        # Удаляем временный файл
        file_path.unlink()

        if not text:
            await message.answer("Не удалось распознать речь в вашем сообщении.")
            return

        # Передаем распознанный текст в основной обработчик текста
        await on_text(message, openai_client, processed_text=text)

    except Exception:
        logger.exception("Voice processing error для user_id=%s", user_id)
        await message.answer("Произошла ошибка при обработке голосового сообщения.")




# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ensure_dirs()

    if not BOT_TOKEN:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN")
    if not PROXYAPI_API_KEY:
        raise SystemExit("Не задана переменная окружения PROXYAPI_API_KEY")

    session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher()
    dp.include_router(router)

    openai_client = AsyncOpenAI(
        api_key=PROXYAPI_API_KEY,
        base_url=PROXYAPI_BASE_URL,
    )
    dp["openai_client"] = openai_client

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот с короткой и долгой памятью запущен. Модель: %s", CHAT_MODEL)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except NETWORK_ERRORS as exc:
        logger.error(
            "Нет связи с api.telegram.org: %s. Подключите VPN или задайте "
            "TELEGRAM_PROXY в .env (например, http://127.0.0.1:12334).",
            exc,
        )
        raise SystemExit(1) from exc
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
