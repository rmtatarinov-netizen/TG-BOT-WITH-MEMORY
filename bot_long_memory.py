import asyncio
import os
import logging
import uuid
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, Document
from aiogram.filters import CommandStart
from aiogram.enums.parse_mode import ParseMode

from openai import OpenAI
from dotenv import load_dotenv
import chromadb

load_dotenv()

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# Опциональные зависимости для парсинга документов
try:
    from pypdf import PdfReader  # type: ignore
except Exception:
    PdfReader = None  # будет обработано в runtime

try:
    import docx  # python-docx
except Exception:
    docx = None  # будет обработано в runtime


# -----------------------------
# Константы и настройки
# -----------------------------
PERSIST_DIR = "./memory"  # папка для ChromaDB
UPLOADS_DIR = "./uploads"  # папка для загруженных файлов
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
SHARED_USER_ID = "shared"  # документы, доступные всем пользователям


router = Router()


def require_env(var_name: str) -> str:
    value = os.getenv(var_name)
    if not value:
        raise RuntimeError(f"Требуется переменная окружения {var_name}")
    return value


def ensure_dirs() -> None:
    Path(PERSIST_DIR).mkdir(parents=True, exist_ok=True)
    Path(UPLOADS_DIR).mkdir(parents=True, exist_ok=True)


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Разбиваем текст на пересекающиеся чанки ~chunk_size символов."""
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
    """Загружает документ (PDF/TXT/DOCX), извлекает текст и возвращает чанки."""
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
    elif suffix in (".docx", ".doc"):
        if docx is None:
            raise RuntimeError("Для DOCX требуется пакет python-docx. Установите: pip install python-docx")
        document = docx.Document(str(path))
        text = "\n".join(p.text for p in document.paragraphs)
    else:
        raise RuntimeError("Поддерживаются только PDF, TXT, DOCX")

    return chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)


def get_chroma_collection() -> "chromadb.api.models.Collection.Collection":
    """Возвращает/создаёт Chroma коллекцию с персистентным хранилищем."""
    client = chromadb.PersistentClient(path=PERSIST_DIR)
    collection = client.get_or_create_collection(name="long_memory", metadata={"hnsw:space": "cosine"})
    return collection


def embed_texts(client: OpenAI, texts: List[str]) -> List[List[float]]:
    """Получаем эмбеддинги для списка текстов с помощью OpenAI Embeddings API."""
    if not texts:
        return []
    resp = client.embeddings.create(model=os.getenv("EMBED_MODEL", "text-embedding-3-small"), input=texts)
    # Порядок соответствует входу
    return [d.embedding for d in resp.data]


def embed_chunks(client: OpenAI, user_id: object, doc_id: str, chunks: List[str]) -> int:
    """Вычисляет эмбеддинги чанков и сохраняет их в Chroma с метаданными."""
    if not chunks:
        return 0
    collection = get_chroma_collection()
    embeddings = embed_texts(client, chunks)

    ids = [f"{user_id}:{doc_id}:{i}:{uuid.uuid4().hex}" for i in range(len(chunks))]
    metadatas = [{"user_id": str(user_id), "doc_id": doc_id, "chunk_index": i} for i in range(len(chunks))]
    collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=chunks)
    return len(chunks)


def retrieve_context(client: OpenAI, user_id: int, question: str, top_k: int = 5, doc_id: Optional[str] = None) -> List[str]:
    """Находит релевантные фрагменты из Chroma по вопросу пользователя."""
    collection = get_chroma_collection()
    q_emb = embed_texts(client, [question])[0]

    owner_filter: Dict[str, object] = {
        "$or": [{"user_id": str(user_id)}, {"user_id": SHARED_USER_ID}]
    }
    where: Dict[str, object] = owner_filter
    if doc_id:
        where = {"$and": [{"doc_id": doc_id}, owner_filter]}

    result = collection.query(query_embeddings=[q_emb], n_results=top_k, where=where)
    docs = result.get("documents") or []
    if not docs:
        return []
    # docs — список списков; берём первую выдачу
    return docs[0]


async def answer_question(client: OpenAI, context_chunks: List[str], question: str) -> str:
    """Формирует ответ через OpenAI Chat Completions, строго опираясь на контекст."""
    system_prompt = (
        "Ты — помощник по документам. Отвечай ТОЛЬКО на основе предоставленного контекста. "
        "Если информации недостаточно, ответь: 'Не нашёл ответа в документе.'"
    )

    context_text = "\n\n".join(f"Фрагмент {i+1}:\n{c}" for i, c in enumerate(context_chunks)) or ""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Контекст:\n{context_text}\n\nВопрос: {question}"},
    ]

    completion = await asyncio.to_thread(
        client.chat.completions.create,
        model=os.getenv("CHAT_MODEL", "gpt-4o-mini"),
        messages=messages,
        temperature=0.2,
        max_tokens=600,
    )
    return completion.choices[0].message.content or ""


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "Привет! Загрузите PDF/TXT/DOCX — я сохраню его в векторной базе.\n"
        "После загрузки задайте вопрос по документу."
    )


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    """Обработка загрузки файла: сохраняем, индексируем, заносим в Chroma."""
    user_id = message.from_user.id if message.from_user else 0
    doc: Document = message.document

    try:
        openai_api_key = require_env("PROXYAPI_API_KEY")
    except RuntimeError as e:
        await message.answer(str(e))
        return

    client = OpenAI(api_key=openai_api_key, base_url=os.getenv("PROXYAPI_BASE_URL") or None)

    # Скачиваем файл во временную папку пользователя
    ensure_dirs()
    user_dir = Path(UPLOADS_DIR) / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    file_name = doc.file_name or f"file_{uuid.uuid4().hex}"
    save_path = user_dir / file_name

    try:
        file_info = await bot.get_file(doc.file_id)
        file_url = f"https://api.telegram.org/file/bot{bot.token}/{file_info.file_path}"
        # aiogram не предоставляет прямой скачки по URL в 3.x, используем HTTP
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                resp.raise_for_status()
                data = await resp.read()
        save_path.write_bytes(data)
    except Exception as e:
        logging.exception("File download error")
        await message.answer("Не удалось скачать файл. Попробуйте ещё раз.")
        return

    # Загружаем и чанкуем документ
    try:
        chunks = load_document(str(save_path))
    except Exception as e:
        await message.answer(str(e))
        return

    if not chunks:
        await message.answer("Не удалось извлечь текст из документа.")
        return

    # Вставляем эмбеддинги в Chroma
    doc_id = uuid.uuid4().hex
    try:
        count = embed_chunks(client, user_id, doc_id, chunks)
    except Exception:
        logging.exception("Embedding/Chroma error")
        await message.answer("Ошибка при сохранении в векторную базу.")
        return

    await message.answer(f"Документ сохранён и проиндексирован. Чанков: {count}. Теперь задайте вопрос.")


@router.message(F.text & ~F.via_bot)
async def on_question(message: Message) -> None:
    """Ответы на вопросы на основе векторной БД (RAG)."""
    user_id = message.from_user.id if message.from_user else 0
    question = message.text or ""

    try:
        openai_api_key = require_env("PROXYAPI_API_KEY")
    except RuntimeError as e:
        await message.answer(str(e))
        return

    client = OpenAI(api_key=openai_api_key, base_url=os.getenv("PROXYAPI_BASE_URL") or None)

    try:
        context_chunks = retrieve_context(client, user_id, question, top_k=5)
    except Exception:
        logging.exception("Retrieve error")
        await message.answer("Ошибка поиска по базе. Попробуйте позже.")
        return

    if not context_chunks:
        await message.answer("Контекст не найден. Загрузите документ и попробуйте снова.")
        return

    try:
        reply = await answer_question(client, context_chunks, question)
    except Exception:
        logging.exception("OpenAI Chat error")
        await message.answer("Не удалось получить ответ от модели.")
        return

    await message.answer(reply)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ensure_dirs()

    bot_token = require_env("BOT_TOKEN")
    bot = Bot(token=bot_token, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)

    logging.info("Бот (долгая память) запущен. Ожидаю сообщения...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass