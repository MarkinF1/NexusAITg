#!/usr/bin/env python3
"""Telegram-бот поверх подписок Claude и Codex: сообщение -> CLI движка -> ответ."""
import asyncio
import base64
import glob
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import time
import uuid
from collections import defaultdict
from contextlib import nullcontext
from functools import wraps
from pathlib import Path

import session_files
import topics
from topics import scope, is_topic

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.chat_action import ChatActionSender

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ["TG_TOKEN"]
# Администраторы и обычные пользователи — id через запятую и/или пробел.
ADMINS = [int(x) for x in os.environ["TG_ALLOWED"].replace(",", " ").split()]
USERS = [int(x) for x in os.environ.get("TG_USERS", "").replace(",", " ").split()]
ALLOWED = sorted(set(ADMINS + USERS))
GROUPS = {int(x) for x in os.environ.get("TG_GROUPS", "").replace(",", " ").split()}
SESSIONS = os.path.abspath(os.environ.get("CLAUDE_TG_SESSIONS", os.path.join(HERE, "sessions")))
HOST_CREDS = os.path.expanduser("~/.claude/.credentials.json")
MODELS = ["opus", "sonnet", "haiku"]  # алиасы claude: opus-5 / sonnet-5 / haiku-4.5
CLAUDE_MODEL_USES = {
    "opus": "для самых сложных задач, глубокого анализа и архитектуры",
    "sonnet": "универсальная модель для повседневной разработки",
    "haiku": "для быстрых и простых задач",
}
CODEX_DESCRIPTION_TRANSLATIONS = {
    "Our most capable model for complex, demanding work.":
        "для сложной работы, где важны максимальные возможности",
    "Reliable agentic workhorse for everyday tasks.":
        "надёжная универсальная модель для повседневных задач",
    "Balanced agentic coding model for everyday work.":
        "сбалансированная модель для повседневной разработки",
    "Fast and affordable agentic coding model.":
        "быстрая модель для несложных задач разработки",
    "Proven previous-generation model for coding and general work.":
        "проверенная модель прошлого поколения для кода и общих задач",
}
DEFAULT_MODEL = os.environ.get("CLAUDE_TG_MODEL", "sonnet")
DEFAULT_ENGINE = os.environ.get("CLAUDE_TG_ENGINE", "claude")
# У systemd-сервиса PATH урезанный и ~/.local/bin в него не входит — ищем сами.
CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or shutil.which("claude")
              or os.path.expanduser("~/.local/bin/claude"))
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex")
CODEX_HOST_AUTH = os.path.expanduser("~/.codex/auth.json")
# Каталог моделей codex качает сам и держит в своём CODEX_HOME — оттуда же берём
# список моделей и уровни размышления, чтобы не хардкодить их у себя.
MODELS_CACHE = os.path.expanduser("~/.codex/models_cache.json")
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]  # claude --effort
MARKS = {"claude": "🟠", "codex": "🟢"}
ADMIN_ONLY_MODELS = {"gpt-6-astra"}
# Claude Code сам подкладывает в контекст email аккаунта подписки — это владелец
# бота, а не собеседник; без запрета модель записывала его в общую память.
SYSTEM_PROMPT = ("Ты отвечаешь пользователю в Telegram. Пиши кратко, без таблиц и без сложной markdown-разметки.\n"
                 "Email из служебного контекста принадлежит владельцу бота, а не собеседнику: "
                 "никогда не называй его, не используй и не записывай в память и файлы.")
# WebFetch и WebSearch --restricted вырезает, если не перечислить их здесь.
FILE_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,NotebookEdit,WebFetch,WebSearch"
LIMIT = 4000  # лимит Telegram 4096, с запасом
TIMEOUT = 600  # обработка файлов может требовать нескольких вызовов инструментов
CODEX_TIMEOUT = 600  # на высоких уровнях размышления codex думает дольше
PAGE = 5  # сессий на страницу списка
# Один список сессий работает в двух режимах: выбрать и удалить.
LISTS = {"sess": "Сессии (✓ — текущая):", "del": "Какую сессию удалить?"}
MAX_IMAGE = 5 * 1024 * 1024  # больше API не принимает
MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_UPLOAD = 50 * 1024 * 1024
MAX_ALBUM = 15 * 1024 * 1024  # суммарно на один запрос с альбомом
ALBUM_WAIT = 1.5  # альбом приходит пачкой сообщений, ждём остальные
STT_MODEL = os.environ.get("CLAUDE_TG_STT_MODEL", "small")
STT_LANG = os.environ.get("CLAUDE_TG_STT_LANG") or None  # пусто = определять само

log = logging.getLogger("claude-tg")
dp = Dispatcher()
stt = None  # модель распознавания грузится при первом голосовом
stt_lock = asyncio.Lock()
# Альбом Telegram шлёт отдельными сообщениями с общим media_group_id — копим их
# и отдаём Claude одним запросом, когда поток сообщений прекратился.
albums = defaultdict(list)
album_deadline = {}
# Одна сессия claude не переживёт два параллельных --resume, поэтому запросы
# одной беседы сериализуются. Разные темы и личные чаты идут параллельно.
locks = defaultdict(asyncio.Lock)
# Два claude, одновременно обновляющие токен, гасят друг друга: выигрывает один,
# второй остаётся с отозванным refresh-токеном. Перед истечением — по одному.
creds_lock = asyncio.Lock()


def topic_store():
    return topics.Store(SESSIONS)


def topic_manager(key, user_id):
    return user_id in ADMINS or user_id == topic_store().get(key).get("owner_id")


def topic_settings(handler):
    """Настройки общей беседы меняет её создатель или администратор бота."""
    @wraps(handler)
    async def wrapped(event, *args, **kwargs):
        callback = isinstance(event, CallbackQuery) or hasattr(event, "data")
        message = event.message if callback else event
        key = scope(message)
        if is_topic(key) and not topic_manager(key, event.from_user.id):
            text = "Настройки темы меняет её создатель или администратор бота."
            await (event.answer(text, show_alert=True) if callback else message.answer(text))
            return
        return await handler(event, *args, **kwargs)
    return wrapped


def private_sessions_only(handler):
    @wraps(handler)
    async def wrapped(call, *args, **kwargs):
        if is_topic(scope(call.message)):
            await call.answer("У каждой темы своя сессия. Для перехода: /sessions", show_alert=True)
            return
        return await handler(call, *args, **kwargs)
    return wrapped


class ForumAccess(BaseMiddleware):
    async def __call__(self, handler, event, data):
        callback = isinstance(event, CallbackQuery)
        message = event.message if callback else event
        if not message or not getattr(message.chat, "is_forum", False):
            return await handler(event, data)
        actor = event.from_user
        lifecycle = any(getattr(message, name, None) is not None for name in (
            "forum_topic_created", "forum_topic_edited", "forum_topic_closed", "forum_topic_reopened",
            "general_forum_topic_hidden", "general_forum_topic_unhidden"))
        group_id = message.chat.id
        if group_id not in GROUPS and not topic_store().group_enabled(group_id):
            command = (getattr(message, "text", None) or "").split(maxsplit=1)
            command = command[0].split("@")[0] if command else ""
            if actor and actor.id in ALLOWED and not callback and (command in ("/start", "/new") or getattr(message, "forum_topic_created", None)):
                topic_store().enable_group(group_id)
            else:
                if actor and actor.id in ALLOWED:
                    text = "Для подключения группы отправьте /start."
                    await (event.answer(text, show_alert=True) if callback else message.answer(text))
                return
        if not lifecycle and (not actor or actor.is_bot):
            return
        return await handler(event, data)


dp.message.outer_middleware(ForumAccess())
dp.callback_query.outer_middleware(ForumAccess())


def serialized_chat(handler):
    """Настройки сессии не меняются посреди скачивания/ответа/отправки файлов."""
    @wraps(handler)
    async def wrapped(event, *args, **kwargs):
        message = event.message if isinstance(event, CallbackQuery) or hasattr(event, "data") else event
        key = scope(message)
        if is_topic(key) and isinstance(event, CallbackQuery) and locks[key].locked():
            await event.answer("Дождитесь завершения ответа в этой теме и нажмите ещё раз.", show_alert=True)
            return
        async with locks[key]:
            return await handler(event, *args, **kwargs)
    return wrapped


def split_message(text, limit=LIMIT):
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


def session_id(chat_id):
    """Детерминированный id сессии: контекст переживает рестарт бота без файла состояния."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"claude-tg-{chat_id}"))


def creds_freshness(path):
    """Чем больше, тем свежее: срок токена claude, иначе время записи файла."""
    try:
        with open(path) as f:
            expires = (json.load(f).get("claudeAiOauth") or {}).get("expiresAt") or 0
    except (OSError, ValueError, AttributeError):
        expires = 0
    try:
        return expires, os.path.getmtime(path)
    except OSError:
        return expires, 0


def link_creds(link, host):
    """Токен подписки один на всех, у каждого чата — симлинк на него."""
    if os.path.islink(link):
        return
    if os.path.exists(link):
        # claude обновляет токен атомарной заменой файла, и симлинк становится
        # копией. Refresh-токен одноразовый: старый у хоста уже не работает,
        # поэтому возвращаем хосту копию — но только если она свежее. Копия
        # после неудачного обновления пустая и не должна затереть /login.
        if not os.path.exists(host) or creds_freshness(link) >= creds_freshness(host):
            shutil.copy2(link, host)
        os.remove(link)
    os.symlink(host, link)


def creds_expiring(margin):
    """Токен вот-вот обновится — тогда claude запускаем по одному."""
    expires, _ = creds_freshness(HOST_CREDS)
    return expires and expires / 1000 - time.time() < margin


def chat_dir(chat_id):
    """Отдельный CLAUDE_CONFIG_DIR и CODEX_HOME на каждую беседу/тему.

    Аккаунты полностью изолированы: своя история, свой .claude.json, своя база
    сессий codex. Токены подписок общие — симлинками на хостовые файлы.
    """
    d = str(topics.storage_path(SESSIONS, chat_id))
    os.makedirs(d, exist_ok=True)
    link_creds(os.path.join(d, ".credentials.json"), HOST_CREDS)
    home = os.path.join(d, "codex-home")
    os.makedirs(home, exist_ok=True)
    link_creds(os.path.join(home, "auth.json"), CODEX_HOST_AUTH)
    config = os.path.join(home, "config.toml")
    if not os.path.exists(config):
        # У `codex exec resume` нет флагов песочницы и системной инструкции —
        # они задаются только конфигом, поэтому кладём их в CODEX_HOME чата.
        with open(config, "w") as f:
            f.write('sandbox_mode = "read-only"\napproval_policy = "never"\n'
                    f'developer_instructions = {json.dumps(SYSTEM_PROMPT)}\n')
    return d


def state(chat_id, name, default=""):
    """Настройки чата — по файлу на каждую, рядом с его сессиями."""
    try:
        with open(os.path.join(chat_dir(chat_id), name)) as f:
            return f.read().strip() or default
    except OSError:
        return default


def set_state(chat_id, name, value):
    with open(os.path.join(chat_dir(chat_id), name), "w") as f:
        f.write(value)


def get_engine(chat_id):
    if is_topic(chat_id) and topic_store().get(chat_id).get("engine"):
        return topic_store().get(chat_id)["engine"]
    choice = state(chat_id, "engine", DEFAULT_ENGINE)
    return choice if choice in MARKS else "claude"


def get_model(chat_id, engine=None):
    """Модель выбирается на чат, у каждого движка своя."""
    engine = engine or get_engine(chat_id)
    if engine == "claude":
        choice = state(chat_id, "model", DEFAULT_MODEL)
        return choice if choice in MODELS else DEFAULT_MODEL
    known = [slug for slug, _, _, _ in codex_models()]
    choice = state(chat_id, "model-codex")
    return choice if choice in known else (known[0] if known else "")


def set_model(chat_id, choice, engine="claude"):
    set_state(chat_id, "model" if engine == "claude" else "model-codex", choice)


def get_effort(chat_id, engine=None):
    """Уровень размышления; пусто — не передавать флаг, пусть решает сам CLI."""
    engine = engine or get_engine(chat_id)
    if engine == "claude":
        choice = state(chat_id, "effort")
        return choice if choice in CLAUDE_EFFORTS else ""
    choice = state(chat_id, "effort-codex")
    return choice if choice in codex_efforts(get_model(chat_id, "codex")) else ""


def set_effort(chat_id, level, engine="claude"):
    set_state(chat_id, "effort" if engine == "claude" else "effort-codex", level)


def active_session(chat_id, engine=None):
    """Активная сессия движка; для старых чатов без файла — прежний uuid5.

    У codex id выдаёт он сам при первом ответе, поэтому до него сессия пустая.
    """
    engine = engine or get_engine(chat_id)
    if engine == "codex":
        return state(chat_id, "active-codex")
    return state(chat_id, "active") or session_id(chat_id)


def set_active_session(chat_id, sid, engine="claude"):
    set_state(chat_id, "active" if engine == "claude" else "active-codex", sid)
    if engine == "codex" and not sid:
        set_state(chat_id, "pending-workspace-codex", uuid.uuid4().hex)


def data_dir(chat_id):
    """Всё, что доступно модели: общая память и рабочие папки всех бесед чата,
    у группы — всех её тем. Токены, настройки и транскрипты лежат рядом, вне неё."""
    owner = chat_id[0] if is_topic(chat_id) else chat_id
    return os.path.join(SESSIONS, str(int(owner)), "data")


def memory_file(chat_id):
    return session_files.shared_memory(data_dir(chat_id))


def workspace_dir(chat_id, engine=None, sid=None):
    """Рабочая папка беседы внутри data_dir: своя у каждой сессии или темы."""
    if is_topic(chat_id):
        return session_files.prepare(os.path.join(data_dir(chat_id), "topics", str(int(chat_id[1]))),
                                     memory_file(chat_id))
    engine = engine or get_engine(chat_id)
    sid = active_session(chat_id, engine) if sid is None else sid
    if engine == "codex":
        if sid:
            key = state(chat_id, "workspace-codex-" + str(uuid.uuid5(uuid.NAMESPACE_URL, sid)))
        else:
            key = state(chat_id, "pending-workspace-codex")
            if not key:
                key = uuid.uuid4().hex
                set_state(chat_id, "pending-workspace-codex", key)
    else:
        key = ""
    key = key or str(uuid.uuid5(uuid.NAMESPACE_URL, engine + ":" + sid))
    # Файл сопоставления является состоянием бота, но путь всё равно проверяем.
    if any(c not in "0123456789abcdef-" for c in key):
        raise ValueError("Повреждён путь рабочей папки сессии")
    return session_files.prepare(os.path.join(data_dir(chat_id), "workspaces", engine, key),
                                 memory_file(chat_id))


def bind_codex_workspace(chat_id, sid, cwd):
    if is_topic(chat_id):
        return
    set_state(chat_id, "workspace-codex-" + str(uuid.uuid5(uuid.NAMESPACE_URL, sid)),
              os.path.basename(cwd))


def model_env(**extra):
    # Секрет Telegram не нужен ни CLI, ни запускаемым им инструментам.
    return {**{k: v for k, v in os.environ.items() if not k.startswith("TG_")},
            "PATH": os.path.expanduser("~/.local/bin") + os.pathsep + os.environ.get("PATH", os.defpath),
            **extra}


def file_prompt(cwd, text):
    return (text + f"\n\n[Рабочая папка сессии: {cwd}. Перед ответом обязательно проверь "
            "files/from_user и MEMORY.md. Готовые файлы сохраняй в files/for_user; "
            "после ответа вложения будут перенесены ботом в files/general.]")


def model_allowed(user_id, slug):
    return user_id in ADMINS or slug not in ADMIN_ONLY_MODELS


def codex_catalog(user_id=None):
    """Видимые модели из каталога самого codex в порядке приоритета."""
    if not CODEX_BIN:
        return []
    try:
        with open(MODELS_CACHE, encoding="utf-8") as f:
            models = json.load(f)["models"]
    except (OSError, ValueError, KeyError):
        return []
    listed = [m for m in models if m.get("visibility") == "list"]
    listed.sort(key=lambda m: m.get("priority", 999))
    if user_id is not None:
        listed = [m for m in listed if model_allowed(user_id, m.get("slug"))]
    return listed


def codex_models():
    """(slug, название, уровни, уровень по умолчанию) из каталога codex."""
    listed = codex_catalog()
    return [(m["slug"], m.get("display_name") or m["slug"],
             [lvl["effort"] for lvl in m.get("supported_reasoning_levels", [])],
             m.get("default_reasoning_level", "")) for m in listed]


def codex_efforts(slug):
    for known, _, efforts, _ in codex_models():
        if known == slug:
            return efforts
    return []


def session_file(chat_id, sid):
    """Имя папки внутри projects/ claude собирает из пути, поэтому ищем глобом,
    а не воспроизводим его правила."""
    found = glob.glob(os.path.join(chat_dir(chat_id), "projects", "*", sid + ".jsonl"))
    return found[0] if found else None


def read_session(path):
    """(реплики пользователя, ответы Claude, заголовок, размер)."""
    counts = {"user": 0, "assistant": 0}
    title = first_prompt = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # оборванная строка в конце файла
            kind = rec.get("type")
            if kind in counts:
                counts[kind] += 1
            elif kind == "ai-title" and rec.get("aiTitle"):
                title = rec["aiTitle"]  # заголовок от Claude Code
            elif kind == "last-prompt" and not first_prompt:
                first_prompt = (rec.get("lastPrompt") or "").replace("\n", " ")
    return counts["user"], counts["assistant"], title or first_prompt, os.path.getsize(path)


def codex_index(chat_id, sid):
    """Свой индекс сессии codex: у него транскрипт лежит внутри его же базы."""
    return os.path.join(chat_dir(chat_id), "codex", sid + ".json")


def bump_codex_index(chat_id, sid, question):
    """После каждого ответа: заголовок из первого вопроса и счётчик реплик."""
    path = codex_index(chat_id, sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data["title"] = data.get("title") or question.replace("\n", " ")[:80]
    data["mine"] = data.get("mine", 0) + 1
    data["theirs"] = data.get("theirs", 0) + 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def read_codex_index(path):
    """(реплики пользователя, ответы codex, заголовок) — как read_session, но из индекса."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0, 0, ""
    return data.get("mine", 0), data.get("theirs", 0), data.get("title", "")


def session_stats(chat_id):
    """(сообщений от пользователя, ответов движка, размер) активной сессии."""
    sid = active_session(chat_id)
    if get_engine(chat_id) == "codex":
        if not sid:
            return 0, 0, 0
        mine, theirs, _ = read_codex_index(codex_index(chat_id, sid))
        return mine, theirs, 0  # размер знает сам codex, у нас только индекс
    path = session_file(chat_id, sid)
    if not path:
        return 0, 0, 0
    mine, claude, _, size = read_session(path)
    return mine, claude, size


def session_list(chat_id):
    """(движок, id, заголовок, сообщений, время) обоих движков, свежие сверху.

    Активная сессия текущего движка всегда в списке, даже если ещё пустая;
    у codex до первого ответа id нет, поэтому она обозначается как "new".
    """
    items = []
    for path in glob.glob(os.path.join(chat_dir(chat_id), "projects", "*", "*.jsonl")):
        mine, claude, title, _ = read_session(path)
        items.append(("claude", os.path.basename(path)[:-len(".jsonl")],
                      title, mine + claude, os.path.getmtime(path)))
    for path in glob.glob(os.path.join(chat_dir(chat_id), "codex", "*.json")):
        mine, theirs, title = read_codex_index(path)
        items.append(("codex", os.path.basename(path)[:-len(".json")],
                      title, mine + theirs, os.path.getmtime(path)))
    items.sort(key=lambda i: i[4], reverse=True)
    engine = get_engine(chat_id)
    active = active_session(chat_id) or "new"
    if not any(e == engine and sid == active for e, sid, _, _, _ in items):
        items.insert(0, (engine, active, "", 0, 0.0))
    return items


def known_session(chat_id, engine, sid):
    """Список мог приехать из старого сообщения — сессии там уже может не быть."""
    return any(e == engine and known == sid for e, known, _, _, _ in session_list(chat_id))


def session_title(chat_id, engine, sid):
    for e, known, title, msgs, _ in session_list(chat_id):
        if e == engine and known == sid:
            return title or ("без названия" if msgs else "новая, пока пустая")
    return "без названия"


def delete_session(chat_id, engine, sid):
    """Стираем транскрипт: у claude это файл, у codex — его база плюс наш индекс.

    Удалили активную — переезжаем на самую свежую сессию того же движка, иначе
    следующее сообщение воскресило бы её же id пустой сессией.
    """
    workspace = workspace_dir(chat_id, engine, "" if sid == "new" else sid)
    shutil.rmtree(workspace)
    if engine == "claude":
        path = session_file(chat_id, sid)
        if path:
            os.remove(path)
    else:
        if os.path.exists(codex_index(chat_id, sid)):
            os.remove(codex_index(chat_id, sid))
        forget_codex_session(chat_id, sid)
    if engine == get_engine(chat_id) and sid == (active_session(chat_id) or "new"):
        rest = [s for e, s, _, n, _ in session_list(chat_id) if e == engine and s != sid and n]
        set_active_session(chat_id, rest[0] if rest else fresh_session(engine), engine)


def fresh_session(engine):
    """Новая сессия: у claude id придумываем мы, у codex он появится с ответом."""
    return "" if engine == "codex" else str(uuid.uuid4())


def forget_codex_session(chat_id, sid):
    """codex delete работает локально по своей базе; нет его — нет и проблемы."""
    if not CODEX_BIN:
        return
    env = {**os.environ, "CODEX_HOME": os.path.join(chat_dir(chat_id), "codex-home")}
    try:
        subprocess.run([CODEX_BIN, "delete", sid], env=env, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        log.warning("codex delete %s не удался", sid)


def human_size(n):
    for unit in ("Б", "КБ", "МБ"):
        if n < 1024 or unit == "МБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


def sessions_page(chat_id, page, prefix="sess"):
    """Текст и клавиатура одной страницы списка сессий: prefix — что делает кнопка."""
    items = session_list(chat_id)
    pages = max(1, -(-len(items) // PAGE))
    page = max(0, min(page, pages - 1))
    current = get_engine(chat_id)
    active = active_session(chat_id) or "new"

    lines, numbers = [], []
    for offset, (engine, sid, title, msgs, _) in enumerate(items[page * PAGE:(page + 1) * PAGE]):
        number = page * PAGE + offset + 1
        about = f"{title or 'без названия'} — {msgs} сообщ." if msgs else "новая, пока пустая"
        mark = "✓ " if engine == current and sid == active else ""
        lines.append(f"{number}. {mark}{MARKS[engine]} {about}")
        numbers.append(InlineKeyboardButton(
            text=str(number), callback_data=f"{prefix}:{engine}:{sid}"))

    rows = [numbers]
    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="◀", callback_data=f"{prefix}page:{page - 1}"),
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"{prefix}page:same"),
            InlineKeyboardButton(text="▶", callback_data=f"{prefix}page:{page + 1}"),
        ])
    text = LISTS[prefix] + "\n" + "\n".join(lines)
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def model_kb(chat_id, user_id=None):
    """Модели обоих движков одним списком: выбор модели заодно выбирает движок."""
    user_id = chat_id if user_id is None else user_id
    engine, current = get_engine(chat_id), get_model(chat_id)
    fixed = topic_store().get(chat_id).get("engine") if is_topic(chat_id) else None
    rows = []
    for name in (MODELS if fixed in (None, "claude") else []):
        mark = "✓ " if engine == "claude" and name == current and (not is_topic(chat_id) or fixed) else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{MARKS['claude']} {name}",
                                          callback_data=f"model:claude:{name}")])
    for model in (codex_catalog(user_id) if fixed in (None, "codex") else []):
        slug, title = model["slug"], model.get("display_name") or model["slug"]
        mark = "✓ " if engine == "codex" and slug == current and (not is_topic(chat_id) or fixed) else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{MARKS['codex']} {title}",
                                          callback_data=f"model:codex:{slug}")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="model:cancel:")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def model_text(chat_id, user_id=None):
    """Текущий выбор и краткая подсказка, для чего подходит каждая модель."""
    user_id = chat_id if user_id is None else user_id
    fixed = topic_store().get(chat_id).get("engine") if is_topic(chat_id) else None
    if is_topic(chat_id) and not fixed:
        return "Выберите модель для темы. Движок Claude или Codex закрепится за темой; модель внутри него можно менять. После выбора отправьте свой вопрос."
    lines = [f"Модель: {about_model(chat_id)}", "", "Для чего использовать:"]
    for name in (MODELS if fixed in (None, "claude") else []):
        lines.append(f"{MARKS['claude']} {name} — {CLAUDE_MODEL_USES[name]}")
    for model in (codex_catalog(user_id) if fixed in (None, "codex") else []):
        title = model.get("display_name") or model["slug"]
        description = model.get("description") or "для задач программирования в Codex"
        description = CODEX_DESCRIPTION_TRANSLATIONS.get(description, description)
        lines.append(f"{MARKS['codex']} {title} — {description.rstrip('.')}")
    return "\n".join(lines)


def efforts(chat_id):
    """Уровни размышления текущего движка: у codex свой набор на каждую модель."""
    if get_engine(chat_id) == "claude":
        return CLAUDE_EFFORTS
    return codex_efforts(get_model(chat_id, "codex"))


def effort_kb(chat_id):
    current = get_effort(chat_id)
    rows = [[InlineKeyboardButton(text=("✓ " if level == current else "") + level,
                                  callback_data=f"effort:{level}")]
            for level in efforts(chat_id)]
    rows.append([InlineKeyboardButton(text=("✓ " if not current else "") + "по умолчанию",
                                      callback_data="effort:")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def about_model(chat_id):
    engine = get_engine(chat_id)
    level = get_effort(chat_id) or "по умолчанию"
    return f"{MARKS[engine]} {get_model(chat_id)} · размышление: {level}"


def transcribe(data):
    """Расшифровка голосового. Локально, офлайн, на CPU — без чужих API.

    Вызывается только из потока: и загрузка модели, и распознавание
    блокирующие, в event loop им нельзя.
    """
    global stt
    if stt is None:
        from faster_whisper import WhisperModel  # ~1 ГБ памяти, грузим по факту
        log.info("загружаю модель распознавания %s", STT_MODEL)
        stt = WhisperModel(STT_MODEL, device="cpu", compute_type="int8")
    segments, _ = stt.transcribe(io.BytesIO(data), language=STT_LANG,
                                 vad_filter=True)  # vad отсекает тишину и шум
    return " ".join(s.text.strip() for s in segments).strip()


def image_source(message):
    """(файл, mime) для фото или картинки-документа."""
    if message.photo:
        # photo — список превью; последнее самое крупное. Telegram жмёт их в JPEG
        return message.photo[-1], "image/jpeg"
    return message.document, message.document.mime_type


def fit_images(images, limit=MAX_ALBUM):
    """Обрезаем альбом по суммарному объёму: base64 раздувает payload в треть."""
    kept, total = [], 0
    for data, mime in images:
        if total + len(data) > limit:
            break
        kept.append((data, mime))
        total += len(data)
    return kept


def build_input(text, images):
    """Одна строка stream-json: текст и/или картинки как контент-блоки."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for data, mime in images:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": mime,
            "data": base64.b64encode(data).decode()}})
    return json.dumps({"type": "user",
                       "message": {"role": "user", "content": content}}) + "\n"


def parse_result(out):
    """В stream-json ответ — последняя строка с type=result."""
    for line in reversed(out.decode(errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "result":
            return rec
    return None


async def ask_claude(chat_id, text, images=()):
    sid = active_session(chat_id, "claude")
    cwd = workspace_dir(chat_id, "claude")
    data = data_dir(chat_id)
    env = model_env(CLAUDE_CONFIG_DIR=chat_dir(chat_id))
    effort = get_effort(chat_id, "claude")
    # stream-json сохраняет прямую передачу изображений вместе с файловыми инструментами.
    base = [
        CLAUDE_BIN, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose",
        "--safe-mode",  # без CLAUDE.md, хуков, скиллов, MCP и плагинов
        "--restricted", "--tools", FILE_TOOLS,
        # Bash в песочнице: без него модель не распакует docx и другие архивы.
        "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep,NotebookEdit,WebFetch,WebSearch",
        "--permission-mode", "dontAsk",
        "--add-dir", data,  # файловым инструментам — та же папка, что и Bash
        "--settings", json.dumps({
            "sandbox": {
                "enabled": True, "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                # Закрыто всё хозяйство бота (.env с TG_TOKEN, токены подписок,
                # чужие чаты), открыта только папка данных этого чата или группы.
                "filesystem": {"denyRead": [os.path.expanduser("~"), HERE, SESSIONS],
                               "allowRead": [data], "allowWrite": [data]},
                # Без allowedDomains прокси песочницы спрашивал бы разрешение на
                # каждый хост, а в dontAsk это отказ: интернета у Bash нет.
                "network": {"allowedDomains": ["*"]},
            },
            # Тем же правилом открываем хосты для WebFetch.
            "permissions": {"allow": ["WebFetch(domain:*)"]},
        }),
        "--system-prompt-snapshot", "off",
        "--model", get_model(chat_id, "claude"),
        "--system-prompt", SYSTEM_PROMPT + "\n" + session_files.instructions(cwd, memory_file(chat_id)),
    ] + (["--effort", effort] if effort else [])
    payload = build_input(file_prompt(cwd, text), images).encode()
    creds = os.path.join(chat_dir(chat_id), ".credentials.json")
    for retry in (True, False):
        # Пока длится запрос, токен может истечь — запас на весь TIMEOUT.
        async with creds_lock if creds_expiring(margin=TIMEOUT + 300) else nullcontext():
            try:
                return await run_claude(base, sid, payload, cwd, env)
            except RuntimeError as e:
                # Токен обновил параллельный запрос: берём его и пробуем ещё раз.
                if not retry or not any(s in str(e).lower() for s in AUTH_ERRORS):
                    raise
                log.warning("claude auth failed, retrying with host token: %s", e)
            finally:
                # Обновлённый токен сразу отдаём хосту и остальным чатам.
                link_creds(creds, HOST_CREDS)


AUTH_ERRORS = ("failed to authenticate", "oauth", "authentication_error", "invalid api key",
               "please run /login")


async def run_claude(base, sid, payload, cwd, env):
    err = ""
    # Сначала пробуем продолжить сессию, при неудаче создаём её с тем же id.
    for tail in (["--resume", sid], ["--session-id", sid]):
        proc = await asyncio.create_subprocess_exec(
            *base, *tail,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        try:
            out, raw_err = await asyncio.wait_for(
                proc.communicate(payload), timeout=TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            raise
        data = parse_result(out)
        if data and not data.get("is_error") and proc.returncode == 0:
            return data.get("result", "")
        err = ((data.get("result") or "\n".join(data.get("errors", []))) if data else "")
        err = err or raw_err.decode().strip() or "claude завершился с ошибкой"
        missing_session = any(s in err.lower() for s in (
            "no conversation found", "no session found", "session not found"))
        if tail[0] != "--resume" or not missing_session:
            raise RuntimeError(err)
    raise RuntimeError(err or "claude завершился с ошибкой")


def parse_codex(out):
    """(id треда, ответ, ошибка) из JSONL-событий codex."""
    sid = text = err = ""
    for line in out.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # человекочитаемые строки вперемешку с событиями
        kind = rec.get("type")
        if kind == "thread.started":
            sid = rec.get("thread_id", "")
        elif kind == "item.completed" and rec.get("item", {}).get("type") == "agent_message":
            text = rec["item"].get("text", "")
        elif kind in ("turn.failed", "error"):
            err = ((rec.get("error") or {}).get("message")
                   or rec.get("message") or "codex вернул ошибку")
    return sid, text, err


def codex_args(chat_id, sid, effort, answer_file, files):
    """Переопределяем старый read-only конфиг и инструкции при каждом запуске."""
    cwd = workspace_dir(chat_id, "codex")
    # Родительский exec принимает -C и при resume: важно для старых бесед,
    # которые раньше запускались прямо в служебной папке чата.
    args = [CODEX_BIN, "exec", "-C", cwd] + (["resume", sid] if sid else []) + [
        "--json", "--skip-git-repo-check",
        "-m", get_model(chat_id, "codex"), "-o", answer_file,
    ]
    if effort:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    # Профиль прав сильнее sandbox_mode: читать и писать можно только папку данных
    # чата, из остального — системные каталоги на чтение. Сеть профиль не даёт.
    for setting in ('default_permissions="chat"', 'approval_policy="never"',
                    'permissions.chat.filesystem={":minimal" = "read", %s = "write"}'
                    % json.dumps(data_dir(chat_id)),
                    'developer_instructions=' + json.dumps(
                        SYSTEM_PROMPT + "\n" + session_files.instructions(cwd, memory_file(chat_id)))):
        args += ["-c", setting]
    for path in files:
        args += ["-i", path]
    return args + ["-"]  # промпт со stdin: длинный текст в аргумент не влезет


async def ask_codex(chat_id, text, images=()):
    """У codex своё хозяйство: id сессии выдаёт он сам, картинки принимает файлами."""
    if not CODEX_BIN:
        raise RuntimeError("codex не установлен")
    cwd = workspace_dir(chat_id, "codex")
    tmp = os.path.join(chat_dir(chat_id), "codex-home", "tmp")
    os.makedirs(tmp, exist_ok=True)
    sid = active_session(chat_id, "codex")
    effort = get_effort(chat_id, "codex")
    answer_file = os.path.join(tmp, uuid.uuid4().hex + ".txt")
    files = []
    for data, mime in images:
        path = os.path.join(tmp, uuid.uuid4().hex + "." + (mime.split("/")[-1] or "jpg"))
        with open(path, "wb") as f:
            f.write(data)
        files.append(path)
    args = codex_args(chat_id, sid, effort, answer_file, files)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=model_env(CODEX_HOME=os.path.join(chat_dir(chat_id), "codex-home")),
        start_new_session=True,
    )
    try:
        out, raw_err = await asyncio.wait_for(
            proc.communicate(file_prompt(cwd, text).encode()), timeout=CODEX_TIMEOUT)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    finally:
        for path in files:
            os.remove(path)
    started, message, err = parse_codex(out)
    if started:
        # И при ошибке сохраняем привязку уже созданного треда и его вложения.
        bind_codex_workspace(chat_id, started, cwd)
        set_active_session(chat_id, started, "codex")
    try:
        with open(answer_file, encoding="utf-8") as f:
            message = f.read().strip() or message
        os.remove(answer_file)
    except OSError:
        pass  # ответа в файле нет — берём его из событий
    if proc.returncode != 0 or not message:
        raise RuntimeError(err or raw_err.decode().strip() or "codex завершился с ошибкой")
    if started:
        bump_codex_index(chat_id, started, text or "картинка")
    return message


async def ask(chat_id, text, images=()):
    if get_engine(chat_id) == "codex":
        return await ask_codex(chat_id, text, images)
    return await ask_claude(chat_id, text, images)


async def topic_setup(message, user_id=None, force=False):
    key = scope(message)
    record = topic_store().ensure(key)
    if record.get("deleted"):
        await message.answer("Сессия этой темы удалена. Для новой беседы: /new")
    elif record.get("closed"):
        return
    elif not record.get("engine") and (force or not record.get("prompt_message_id")):
        sent = await message.answer(model_text(key, user_id), reply_markup=model_kb(key, user_id))
        topic_store().update(key, prompt_message_id=sent.message_id)


def topic_page(group_id, page=0):
    entries = topic_store().list(group_id)
    pages = max(1, (len(entries) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for thread, record in entries[page * PAGE:(page + 1) * PAGE]:
        engine = record.get("engine")
        label = record["name"][:45]
        label += f" · {MARKS[engine]} {get_model((group_id, thread), engine)}" if engine else " · выбрать модель"
        if record.get("closed"):
            label = "🔒 " + label
        rows.append([InlineKeyboardButton(text=label, url=topics.topic_link(group_id, thread))])
    if pages > 1:
        rows.append([InlineKeyboardButton(text="◀", callback_data=f"topics:{page - 1}"),
                     InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"topics:{page}"),
                     InlineKeyboardButton(text="▶", callback_data=f"topics:{page + 1}")])
    text = "Темы с сессиями — нажмите для перехода:" if entries else "Пока нет тем с сессиями. Создайте тему или отправьте /new Название."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.forum_topic_created | F.forum_topic_edited | F.forum_topic_closed |
            F.forum_topic_reopened | F.general_forum_topic_hidden | F.general_forum_topic_unhidden)
@serialized_chat
async def on_topic_event(message: Message):
    key = scope(message)
    if not is_topic(key):
        return
    record = topic_store().ensure(key)
    if message.forum_topic_created:
        changes = {"name": message.forum_topic_created.name}
        if not record.get("owner_id") and message.from_user and not message.from_user.is_bot:
            changes["owner_id"] = message.from_user.id
        topic_store().update(key, **changes)
        if not message.from_user or message.from_user.id != message.bot.id:
            await topic_setup(message, message.from_user.id if message.from_user else None)
    elif message.forum_topic_edited:
        if message.forum_topic_edited.name is not None:
            topic_store().update(key, name=message.forum_topic_edited.name)
    elif message.forum_topic_closed or message.general_forum_topic_hidden:
        topic_store().update(key, closed=True)
    elif message.forum_topic_reopened or message.general_forum_topic_unhidden:
        topic_store().update(key, closed=False)


@dp.callback_query(F.data.startswith("topics:"), F.from_user.id.in_(ALLOWED))
async def on_topic_page(call: CallbackQuery):
    if not is_topic(scope(call.message)):
        return
    text, kb = topic_page(call.message.chat.id, int(call.data.split(":")[1]))
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await call.answer()


@dp.message(Command("start"), F.from_user.id.in_(ALLOWED))
async def on_start(message: Message):
    if is_topic(scope(message)):
        await message.answer("Группа подключена. Одна тема — одна сессия Claude или Codex.\n"
                             "/new Название — новая тема\n/model — модель этой темы\n"
                             "/effort — размышление\n/info — текущая сессия\n"
                             "/sessions — перейти в другую тему\n/delete — удалить сессию этой темы")
        async with locks[scope(message)]:
            await topic_setup(message, message.from_user.id)
        return
    await message.answer(
        "Привет! Это Claude и Codex в Telegram.\n\n"
        "Пиши текстом, присылай фото, видео, документы и голосовые — файлы и контекст "
        "сохраняются, можно продолжать с того же места хоть завтра.\n\n"
        "/info — текущая модель и сессия\n"
        "/model — модель и движок: Claude или Codex\n"
        "/effort — уровень размышления\n"
        "/new — начать новую сессию с чистого листа\n"
        "/sessions — вернуться к прошлой сессии\n"
        "/delete — удалить ненужную сессию"
    )


@dp.message(Command("info"), F.from_user.id.in_(ALLOWED))
async def on_info(message: Message):
    chat_id = scope(message)
    if is_topic(chat_id):
        record = topic_store().ensure(chat_id)
        if record.get("deleted") or not record.get("engine"):
            await topic_setup(message, message.from_user.id, force=True)
            return
    mine, theirs, size = session_stats(chat_id)
    await message.answer(
        f"Модель: {about_model(chat_id)}\n"
        f"Сессия: {topic_store().get(chat_id)['name'] if is_topic(chat_id) else session_title(chat_id, get_engine(chat_id), active_session(chat_id) or 'new')}\n"
        f"Сообщений: {mine + theirs} (вы {mine}, модель {theirs})\n"
        # размер знает только claude: у codex транскрипт лежит внутри него самого
        + (f"Размер сессии: {human_size(size)}\n" if size else "") +
        f"Сменить модель: /model · размышление: /effort\n"
        f"Сессия: /sessions · начать заново: /new · удалить: /delete"
    )


@dp.message(Command("new"), F.from_user.id.in_(ALLOWED))
@serialized_chat
async def on_new(message: Message, bot: Bot = None):
    if is_topic(scope(message)):
        parts = (message.text or "").split(maxsplit=1)
        name = parts[1].strip()[:128] if len(parts) > 1 else "Новая беседа"
        try:
            created = await bot.create_forum_topic(message.chat.id, name)
        except (TelegramBadRequest, TelegramForbiddenError):
            await message.answer("Не удалось создать тему. Дайте боту право «Управление темами» "
                                 "или создайте тему вручную в Telegram.")
            return
        key = message.chat.id, created.message_thread_id
        async with locks[key]:
            topic_store().ensure(key, name=created.name, owner_id=message.from_user.id)
            topic_store().update(key, name=created.name, owner_id=message.from_user.id)
            sent = await bot.send_message(message.chat.id, model_text(key, message.from_user.id),
                                          message_thread_id=key[1], reply_markup=model_kb(key, message.from_user.id))
            topic_store().update(key, prompt_message_id=sent.message_id)
        await message.answer("Тема создана.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=created.name, url=topics.topic_link(*key))]]))
        return
    engine = get_engine(scope(message))
    set_active_session(scope(message), fresh_session(engine), engine)
    await message.answer("Новая сессия. Контекст чистый, прошлые сессии целы — /sessions")


@dp.message(Command("sessions"), F.from_user.id.in_(ALLOWED))
async def on_sessions(message: Message):
    if is_topic(scope(message)):
        text, kb = topic_page(message.chat.id)
        await message.answer(text, reply_markup=kb)
        return
    text, kb = sessions_page(scope(message), 0)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.regexp(r"^(sess|del)page:"), F.from_user.id.in_(ALLOWED))
@private_sessions_only
async def on_sessions_page(call: CallbackQuery):
    prefix, page = call.data.split("page:", 1)
    if page == "same":  # кнопка-счётчик страниц, нажимать нечего
        await call.answer()
        return
    text, kb = sessions_page(scope(call.message), int(page), prefix)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("sess:"), F.from_user.id.in_(ALLOWED))
@serialized_chat
@private_sessions_only
async def on_session_pick(call: CallbackQuery):
    chat_id = scope(call.message)
    engine, sid = call.data.split(":", 2)[1:]
    if engine == get_engine(chat_id) and sid == (active_session(chat_id) or "new"):
        await call.answer("Уже выбрана")
        return
    if not known_session(chat_id, engine, sid):
        await call.answer("Сессии больше нет")  # список из старого сообщения
        return
    set_state(chat_id, "engine", engine)  # сессия чужого движка заодно его и включает
    set_active_session(chat_id, "" if sid == "new" else sid, engine)
    text, kb = sessions_page(chat_id, 0)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer(f"Переключил на {MARKS[engine]} {get_model(chat_id)}")


@dp.message(Command("delete"), F.from_user.id.in_(ALLOWED))
async def on_delete(message: Message):
    key = scope(message)
    if is_topic(key):
        if not topic_manager(key, message.from_user.id):
            await message.answer("Удалить сессию может создатель темы или администратор бота.")
            return
        record = topic_store().get(key)
        if not record.get("engine") or record.get("deleted"):
            await message.answer("В этой теме нет активной сессии.")
            return
        sid = active_session(key) or "new"
        await message.answer("Удалить историю ИИ и файлы этой темы? Восстановить их будет нельзя.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                 InlineKeyboardButton(text="Удалить", callback_data=f"topicdelete:{sid}"),
                                 InlineKeyboardButton(text="Отмена", callback_data="topiccancel")]]))
        return
    text, kb = sessions_page(scope(message), 0, "del")
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("topicdelete:"), F.from_user.id.in_(ALLOWED))
@serialized_chat
@topic_settings
async def on_topic_delete(call: CallbackQuery):
    key = scope(call.message)
    if not is_topic(key):
        return
    record = topic_store().get(key)
    if record.get("deleted") or call.data.split(":", 1)[1] != (active_session(key) or "new"):
        await call.answer("Сессия уже изменилась")
        return
    delete_session(key, get_engine(key), active_session(key) or "new")
    topic_store().update(key, deleted=True)
    await call.message.edit_text("История ИИ и файлы удалены. Для новой беседы: /new")
    await call.answer("Удалено")


@dp.callback_query(F.data == "topiccancel", F.from_user.id.in_(ALLOWED))
@topic_settings
async def on_topic_cancel(call: CallbackQuery):
    await call.message.edit_text("Удаление отменено.")
    await call.answer()


@dp.callback_query(F.data.startswith("del:"), F.from_user.id.in_(ALLOWED))
@private_sessions_only
async def on_delete_pick(call: CallbackQuery):
    chat_id = scope(call.message)
    engine, sid = call.data.split(":", 2)[1:]
    if not known_session(chat_id, engine, sid):
        await call.answer("Сессии больше нет")  # список из старого сообщения
        return
    await call.message.edit_text(
        f"Удалить сессию {MARKS[engine]} «{session_title(chat_id, engine, sid)}»? "
        f"История и файлы сессии будут удалены. Восстановить будет нельзя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да", callback_data=f"delyes:{engine}:{sid}"),
            InlineKeyboardButton(text="Нет", callback_data="delno"),
        ]]))
    await call.answer()


@dp.callback_query(F.data.startswith("delyes:"), F.from_user.id.in_(ALLOWED))
@serialized_chat
@private_sessions_only
async def on_delete_confirm(call: CallbackQuery):
    chat_id = scope(call.message)
    engine, sid = call.data.split(":", 2)[1:]
    delete_session(chat_id, engine, sid)
    text, kb = sessions_page(chat_id, 0, "del")
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer("Удалил")


@dp.callback_query(F.data == "delno", F.from_user.id.in_(ALLOWED))
@private_sessions_only
async def on_delete_cancel(call: CallbackQuery):
    text, kb = sessions_page(scope(call.message), 0, "del")
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer("Отменил")


@dp.message(Command("model"), F.from_user.id.in_(ALLOWED))
@topic_settings
async def on_model(message: Message):
    if is_topic(scope(message)) and topic_store().get(scope(message)).get("deleted"):
        await topic_setup(message, message.from_user.id)
        return
    await message.answer(model_text(scope(message), message.from_user.id),
                         reply_markup=model_kb(scope(message), message.from_user.id))


@dp.callback_query(F.data.startswith("model:"), F.from_user.id.in_(ALLOWED))
@serialized_chat
@topic_settings
async def on_model_pick(call: CallbackQuery):
    chat_id = scope(call.message)
    engine, choice = call.data.split(":", 2)[1:]
    record = topic_store().ensure(chat_id) if is_topic(chat_id) else {}
    if record.get("deleted"):
        await call.answer("Сессия удалена. Создайте новую тему: /new", show_alert=True)
        return
    if engine == "cancel":
        await call.message.edit_text("Выбор модели: /model" if is_topic(chat_id) and not record.get("engine")
                                     else f"Модель осталась: {about_model(chat_id)}")
        await call.answer()
        return
    if record.get("engine") and engine != record["engine"]:
        await call.answer("За темой закреплён движок. Для другого движка создайте тему: /new", show_alert=True)
        return
    known = (MODELS if engine == "claude" else
             [model["slug"] for model in codex_catalog(call.from_user.id)])
    if choice not in known:
        await call.answer("Неизвестная модель")
        return
    if (not is_topic(chat_id) or record.get("engine")) and engine == get_engine(chat_id) and choice == get_model(chat_id):
        # повторное нажатие: правка на тот же текст — это ошибка Telegram
        await call.answer("Уже выбрана")
        return
    was = get_engine(chat_id)
    set_state(chat_id, "engine", engine)
    set_model(chat_id, choice, engine)
    if is_topic(chat_id) and not record.get("engine"):
        set_active_session(chat_id, fresh_session(engine), engine)
        topic_store().update(chat_id, engine=engine, owner_id=record.get("owner_id") or call.from_user.id)
    # уровень, которого у новой модели нет, get_effort сам сочтёт «по умолчанию»
    await call.message.edit_text(model_text(chat_id, call.from_user.id),
                                 reply_markup=model_kb(chat_id, call.from_user.id))
    if engine != was and not is_topic(chat_id):
        # сессии у движков разные: начинаем с чистой, прежние — тут же списком
        set_active_session(chat_id, fresh_session(engine), engine)
        text, kb = sessions_page(chat_id, 0)
        await call.message.answer(
            "Сменил движок, сессия новая — пишите прямо в неё или выберите "
            "другую.\n\n" + text, reply_markup=kb)
    await call.answer(f"Теперь {choice}")


@dp.message(Command("effort"), F.from_user.id.in_(ALLOWED))
@topic_settings
async def on_effort(message: Message):
    chat_id = scope(message)
    if is_topic(chat_id) and (not topic_store().get(chat_id).get("engine") or topic_store().get(chat_id).get("deleted")):
        await topic_setup(message, message.from_user.id, force=True)
        return
    if not efforts(chat_id):
        await message.answer("У этой модели уровень размышления не выбирается.")
        return
    await message.answer(f"Размышление: {get_effort(chat_id) or 'по умолчанию'}",
                         reply_markup=effort_kb(chat_id))


@dp.callback_query(F.data.startswith("effort:"), F.from_user.id.in_(ALLOWED))
@serialized_chat
@topic_settings
async def on_effort_pick(call: CallbackQuery):
    chat_id = scope(call.message)
    if is_topic(chat_id) and (not topic_store().get(chat_id).get("engine") or topic_store().get(chat_id).get("deleted")):
        await call.answer("Сначала выберите модель в активной теме: /model")
        return
    level = call.data.split(":", 1)[1]
    if level and level not in efforts(chat_id):
        await call.answer("Этот уровень модели не подходит")
        return
    if level == get_effort(chat_id):
        await call.answer("Уже выбран")
        return
    set_effort(chat_id, level, get_engine(chat_id))
    await call.message.edit_text(f"Размышление: {level or 'по умолчанию'}",
                                 reply_markup=effort_kb(chat_id))
    await call.answer(f"Теперь {level or 'по умолчанию'}")


async def download_attachment(message, bot, cwd):
    source, name, mime = session_files.attachment(message)
    if (source.file_size or 0) > MAX_DOWNLOAD:
        raise ValueError(f"Файл {name} больше {human_size(MAX_DOWNLOAD)} — "
                         "Telegram Bot API не позволяет его скачать.")
    inbox = session_files.directory(Path(cwd) / "files" / "from_user")
    destination = session_files.vacant(inbox / name)
    # Недокачанный файл не должен попасть к модели, включая перезапуск сервиса.
    staging = session_files.directory(Path(chat_dir(scope(message))) / "downloads")
    partial = staging / (uuid.uuid4().hex + ".part")
    try:
        await bot.download(source, destination=partial)
        if partial.stat().st_size > MAX_DOWNLOAD:
            raise ValueError(f"Файл {name} больше {human_size(MAX_DOWNLOAD)}.")
        partial.rename(destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination, mime


async def transcribe_attachment(message, bot):
    """Аудио существует только в памяти и не попадает в файловую очередь."""
    source = getattr(message, "voice", None) or message.audio
    if (source.file_size or 0) > MAX_DOWNLOAD:
        raise ValueError(f"Аудио больше {human_size(MAX_DOWNLOAD)} — его не скачать.")
    with io.BytesIO() as buffer:
        await bot.download(source, destination=buffer)
        if buffer.getbuffer().nbytes > MAX_DOWNLOAD:
            raise ValueError(f"Аудио больше {human_size(MAX_DOWNLOAD)}.")
        async with stt_lock:
            return (await asyncio.to_thread(transcribe, buffer.getvalue())).strip()


async def send_generated_files(message, cwd):
    for path in session_files.queued(cwd, "for_user"):
        try:
            if path.stat().st_size > MAX_UPLOAD:
                raise ValueError(f"размер больше {human_size(MAX_UPLOAD)}")
            if path.stat().st_size == 0:
                raise ValueError("Telegram не принимает пустые файлы")
            await message.answer_document(FSInputFile(path), disable_content_type_detection=True)
        except Exception as e:
            log.exception("file delivery failed: %s", path.name)
            await message.answer(f"Не удалось отправить {path.name}: {e}. "
                                 "Файл сохранён, повторю отправку после следующего ответа.")
            continue
        session_files.archive(cwd, "for_user", [path])


async def respond(message: Message, bot: Bot, text, images=(), attachments=(), locked=False):
    chat_id = scope(message)
    if not locked:
        async with locks[chat_id]:
            return await respond(message, bot, text, images, attachments, locked=True)
    record = topic_store().ensure(chat_id) if is_topic(chat_id) else {}
    if record.get("deleted"):
        await topic_setup(message, message.from_user.id)
        return
    if record.get("closed"):
        return
    ready = not is_topic(chat_id) or bool(record.get("engine"))
    has_prompt = bool((text or "").strip())
    has_context = has_prompt or any(
        getattr(part, "voice", None) or getattr(part, "audio", None)
        for part in attachments)
    if (ready and has_context and get_engine(chat_id) == "codex"
            and not model_allowed(message.from_user.id, get_model(chat_id))):
        await message.answer("GPT Astra доступна только администраторам. Выбери другую модель: /model")
        return
    try:
        cwd = workspace_dir(chat_id)
        images = list(images)
        notes = []
        activity = ChatActionSender.typing(bot=bot, chat_id=message.chat.id,
                                          message_thread_id=topics.thread_id(message)) if has_context and ready else nullcontext()
        async with activity:
            for part in attachments:
                if getattr(part, "voice", None) or getattr(part, "audio", None):
                    try:
                        transcript = await transcribe_attachment(part, bot)
                    except Exception as e:
                        log.exception("stt failed")
                        await message.answer(f"Не смог расшифровать аудио: {e}")
                        continue
                    if not transcript:
                        await message.answer("Тишина или не разобрал — попробуй ещё раз.")
                        continue
                    has_prompt = True
                    notes.append("Расшифровка аудио: " + transcript)
                    for chunk in split_message("🎤 " + transcript):
                        await message.answer(chunk)
                    continue
                try:
                    path, mime = await download_attachment(part, bot, cwd)
                except Exception as e:
                    log.exception("download failed")
                    await message.answer(f"Не удалось скачать вложение: {e}")
                    continue
                if not has_context:
                    continue  # без текста/аудио только сохраняем, очередь не трогаем
                notes.append(f"Вложение: files/from_user/{path.name}")
                if mime in ("image/jpeg", "image/png", "image/gif", "image/webp") and path.stat().st_size <= MAX_IMAGE:
                    images.append((path.read_bytes(), mime))
            if not ready:
                await topic_setup(message, message.from_user.id, force=has_prompt)
                return
            if not has_prompt:
                return
            prompt = "\n\n".join(p for p in (text, "\n".join(notes)) if p)
            reply = await ask(chat_id, prompt, fit_images(images))
        session_files.archive(cwd, "from_user")
    except asyncio.TimeoutError:
        await message.answer("Слишком долго думаю, попробуй ещё раз или упрости запрос. "
                             "Новые файлы сохранены для следующей попытки.")
        return
    except Exception as e:
        log.exception("model failed")
        await message.answer(f"Ошибка: {e}")
        return
    for chunk in split_message(reply):
        if chunk:
            await message.answer(chunk)
    await send_generated_files(message, cwd)


@dp.message(F.text, F.from_user.id.in_(ALLOWED))
async def on_text(message: Message, bot: Bot):
    await respond(message, bot, message.text)


async def collect_album(group_id, bot):
    """Первое вложение резервирует сессию до конца сборки и ответа."""
    parts = []
    batch = albums[group_id]
    try:
        chat_id = scope(batch[0])
        async with locks[chat_id]:
            while True:
                left = album_deadline.get(group_id, 0) - time.monotonic()
                if left <= 0:
                    break
                await asyncio.sleep(left)
            parts = albums.pop(group_id, [])
            album_deadline.pop(group_id, None)
            if parts:
                caption = "\n".join(p.caption for p in parts if p.caption)
                await respond(parts[-1], bot, caption, attachments=parts, locked=True)
    except Exception:
        log.exception("album failed")
        if parts:
            await parts[-1].answer("Не удалось обработать альбом. Попробуй ещё раз.")
    finally:
        if albums.get(group_id) is batch:
            albums.pop(group_id, None)
            album_deadline.pop(group_id, None)


async def receive_files(message, bot):
    if message.media_group_id:
        group_id = (scope(message), message.media_group_id)
        albums[group_id].append(message)
        album_deadline[group_id] = time.monotonic() + ALBUM_WAIT
        if len(albums[group_id]) == 1:
            asyncio.create_task(collect_album(group_id, bot))
        return
    await respond(message, bot, message.caption or "", attachments=[message])


@dp.message(F.photo | F.document.mime_type.startswith("image/"),
            F.from_user.id.in_(ALLOWED))
async def on_photo(message: Message, bot: Bot):
    await receive_files(message, bot)


@dp.message(F.voice | F.audio, F.from_user.id.in_(ALLOWED))
async def on_voice(message: Message, bot: Bot):
    await receive_files(message, bot)


@dp.message(F.document | F.video | F.animation | F.video_note | F.sticker,
            F.from_user.id.in_(ALLOWED))
async def on_file(message: Message, bot: Bot):
    await receive_files(message, bot)


@dp.message(F.from_user.id.in_(ALLOWED))
async def on_other(message: Message):
    await message.answer("Пришли текст, фото, видео, документ, аудио или другое файловое вложение.")


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(SESSIONS, exist_ok=True)
    # падаем на старте, а не на первом сообщении пользователя
    if not os.access(CLAUDE_BIN, os.X_OK):
        raise SystemExit(f"не найден исполняемый claude: {CLAUDE_BIN} "
                         f"(укажи путь в переменной CLAUDE_BIN)")
    bot = Bot(token=TOKEN)
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="что умеет бот"),
            BotCommand(command="info", description="текущая модель и сессия"),
            BotCommand(command="model", description="модель и движок"),
            BotCommand(command="effort", description="уровень размышления"),
            BotCommand(command="new", description="новая сессия или тема группы"),
            BotCommand(command="sessions", description="сессии и темы группы"),
            BotCommand(command="delete", description="удалить сессию"),
        ])
    except TelegramNetworkError as e:
        # меню команд — косметика: если связь легла на старте, не роняем
        # процесс, polling дождётся сети сам и меню встанет при перезапуске
        log.warning("меню команд не обновилось: %s", e)
    if not CODEX_BIN:
        log.warning("codex не найден — доступен только claude")
    log.info("claude=%s codex=%s sessions=%s engine=%s model=%s allowed=%s",
             CLAUDE_BIN, CODEX_BIN, SESSIONS, DEFAULT_ENGINE, DEFAULT_MODEL, ALLOWED)
    # resolve_used_update_types вместо ["message"] — иначе нажатия кнопок
    # (callback_query) до бота просто не доедут.
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
