# Публикация Telegram-бота Medlife на сервере Reg.ru через Coolify

Пошаговая инструкция: от заказа VPS в Reg.ru до работающего бота в Coolify.

Бот работает через Telegram long polling: ему **не нужны домен, входящий HTTP-порт и reverse proxy**. Наружу открывается только панель Coolify. Контейнер бота постоянно обращается к `api.telegram.org` и API ProxyAPI.

```
Telegram → api.telegram.org ← long polling ← контейнер бота (Coolify, Reg.ru VPS)
                                              ├── /app/memory          (ChromaDB)
                                              └── /app/rag_knowledge   (исходные документы)
```

## Важные ограничения

- **Один экземпляр бота.** Нельзя запускать одновременно локального бота и бота на сервере с одним `BOT_TOKEN`, а также нельзя масштабировать контейнер. Иначе Telegram вернёт `TelegramConflictError: terminated by other getUpdates request`.
- **База знаний должна лежать на постоянном volume.** Без этого ChromaDB и загруженные документы исчезнут при каждом передеплое.
- **Секреты хранятся только в Coolify.** Файл `.env` не коммитится и не попадает в Docker-образ.
- **Версии зависимостей лучше зафиксировать.** Текущая база создана на `chromadb 1.5.9`. Если на сервере установится другая версия ChromaDB, перенесённая база может не открыться.

## Что понадобится

| Что | Где взять |
| --- | --- |
| VPS Reg.ru | личный кабинет Reg.ru |
| Приватный GitHub-репозиторий | `https://github.com/rmtatarinov-netizen/TG-BOT-WITH-MEMORY` |
| Токен бота | [@BotFather](https://t.me/BotFather) |
| Ключ ProxyAPI | личный кабинет ProxyAPI |
| Telegram ID администраторов | [@userinfobot](https://t.me/userinfobot) |
| Домен для панели Coolify (необязательно) | личный кабинет Reg.ru |

В командах ниже замените плейсхолдеры:

- `<SERVER_IP>` — публичный IP VPS;
- `<COOLIFY_DOMAIN>` — домен панели, например `coolify.example.ru`;
- `<APP_UUID>` — UUID приложения в Coolify;
- `<VOLUME_NAME>` — реальное имя Docker-volume;
- `<CONTAINER_NAME>` — имя контейнера бота.

---

## Шаг 1. Заказ и настройка VPS в Reg.ru

1. Войдите в личный кабинет Reg.ru.
2. Откройте раздел с VPS (облачный сервер).
3. Создайте сервер со следующими параметрами:

| Параметр | Значение |
| --- | --- |
| ОС | Ubuntu 24.04 LTS (64-bit) |
| CPU | 2 ядра или больше |
| RAM | 4 ГБ (минимум 2 ГБ + swap) |
| Диск | 20 ГБ SSD или больше |
| Архитектура | x86_64 / amd64 |

4. Запишите публичный IP-адрес сервера.
5. Получите root-пароль: он приходит на email или отображается в панели Reg.ru.
6. Если на сервере уже стоит другая ОС, при необходимости переустановите Ubuntu 24.04 LTS через панель. Переустановка удаляет все данные.

### Файрвол Reg.ru

В панели Reg.ru откройте настройки сети/файрвола для сервера и разрешите входящий TCP:

| Порт | Назначение |
| --- | --- |
| `22` | SSH |
| `80` | HTTP (панель Coolify и сертификаты) |
| `443` | HTTPS (панель Coolify и сертификаты) |
| `8000` | панель Coolify по IP |
| `6001` | live-обновления панели Coolify |
| `6002` | веб-терминал Coolify |

Если есть возможность, ограничьте порты `8000`, `6001`, `6002` только своим IP-адресом.

Не удаляйте правило для порта `22`, иначе потеряете доступ к серверу.

### Домен для панели Coolify (необязательно)

Если хотите открывать Coolify по домену:

1. В разделе доменов Reg.ru откройте DNS-зону.
2. Добавьте A-запись:
   - имя: `coolify`;
   - значение: `<SERVER_IP>`;
   - TTL: по умолчанию.
3. Дождитесь обновления DNS (обычно от 5 минут до нескольких часов).

Домен нужен только для удобства и автоматических webhook-деплоев. Бот будет работать и без него.

---

## Шаг 2. Первичная настройка сервера

Подключитесь к серверу из PowerShell на своём компьютере:

```powershell
ssh root@<SERVER_IP>
```

При первом подключении подтвердите fingerprint (`yes`) и введите root-пароль.

Обновите систему:

```bash
apt update && apt upgrade -y
```

Установите базовые утилиты (нужны для распаковки архива базы):

```bash
apt install -y curl wget git unzip ca-certificates
```

Если на VPS 2 ГБ RAM, создайте swap-файл на 2 ГБ:

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h
```

Проверьте доступ к Telegram API с сервера:

```bash
curl -I https://api.telegram.org
```

Ожидается ответ `HTTP/2 200` или `HTTP/1.1 200`. Если соединения нет, на этом сервере потребуется прокси (`TELEGRAM_PROXY`), но для VPS Reg.ru обычно доступ есть.

---

## Шаг 3. Установка Coolify

Выполните на сервере от имени root:

```bash
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash
```

Установщик поставит Docker, создаст каталоги `/data/coolify` и запустит Coolify. Дождитесь сообщения об успешной установке.

Откройте в браузере:

```
http://<SERVER_IP>:8000
```

Сразу создайте администратора Coolify. **Не оставляйте страницу регистрации открытой**: тот, кто зарегистрируется первым, получит полный доступ к серверу.

Сразу сделайте бэкап секретов установки:

```bash
cat /data/coolify/source/.env
```

Сохраните содержимое в надёжном месте. Файл содержит `APP_KEY` и пароли внутренних сервисов; без него восстановить Coolify после сбоя сложно.

Проверьте в панели:

- сервер `localhost` присутствует в разделе Servers;
- proxy запущен (Proxy is running).

### Домен для панели (если настроили DNS)

В Coolify откройте **Settings → Instance** (или настройки сервера) и укажите домен панели:

```
https://<COOLIFY_DOMAIN>
```

После успешного выпуска сертификата можно закрыть порты `8000`, `6001`, `6002` в файрволе Reg.ru и работать через `https://<COOLIFY_DOMAIN>`.

---

## Шаг 4. Подготовка репозитория к деплою

В репозитории пока нет Dockerfile. Coolify должен собирать контейнер по Dockerfile, поэтому добавьте два файла в корень репозитория.

### 4.1. `Dockerfile`

Создайте файл `Dockerfile` в корне проекта:

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/memory /app/rag_knowledge /app/uploads

CMD ["python", "bot.py"]
```

`libgomp1` нужен для работы ChromaDB/onnxruntime на Debian slim.

### 4.2. `.dockerignore`

Создайте файл `.dockerignore` в корне проекта:

```gitignore
.env
.env.*
.venv
.git
.gitignore
__pycache__
*.pyc
memory
rag_knowledge
uploads
*.zip
```

Это не даст попасть в образ секретам, виртуальному окружению и локальной базе. База на сервере будет жить в отдельном volume, а не внутри образа.

### 4.3. Зафиксируйте версии зависимостей

Чтобы серверная сборка не поставила более новую ChromaDB и не сломала перенесённую базу, приведите `requirements.txt` к виду:

```text
aiogram==3.31.0
openai==3.42.0
chromadb==1.5.9
pypdf==6.21.4
python-docx==1.2.0
aiohttp==3.14.5
aiohttp-socks==0.12.0
python-dotenv==1.2.4
```

Версии соответствуют локальному окружению, на котором создана текущая база. Если обновляете зависимости осознанно, сначала проверьте, что бот открывает существующую `memory/`.

### 4.4. Проверьте, что попадёт на сервер

`index_shared_document.py` сейчас указан в `.gitignore` и в GitHub отсутствует. Кроме того, в текущей версии он не совместим с сигнатурой `embed_chunks` в `bot.py`: скрипт передаёт лишний аргумент `SHARED_USER_ID`. Поэтому для пополнения базы используйте **админский чат бота**: документы от пользователей из `ADMIN_IDS` индексируются автоматически.

Если хотите пользоваться скриптом, сначала исправьте вызов в `index_shared_document.py`:

```python
count = await embed_chunks(client, doc_id, chunks)
```

затем добавьте файл в Git принудительно:

```powershell
git add -f index_shared_document.py
```

### 4.5. Исправьте блокирующую ошибку запуска

В текущей версии `bot.py` в сигнатуре `split_text` используется необъявленная константа `SEND_CHUNK`:

```python
def split_text(text: str, limit: int = SEND_CHUNK) -> List[str]:
```

Из-за этого `python bot.py` падает с `NameError: name 'SEND_CHUNK' is not defined` ещё до подключения к Telegram. Добавьте константу в блок настроек рядом с `TOP_K`:

```python
TOP_K = 5
SEND_CHUNK = 4000  # лимит длины одного сообщения Telegram
```

Проверьте, что модуль импортируется без ошибок:

```powershell
& .venv\Scripts\python.exe -c "import bot; print('ok')"
```

> Отдельно: в `bot.py` голосовые сообщения обрабатываются через `openai_client.audio.transcriptions`, но у клиента нужен метод `.create`. Если голосовые ответы не нужны сразу после публикации, это можно исправить позже: `openai_client.audio.transcriptions.create(...)`.

Закоммитьте изменения и отправьте в GitHub:

```powershell
git add Dockerfile .dockerignore requirements.txt bot.py
git commit -m "Add Docker deployment and fix startup"
git push origin main
```

> Папку `memory/` можно оставить в Git или перестать отслеживать. На сервере она всё равно будет перекрыта volume. Не удаляйте её локально, пока не перенесёте базу на сервер.

---

## Шаг 5. Подключение приватного репозитория GitHub

Репозиторий приватный, поэтому Coolify нужен read-only доступ по Deploy Key.

### 5.1. Создайте ключ в Coolify

1. Откройте **Keys & Tokens → Private Keys**.
2. Нажмите **New private key**.
3. Выберите **Generate ED25519**.
4. Сохраните ключ и скопируйте его **публичную** часть.

### 5.2. Добавьте ключ в GitHub

1. Откройте репозиторий на GitHub.
2. Перейдите в **Settings → Deploy keys → Add deploy key**.
3. Укажите название, например `coolify-medlife`.
4. Вставьте публичный ключ из Coolify.
5. **Allow write access** оставьте выключенным.
6. Нажмите **Add key**.

### 5.3. Создайте ресурс в Coolify

1. Откройте проект (создайте, например, `medlife`).
2. Нажмите **+ New**.
3. Выберите **Private Repository (with Deploy Key)**.
4. Выберите сервер `localhost`.
5. Выберите созданный приватный ключ.
6. Укажите SSH-URL репозитория:

```
git@github.com:rmtatarinov-netizen/TG-BOT-WITH-MEMORY.git
```

7. Ветка: `main`.
8. Build pack: **Dockerfile**.
9. Base Directory: `/`.
10. Dockerfile Location: `/Dockerfile`.
11. Продолжите создание приложения.

---

## Шаг 6. Настройка приложения в Coolify

Откройте созданное приложение и проверьте настройки.

### General / Build

| Параметр | Значение |
| --- | --- |
| Build Pack | Dockerfile |
| Base Directory | `/` |
| Dockerfile Location | `/Dockerfile` |
| Branch | `main` |

### Network

- **Domains** — не добавляйте домен.
- **Ports Exposes** — оставьте пустым. Если интерфейс требует значение, укажите `8000`; наружу порт не публикуется.
- **Ports Mappings** — не добавляйте.

Бот не слушает HTTP, поэтому домен и маршрутизация ему не нужны.

### Healthcheck

Отключите HTTP-healthcheck: у бота нет веб-эндпоинта. Если healthcheck включён и проверяет HTTP-порт, Coolify будет считать контейнер нездоровым.

### Масштабирование

Оставьте **1 экземпляр**. Не включайте scaling/replicas: два процесса с одним `BOT_TOKEN` конфликтуют.

### Auto Deploy

Можно включить, если панель Coolify доступна по домену через порты 80/443. Без домена GitHub-webhook может не доставляться — тогда деплой запускайте кнопкой **Redeploy**.

---

## Шаг 7. Переменные окружения

Откройте **Environment Variables** приложения и добавьте runtime-переменные:

| Переменная | Обязательна | Пример / значение |
| --- | --- | --- |
| `BOT_TOKEN` | да | `123456789:AA...` |
| `PROXYAPI_API_KEY` | да | `sk-...` |
| `PROXYAPI_BASE_URL` | да | `https://api.proxyapi.ru/v1` |
| `EMBED_MODEL` | нет | `text-embedding-3-small` |
| `CHAT_MODEL` | нет | `gpt-4o-mini` |
| `OPENAI_MODEL` | нет | `gpt-4o-mini` |
| `ADMIN_IDS` | да для загрузки документов | `123456789,987654321` |
| `TELEGRAM_PROXY` | нет | не задавать на Reg.ru |

Правила:

- `ADMIN_IDS` — числовые Telegram ID через запятую, без username.
- `EMBED_MODEL` должен совпадать с моделью, которой построена текущая база `memory/`. По умолчанию это `text-embedding-3-small`.
- `BOT_TOKEN` и `PROXYAPI_API_KEY` отметьте как secret/locked, если Coolify предлагает такую опцию.
- Не задавайте `TELEGRAM_PROXY`, если сервер напрямую открывает `api.telegram.org`.
- Не добавляйте `.env` в репозиторий.

---

## Шаг 8. Постоянные хранилища

> Перед первым запуском серверного бота остановите локальный `python bot.py`. Два процесса с одним `BOT_TOKEN` конфликтуют, даже если база на сервере ещё пустая.

Откройте **Configuration → Persistent Storage → Add → Volume Mount**.

Добавьте два обязательных volume:

| Name | Destination Path | Назначение |
| --- | --- | --- |
| `bot-memory` | `/app/memory` | ChromaDB и эмбеддинги |
| `bot-rag-knowledge` | `/app/rag_knowledge` | исходные PDF/TXT/DOCX |

Опционально (легаси-папка, бот её почти не использует):

| Name | Destination Path |
| --- | --- |
| `bot-uploads` | `/app/uploads` |

После добавления нажмите **Redeploy**, чтобы Coolify создал volume и пересоздал контейнер с монтированием.

Важно:

- Пути указаны относительно `WORKDIR /app` в Dockerfile: `./memory` → `/app/memory`, `./rag_knowledge` → `/app/rag_knowledge`.
- Не включайте автоматическое удаление неиспользуемых volume в настройках сервера.
- Volume — не бэкап. Настройте регулярные бэкапы (см. шаг 11).

---

## Шаг 9. Перенос текущей базы знаний

Сейчас нужные документы уже проиндексированы в локальной папке `memory/`. Перенесём её в volume на сервере.

### 9.1. Остановите локального бота

Если локально запущен `python bot.py`, остановите его. Иначе после запуска серверного бота Telegram будет отдавать обновления то одному, то другому процессу и появится `TelegramConflictError`.

### 9.2. Упакуйте базу на Windows

В PowerShell из корня проекта:

```powershell
Compress-Archive -Path .\memory -DestinationPath .\memory.zip -Force
```

### 9.3. Загрузите архив на сервер

```powershell
scp .\memory.zip root@<SERVER_IP>:/root/
```

### 9.4. Распакуйте архив на сервере

```bash
cd /root
unzip -o memory.zip -d /root/seed
ls -la /root/seed/memory
```

В папке должны быть `chroma.sqlite3` и каталоги с UUID.

### 9.5. Подготовьте volume

1. В Coolify задеплойте приложение один раз, чтобы создались volume.
2. Остановите приложение (Stop), чтобы ChromaDB не держала файлы открытыми.
3. Найдите реальное имя volume:

```bash
docker volume ls
```

Coolify добавляет к имени префикс с UUID ресурса. Найдите volume, в имени которого есть `bot-memory` (или заданный вами суффикс), и сохраните его полное имя в `<VOLUME_NAME>`.

### 9.6. Скопируйте базу в volume

```bash
docker run --rm \
  -v "<VOLUME_NAME>:/data" \
  -v /root/seed/memory:/seed:ro \
  alpine:3.20 sh -c "rm -rf /data/* /data/.[!.]* 2>/dev/null; cp -a /seed/. /data/"
```

Проверьте результат:

```bash
docker run --rm -v "<VOLUME_NAME>:/data" alpine:3.20 ls -la /data
```

Должны быть `chroma.sqlite3` и каталоги с UUID.

Исходные файлы из `rag_knowledge` для ответов не обязательны: бот ищет контекст в ChromaDB. Если хотите сохранить и их, отдельно упакуйте папку `rag_knowledge`, загрузите на сервер, распакуйте в `/root/seed/rag_knowledge` и скопируйте в volume `bot-rag-knowledge`:

```bash
docker run --rm \
  -v "<RAG_VOLUME_NAME>:/data" \
  -v /root/seed/rag_knowledge:/seed:ro \
  alpine:3.20 sh -c "cp -a /seed/. /data/"
```

### 9.7. Запустите приложение и проверьте базу

Запустите приложение в Coolify (Start/Redeploy). Затем на сервере:

```bash
docker ps --format '{{.Names}}'
```

Найдите контейнер приложения и выполните:

```bash
docker exec -it <CONTAINER_NAME> python -c "import chromadb; c=chromadb.PersistentClient('/app/memory').get_collection('long_memory'); print('chunks:', c.count())"
```

Число должно быть больше нуля и примерно совпадать с локальным.

> Если локальная и серверная версии ChromaDB сильно отличаются, база может не открыться. Поэтому версии в `requirements.txt` лучше зафиксировать (шаг 4.3).

---

## Шаг 10. Первый деплой и проверка

1. Нажмите **Deploy**.
2. Откройте **Deployment Logs** и дождитесь успешной сборки.
3. Откройте **Runtime Logs** и найдите строку:

```
Бот с короткой и долгой памятью запущен. Модель: ...
```

4. Проверьте бота в Telegram:
   - отправьте `/start` — должно прийти приветствие;
   - задайте обычный вопрос — должен прийти ответ;
   - задайте вопрос по документу из перенесённой базы — бот должен использовать контекст;
   - отправьте документ с аккаунта из `ADMIN_IDS` — бот должен ответить «Документ … сохранён … Чанков: N».
5. Сделайте **Redeploy** и повторите вопрос по документу. Ответ должен остаться корректным — значит, volume работает.
6. Убедитесь, что запущен ровно один контейнер бота:

```bash
docker ps --format '{{.Names}} {{.Status}}' | grep -i <APP_UUID>
```

7. Проверьте логи на отсутствие ошибок:

```bash
docker logs --tail 100 <CONTAINER_NAME>
```

Если в логах есть `TelegramConflictError`, остановите все другие процессы бота с тем же токеном.

> Отсутствие зелёного статуса «healthy» — нормально: у бота нет HTTP-healthcheck. Ориентируйтесь на логи и ответы в Telegram.

---

## Шаг 11. Обновление, бэкап и откат

### Обновление кода

1. Внесите изменения локально.
2. Закоммитьте и отправьте в GitHub:

```powershell
git add .
git commit -m "Update bot"
git push origin main
```

3. В Coolify нажмите **Redeploy** (или дождитесь автодеплоя, если настроен webhook).

### Бэкап volume

Остановите приложение в Coolify, затем на сервере:

```bash
mkdir -p /root/backups
docker run --rm \
  -v "<VOLUME_NAME>:/data:ro" \
  -v /root/backups:/backup \
  alpine:3.20 sh -c "tar czf /backup/memory-$(date +%F-%H%M).tar.gz -C /data ."
ls -lh /root/backups
```

Запустите приложение обратно. Скачайте архив на локальный компьютер:

```powershell
scp root@<SERVER_IP>:/root/backups/memory-*.tar.gz .
```

Дополнительно в Coolify можно настроить **Persistent Storage → Backups** для регулярных архивов volume. Храните хотя бы одну копию вне сервера.

### Восстановление из бэкапа

```bash
# приложение в Coolify остановлено
docker run --rm \
  -v "<VOLUME_NAME>:/data" \
  -v /root/backups:/backup \
  alpine:3.20 sh -c "rm -rf /data/* /data/.[!.]* 2>/dev/null; tar xzf /backup/memory-YYYY-MM-DD-HHMM.tar.gz -C /data"
```

Затем запустите приложение в Coolify.

### Откат версии

В Coolify откройте историю деплоев приложения и выберите предыдущий успешный деплой, затем **Rollback**. Либо верните предыдущий коммит в GitHub и нажмите **Redeploy**.

---

## Шаг 12. Диагностика

### Бот не отвечает в Telegram

1. Проверьте Runtime Logs: есть ли строка о запуске.
2. Проверьте, что контейнер запущен: `docker ps`.
3. Убедитесь, что локальный бот с тем же токеном остановлен.
4. Проверьте доступ к Telegram:

```bash
docker exec -it <CONTAINER_NAME> python -c "import urllib.request; print(urllib.request.urlopen('https://api.telegram.org', timeout=10).status)"
```

### `TelegramConflictError: terminated by other getUpdates request`

Запущено два процесса с одним `BOT_TOKEN`. Остановите локальный бот и убедитесь, что в Coolify ровно одна реплика.

### `TelegramNetworkError: Cannot connect to host api.telegram.org`

С сервера нет доступа к Telegram API. Проверьте `curl -I https://api.telegram.org`. Если доступ закрыт, укажите прокси в `TELEGRAM_PROXY` (например, `http://IP:PORT` или `socks5://IP:PORT`).

### `401 Unauthorized`

Неверный `PROXYAPI_API_KEY`. Проверьте ключ в Coolify.

### `404` или запросы уходят не туда

Проверьте `PROXYAPI_BASE_URL`. Для ProxyAPI: `https://api.proxyapi.ru/v1`.

### Ошибка при открытии ChromaDB / `Permission denied`

Проверьте, что volume смонтирован в `/app/memory` и что база перенесена. Контейнер запускается от root, поэтому права обычно не мешают. Если ошибка сохраняется, проверьте владельца файлов внутри volume:

```bash
docker run --rm -v "<VOLUME_NAME>:/data" alpine:3.20 ls -la /data
```

### База пустая после деплоя

- volume не смонтирован;
- перенос не выполнен;
- приложение было задеплоено с другим volume.

Повторите шаг 9 и проверьте `c.count()`.

### Ошибка размерности эмбеддингов

`EMBED_MODEL` не совпадает с моделью, которой строилась база. Верните прежнюю модель или переиндексируйте документы с нуля.

### Сборка падает или сервер зависает

ChromaDB и её зависимости тяжёлые. На 2 ГБ RAM добавьте swap (шаг 2) или увеличьте VPS до 4 ГБ. Проверьте свободное место:

```bash
df -h
docker system df
```

Если в логе сборки pip пытается компилировать пакет и падает из-за отсутствия компилятора, добавьте в `Dockerfile` в строку `apt-get install` пакет `build-essential`, затем пересоберите образ.

### `ModuleNotFoundError`

Сборка не установила зависимости. Проверьте, что `requirements.txt` попал в образ и сборка прошла без ошибок.

### `Для PDF требуется пакет pypdf` / `Для DOCX требуется пакет python-docx`

Проверьте, что пакеты есть в `requirements.txt`, и пересоберите образ.

---

## Чек-лист перед публикацией

- [ ] VPS Reg.ru создан, Ubuntu 24.04 LTS, доступ по SSH есть.
- [ ] Файрвол открывает `22`, `80`, `443`, `8000`, `6001`, `6002`.
- [ ] Coolify установлен, админ создан, `/data/coolify/source/.env` сохранён.
- [ ] В репозиторий добавлены `Dockerfile` и `.dockerignore`.
- [ ] Версии в `requirements.txt` зафиксированы (особенно `chromadb`).
- [ ] Deploy Key добавлен в GitHub, Coolify клонирует приватный репозиторий.
- [ ] Build pack — Dockerfile, Base Directory `/`, Dockerfile Location `/Dockerfile`.
- [ ] Домен и порты для приложения не настроены.
- [ ] Healthcheck отключён, реплика одна.
- [ ] Переменные окружения заданы, `BOT_TOKEN` и `PROXYAPI_API_KEY` помечены как секреты.
- [ ] Volume `/app/memory` и `/app/rag_knowledge` созданы.
- [ ] Локальная папка `memory/` перенесена в volume, `c.count()` больше нуля.
- [ ] Локальный бот остановлен.
- [ ] В Telegram работают `/start`, вопрос по документу и загрузка документа админом.
- [ ] После Redeploy база знаний сохраняется.
- [ ] Настроен бэкап volume.

---

## Полезные команды

```bash
# статус контейнеров
docker ps -a

# логи приложения
docker logs --tail 200 -f <CONTAINER_NAME>

# список volume
docker volume ls

# размер Docker-данных
docker system df

# проверить доступ к Telegram
curl -I https://api.telegram.org

# проверить ProxyAPI
curl -I https://api.proxyapi.ru

# содержимое volume
docker run --rm -v "<VOLUME_NAME>:/data" alpine:3.20 ls -la /data

# количество чанков в базе
docker exec -it <CONTAINER_NAME> python -c "import chromadb; c=chromadb.PersistentClient('/app/memory').get_collection('long_memory'); print('chunks:', c.count())"
```

```powershell
# подключение к серверу
ssh root@<SERVER_IP>

# загрузка файла на сервер
scp .\memory.zip root@<SERVER_IP>:/root/

# скачивание бэкапа
scp root@<SERVER_IP>:/root/backups/memory-*.tar.gz .
```
