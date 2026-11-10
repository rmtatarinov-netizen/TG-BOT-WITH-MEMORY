"""Индексирует документ в общую память бота.

Общие документы доступны всем пользователям бота.

Использование:
    python index_shared_document.py company_profile.txt
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from dotenv import load_dotenv
from openai import AsyncOpenAI

from bot import embed_chunks, ensure_dirs, load_document

load_dotenv()


async def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Использование: python index_shared_document.py <путь к файлу>")

    path = sys.argv[1]
    api_key = os.getenv("PROXYAPI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Не задана переменная окружения PROXYAPI_API_KEY")

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.getenv("PROXYAPI_BASE_URL") or None,
    )

    try:
        ensure_dirs()
        chunks = await asyncio.to_thread(load_document, path)
        if not chunks:
            raise SystemExit("Не удалось извлечь текст из документа")

        doc_id = uuid.uuid4().hex
        count = await embed_chunks(client, doc_id, chunks)
        print(
            f"Документ {path} проиндексирован как общий. "
            f"Чанков: {count}. doc_id: {doc_id}"
        )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
