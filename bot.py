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
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

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
PROXYAPI_API_KEY = os.getenv("PROXYAPI_API_KEY", "").strip()
PROXYAPI_BASE_URL = os.getenv("PROXYAPI_BASE_URL", "").strip() or None

EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small").strip()
CHAT_MODEL = os.getenv("CHAT_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip()

PERSIST_DIR = "./memory"  # папка для ChromaDB
UPLOADS_DIR = "./uploads"  # папка для загруженных файлов
COLLECTION_NAME = "long_memory"
SHARED_USER_ID = "shared"  # документы, доступные всем пользователям

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
TOP_K = 5

# Сколько пар "пользователь + ассистент" помнить (10 пар = 20 сообщений).
MEMORY_PAIRS = 10
MAX_HISTORY_MESSAGES = MEMORY_PAIRS * 2

# Лимит Telegram — 4096 символов; отправляем кусками чуть меньше лимита.
SEND_CHUNK = 4000

SYSTEM_PROMPT = (
    "Ты дружелюбный и полезный ассистент в Telegram. "
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
    Path(UPLOADS_DIR).mkdir(parents=True, exist_ok=True)


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
    user_id: object,
    doc_id: str,
    chunks: List[str],
) -> int:
    """Вычислить эмбеддинги чанков и сохранить их в Chroma с метаданными."""
    if not chunks:
        return 0
    collection = get_chroma_collection()
    embeddings = await embed_texts(client, chunks)

    ids = [f"{user_id}:{doc_id}:{i}:{uuid.uuid4().hex}" for i in range(len(chunks))]
    metadatas = [
        {"user_id": str(user_id), "doc_id": doc_id, "chunk_index": i}
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
    user_id: int,
    question: str,
    top_k: int = TOP_K,
) -> List[str]:
    """Найти релевантные фрагменты личных и общих документов пользователя."""
    collection = get_chroma_collection()
    total = await asyncio.to_thread(collection.count)
    if total == 0:
        return []

    q_emb = (await embed_texts(client, [question]))[0]
    owner_filter = {
        "$or": [{"user_id": str(user_id)}, {"user_id": SHARED_USER_ID}]
    }
    result = await asyncio.to_thread(
        collection.query,
        query_embeddings=[q_emb],
        n_results=top_k,
        where=owner_filter,
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
        "Привет! Я бот с короткой и долгой памятью.\n\n"
        "Короткая память: я помню последние 10 пар сообщений нашего диалога.\n"
        "Долгая память: пришлите PDF/TXT/DOCX — я проиндексирую документ и буду "
        "отвечать на вопросы по нему.\n\n"
        "Команды:\n"
        "/reset — очистить историю диалога\n"
        "/reset_docs — удалить ваши загруженные документы\n"
        "/help — краткая справка"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Как пользоваться:\n"
        "1. Просто напишите вопрос — ответ придёт с учётом истории диалога.\n"
        "2. Отправьте документ PDF/TXT/DOCX — после индексации задавайте вопросы по нему.\n"
        "3. /reset — очистить короткую память.\n"
        "4. /reset_docs — удалить ваши документы из долгой памяти."
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


@router.message(Command("reset_docs"))
async def cmd_reset_docs(message: Message) -> None:
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    user_id = message.from_user.id
    try:
        await delete_user_documents(user_id)
    except Exception:
        logger.exception("Ошибка удаления документов для user_id=%s", user_id)
        await message.answer("Не удалось удалить документы. Попробуйте позже.")
        return

    logger.info("Документы удалены для user_id=%s", user_id)
    await message.answer("Ваши документы удалены из долгой памяти.")


# ---------------------------------------------------------------------------
# Документы (долгая память)
# ---------------------------------------------------------------------------


@router.message(F.document)
async def on_document(message: Message, bot: Bot, openai_client: AsyncOpenAI) -> None:
    """Скачать документ, проиндексировать и сохранить в Chroma."""
    if message.from_user is None or message.document is None:
        return

    user_id = message.from_user.id
    doc = message.document

    ensure_dirs()
    user_dir = Path(UPLOADS_DIR) / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    file_name = Path(doc.file_name or f"file_{uuid.uuid4().hex}").name
    save_path = user_dir / file_name

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
        count = await embed_chunks(openai_client, user_id, doc_id, chunks)
    except Exception:
        logger.exception("Embedding/Chroma error")
        await message.answer("Ошибка при сохранении в векторную базу.")
        return

    await message.answer(
        f"Документ «{file_name}» сохранён и проиндексирован. "
        f"Чанков: {count}. Теперь задайте вопрос."
    )


# ---------------------------------------------------------------------------
# Текст (короткая и долгая память)
# ---------------------------------------------------------------------------


@router.message(F.text & ~F.via_bot)
async def on_text(message: Message, openai_client: AsyncOpenAI) -> None:
    """Ответить с учётом истории диалога и документов пользователя."""
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    user_id = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришлите, пожалуйста, текстовое сообщение.")
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")

    async with get_lock(user_id):
        context_chunks: List[str] = []
        try:
            context_chunks = await retrieve_context(openai_client, user_id, text)
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


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Пока я умею работать только с текстом и документами PDF/TXT/DOCX.")


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

    bot = Bot(token=BOT_TOKEN)
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
