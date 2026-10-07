#!/usr/bin/env python3
"""Offline-проверки: сеть и claude не задействованы."""
import os
import tempfile

# жёстко, а не setdefault: тест не должен зависеть от .env окружения
os.environ["TG_TOKEN"] = "test"
os.environ["TG_ALLOWED"] = "111, 222 333"
os.environ["TG_USERS"] = "444, 555"
tmp = tempfile.mkdtemp()
os.environ["CLAUDE_TG_SESSIONS"] = os.path.join(tmp, "sessions")

import bot  # noqa: E402

big = "".join(str(i % 10) for i in range(10_000))
chunks = bot.split_message(big)
assert all(len(c) <= 4096 for c in chunks), [len(c) for c in chunks]
assert "".join(chunks) == big
assert bot.split_message("") == [""]

assert bot.session_id(42) == bot.session_id(42)
assert bot.session_id(42) != bot.session_id(43)

# разрешённые чаты — массив, разделители: запятая и/или пробел
assert bot.ADMINS == [111, 222, 333], bot.ADMINS
assert bot.USERS == [444, 555], bot.USERS
assert bot.ALLOWED == [111, 222, 333, 444, 555], bot.ALLOWED
assert bot.model_allowed(111, "gpt-6-astra")
assert not bot.model_allowed(444, "gpt-6-astra")
assert bot.model_allowed(444, "gpt-first")

# на каждый чат своя папка + симлинк на общий токен подписки
bot.HOST_CREDS = os.path.join(tmp, "creds.json")
open(bot.HOST_CREDS, "w").write("{}")
# codex: свой токен и свой каталог моделей, чтобы тест не зависел от машины
bot.CODEX_HOST_AUTH = os.path.join(tmp, "codex-auth.json")
open(bot.CODEX_HOST_AUTH, "w").write("{}")
bot.CODEX_BIN = "/bin/true"
bot.MODELS_CACHE = os.path.join(tmp, "models_cache.json")
import json as _json0
_json0.dump({"models": [
    {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list",
     "priority": 0, "description": "Our most capable model for complex, demanding work.",
     "default_reasoning_level": "high",
     "supported_reasoning_levels": [{"effort": "high"}]},
    {"slug": "gpt-hidden", "display_name": "Скрытая", "visibility": "hide", "priority": 1,
     "default_reasoning_level": "low", "supported_reasoning_levels": [{"effort": "low"}]},
    {"slug": "gpt-second", "display_name": "Вторая", "visibility": "list", "priority": 9,
     "description": "Reliable agentic workhorse for everyday tasks.",
     "default_reasoning_level": "medium",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]},
    {"slug": "gpt-first", "display_name": "Первая", "visibility": "list", "priority": 2,
     "description": "Our most capable model for complex, demanding work.",
     "default_reasoning_level": "low",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "ultra"}]},
]}, open(bot.MODELS_CACHE, "w"))
d1, d2 = bot.chat_dir(111), bot.chat_dir(222)
assert d1 != d2
assert d1.endswith(os.path.join("sessions", "111"))
assert os.path.islink(os.path.join(d1, ".credentials.json"))

# симлинк, затёртый обновлением токена, чинится, а свежий токен не теряется
link = os.path.join(d1, ".credentials.json")
os.remove(link)
open(link, "w").write('{"fresh": true}')
bot.chat_dir(111)
assert os.path.islink(link)
assert '"fresh"' in open(bot.HOST_CREDS).read()

# выбор модели: по умолчанию из окружения, хранится на чат, мусор игнорируется
assert bot.get_model(111) == bot.DEFAULT_MODEL
bot.set_model(111, "opus")
assert bot.get_model(111) == "opus"
assert bot.get_model(222) == bot.DEFAULT_MODEL, "выбор одного чата не течёт в другой"
open(os.path.join(bot.chat_dir(111), "model"), "w").write("nonsense")
assert bot.get_model(111) == bot.DEFAULT_MODEL

# клавиатура: обе линейки моделей, галочка на текущей, кнопка отмены
bot.set_model(111, "opus")
kb = bot.model_kb(111)
labels = [b[0].text for b in kb.inline_keyboard]
calls = [b[0].callback_data for b in kb.inline_keyboard]
assert labels[0] == "✓ 🟠 opus", labels
assert labels.count("Отмена") == 1
assert calls[:3] == ["model:claude:opus", "model:claude:sonnet", "model:claude:haiku"], calls
assert calls[3:6] == ["model:codex:gpt-6-astra", "model:codex:gpt-first",
                      "model:codex:gpt-second"], "codex по priority"
assert labels[3] == "🟢 GPT-6-Astra"
assert len(kb.inline_keyboard) == len(bot.MODELS) + 3 + 1
assert calls[-1] == "model:cancel:"

# обычный пользователь видит все модели, кроме доступной только админам Astra
user_calls = [b[0].callback_data for b in bot.model_kb(444).inline_keyboard]
assert "model:codex:gpt-6-astra" not in user_calls
assert "model:codex:gpt-first" in user_calls and "model:codex:gpt-second" in user_calls
assert "GPT-6-Astra" not in bot.model_text(444)

# сообщение объясняет назначение каждой модели из обеих линеек
model_text = bot.model_text(111)
for title in ["opus", "sonnet", "haiku", "GPT-6-Astra", "Первая", "Вторая"]:
    assert title in model_text, (title, model_text)
assert "для самых сложных задач" in model_text
assert "надёжная универсальная модель" in model_text

print("ok (модели)")

# статистика сессии: считаем только реплики, служебные записи игнорируем
assert bot.session_stats(444) == (0, 0, 0), "нет файла сессии — нули"
d = os.path.join(bot.chat_dir(444), "projects", "-any-slug")
os.makedirs(d)
with open(os.path.join(d, bot.session_id(444) + ".jsonl"), "w") as f:
    f.write('{"type":"user"}\n{"type":"assistant"}\n{"type":"assistant"}\n')
    f.write('{"type":"queue-operation"}\n\n{"type":"user"')  # мусор и обрыв
mine, claude, size = bot.session_stats(444)
assert (mine, claude) == (1, 2), (mine, claude)
assert size > 0

assert bot.human_size(512) == "512 Б"
assert bot.human_size(1536) == "1.5 КБ"
assert bot.human_size(5 * 1024 * 1024) == "5.0 МБ"

print("ok (статистика)")

# --- сессии: активная, список, страницы ---
import json as _json, time as _time

CHAT = 555
assert bot.active_session(CHAT) == bot.session_id(CHAT), "старые чаты работают как раньше"
bot.set_active_session(CHAT, "aaaa-1")
assert bot.active_session(CHAT) == "aaaa-1"

pdir = os.path.join(bot.chat_dir(CHAT), "projects", "-slug")
os.makedirs(pdir, exist_ok=True)


def make(sid, title=None, prompt=None, msgs=1, age=0):
    p = os.path.join(pdir, sid + ".jsonl")
    with open(p, "w") as f:
        for _ in range(msgs):
            f.write('{"type":"user"}\n{"type":"assistant"}\n')
        if prompt:
            f.write(_json.dumps({"type": "last-prompt", "lastPrompt": prompt}) + "\n")
        if title:
            f.write(_json.dumps({"type": "ai-title", "aiTitle": title}) + "\n")
    os.utime(p, (_time.time() - age, _time.time() - age))
    return p


make("aaaa-1", title="Заголовок от Claude", msgs=3, age=100)
make("bbbb-2", prompt="Первый вопрос без заголовка", age=50)
for n in range(3, 9):  # всего 8 сессий -> две страницы
    make(f"cccc-{n}", title=f"Тема {n}", age=n)

items = bot.session_list(CHAT)
assert len(items) == 8
assert items[0][4] >= items[-1][4], "свежие сверху"
assert {e for e, _, _, _, _ in items} == {"claude"}

text, kb = bot.sessions_page(CHAT, 0)
assert "1/2" in [b.text for row in kb.inline_keyboard for b in row]
assert len(kb.inline_keyboard[0]) == 5, "5 кнопок с цифрами в ряд"
assert len(kb.inline_keyboard[1]) == 3, "влево / счётчик / вправо"
assert text.count("\n") == 5

text2, kb2 = bot.sessions_page(CHAT, 1)
assert "6." in text2 and "8." in text2, "нумерация продолжается со страницы 1"
assert len(kb2.inline_keyboard[0]) == 3, "на последней странице сколько осталось"

# заголовок: ai-title, иначе первый вопрос
assert "Заголовок от Claude" in bot.sessions_page(CHAT, 0)[0] + text2
assert "Первый вопрос без заголовка" in text + text2
# галочка стоит на активной
assert "✓" in text + text2
# за границы страниц не уходим
assert bot.sessions_page(CHAT, 99)[0] == text2
assert bot.sessions_page(CHAT, -5)[0] == text

# новая сессия ещё без файла — всё равно в списке и помечена
bot.set_active_session(CHAT, "dddd-new")
assert any(sid == "dddd-new" for _, sid, _, _, _ in bot.session_list(CHAT))
assert "новая, пока пустая" in bot.sessions_page(CHAT, 0)[0]

print("ok (сессии)")

# --- удаление сессий ---
# тот же список, но кнопки ведут на удаление
dtext, dkb = bot.sessions_page(CHAT, 0, "del")
assert "удалить" in dtext.lower(), dtext
assert all(b.callback_data.startswith("del:claude:") for b in dkb.inline_keyboard[0])
assert [b.callback_data for b in dkb.inline_keyboard[1]] == [
    "delpage:-1", "delpage:same", "delpage:1"], "стрелки остаются в своём режиме"
assert "1/2" in [b.text for row in dkb.inline_keyboard for b in row]

# обычное удаление: файл исчез, соседи целы
assert os.path.exists(os.path.join(pdir, "cccc-3.jsonl"))
bot.delete_session(CHAT, "claude", "cccc-3")
assert not os.path.exists(os.path.join(pdir, "cccc-3.jsonl"))
assert not any(sid == "cccc-3" for _, sid, _, _, _ in bot.session_list(CHAT))
assert os.path.exists(os.path.join(pdir, "cccc-4.jsonl"))

# удалили активную — переезжаем на самую свежую из оставшихся
bot.set_active_session(CHAT, "bbbb-2")
bot.delete_session(CHAT, "claude", "bbbb-2")
fresh = bot.session_list(CHAT)[0][1]
assert bot.active_session(CHAT) == fresh != "bbbb-2", bot.active_session(CHAT)

# удалили всё — активной становится новая пустая, а не стёртый id
for _, sid, _, msgs, _ in bot.session_list(CHAT):
    if msgs:
        bot.delete_session(CHAT, "claude", sid)
left = bot.session_list(CHAT)
assert len(left) == 1 and left[0][3] == 0, left
assert bot.active_session(CHAT) == left[0][1]
assert "новая, пока пустая" in bot.sessions_page(CHAT, 0, "del")[0]

print("ok (удаление сессий)")

# --- два движка: claude и codex ---
DUO = 666
assert bot.get_engine(DUO) == bot.DEFAULT_ENGINE == "claude"

# каталог моделей codex: только видимые, по priority, со своими уровнями
assert [slug for slug, _, _, _ in bot.codex_models()] == [
    "gpt-6-astra", "gpt-first", "gpt-second"]
assert bot.codex_efforts("gpt-first") == ["low", "high", "ultra"]
assert bot.codex_efforts("нет такой") == []

# настройки движков не путаются между собой
bot.set_model(DUO, "opus")
bot.set_state(DUO, "engine", "codex")
assert bot.get_engine(DUO) == "codex"
assert bot.get_model(DUO) == "gpt-6-astra", "по умолчанию первая модель каталога"
bot.set_model(DUO, "gpt-second", "codex")
assert bot.get_model(DUO) == "gpt-second"
assert bot.get_model(DUO, "claude") == "opus", "модель claude на месте"

# уровень размышления: чужой для этой модели не применяется
bot.set_effort(DUO, "ultra", "codex")
assert bot.get_effort(DUO) == "", "у gpt-second уровня ultra нет"
bot.set_effort(DUO, "medium", "codex")
assert bot.get_effort(DUO) == "medium"
bot.set_effort(DUO, "xhigh")
assert bot.get_effort(DUO, "claude") == "xhigh"
levels = [b[0].text for b in bot.effort_kb(DUO).inline_keyboard]
assert levels == ["low", "✓ medium", "по умолчанию"], levels

# сборка команды codex: без id — новая сессия, с id — продолжение
args = bot.codex_args(DUO, "", "medium", "/tmp/ans.txt", ["/tmp/a.png", "/tmp/b.jpg"])
assert args[1] == "exec" and "resume" not in args
assert args[args.index("-m") + 1] == "gpt-second"
assert args[args.index("-c") + 1] == 'model_reasoning_effort="medium"'
assert "--skip-git-repo-check" in args and args[-1] == "-"
assert args.count("-i") == 2 and "/tmp/b.jpg" in args
assert args[args.index("-C") + 1] == bot.workspace_dir(DUO, "codex"), "новой сессии нужна рабочая папка"
resumed = bot.codex_args(DUO, "th-1", "", "/tmp/ans.txt", [])
assert resumed[1] == "exec" and resumed[4:6] == ["resume", "th-1"]
assert resumed[resumed.index("-C") + 1] == bot.workspace_dir(DUO, "codex")
assert not any("model_reasoning_effort" in arg for arg in resumed)
assert 'sandbox_mode="workspace-write"' in resumed

# разбор событий codex: id, ответ, ошибка; мусор игнорируется
out = (b'not json\n{"type":"thread.started","thread_id":"th-9"}\n'
       b'{"type":"item.completed","item":{"type":"reasoning","text":"..."}}\n'
       b'{"type":"item.completed","item":{"type":"agent_message","text":"\xd0\x9f\xd0\xb0\xd1\x80\xd0\xb8\xd0\xb6"}}\n'
       b'{"type":"turn.completed","usage":{}}\n')
assert bot.parse_codex(out) == ("th-9", "Париж", "")
failed = '{"type":"turn.failed","error":{"message":"лимит"}}'.encode()
assert bot.parse_codex(failed)[2] == "лимит"
assert bot.parse_codex(b"") == ("", "", "")

# индекс сессий codex: заголовок от первого вопроса, счётчики растут
bot.bump_codex_index(DUO, "th-9", "Первый вопрос к codex")
bot.bump_codex_index(DUO, "th-9", "второй")
mine, theirs, title = bot.read_codex_index(bot.codex_index(DUO, "th-9"))
assert (mine, theirs, title) == (2, 2, "Первый вопрос к codex")
bot.set_active_session(DUO, "th-9", "codex")

# общий список: обе сессии в одном списке, с пометками движков
cdir = os.path.join(bot.chat_dir(DUO), "projects", "-slug")
os.makedirs(cdir, exist_ok=True)
with open(os.path.join(cdir, "cl-1.jsonl"), "w") as f:
    f.write('{"type":"user"}\n{"type":"ai-title","aiTitle":"Разговор с claude"}\n')
both = bot.session_list(DUO)
assert {e for e, _, _, _, _ in both} == {"claude", "codex"}, both
dtext, dkb = bot.sessions_page(DUO, 0)
assert "🟢 Первый вопрос к codex — 4 сообщ." in dtext, dtext
assert "🟠 Разговор с claude" in dtext
assert "✓ 🟢" in dtext and "✓ 🟠" not in dtext, "галочка только у активной текущего движка"
picks = sorted(b.callback_data for b in dkb.inline_keyboard[0])
assert picks == ["sess:claude:cl-1", "sess:codex:th-9"], picks
assert bot.session_title(DUO, "codex", "th-9") == "Первый вопрос к codex"
assert bot.known_session(DUO, "codex", "th-9") and not bot.known_session(DUO, "claude", "th-9")

# удаление сессии codex: индекс исчез, сессия claude цела
bot.delete_session(DUO, "codex", "th-9")
assert not os.path.exists(bot.codex_index(DUO, "th-9"))
assert bot.active_session(DUO) == "", "активной стала новая пустая сессия codex"
assert os.path.exists(os.path.join(cdir, "cl-1.jsonl"))
assert [e for e, _, _, _, _ in bot.session_list(DUO)].count("claude") == 1

print("ok (движки)")

# --- картинки: сборка stream-json и разбор ответа ---
import base64 as _b64

line = bot.build_input("что тут?", [(b"\x89PNG-bytes", "image/png")])
payload = _json.loads(line)
assert payload["type"] == "user" and payload["message"]["role"] == "user"
blocks = payload["message"]["content"]
assert blocks[0] == {"type": "text", "text": "что тут?"}
assert blocks[1]["type"] == "image"
assert blocks[1]["source"]["media_type"] == "image/png"
assert _b64.b64decode(blocks[1]["source"]["data"]) == b"\x89PNG-bytes"
assert line.endswith("\n"), "claude читает stream-json построчно"

# фото без подписи — только картинка, пустого текстового блока быть не должно
only_img = _json.loads(bot.build_input("", [(b"x", "image/jpeg")]))
assert len(only_img["message"]["content"]) == 1

# ответ берём из строки result, мусор и промежуточные события игнорируем
out = b'not json\n{"type":"assistant"}\n{"type":"result","result":"answer","is_error":false}\n'
assert bot.parse_result(out)["result"] == "answer"
assert bot.parse_result(b"") is None

print("ok (картинки)")

# --- голосовые ---
# модель не должна грузиться при импорте: старт бота остаётся мгновенным
assert bot.stt is None, "модель распознавания грузится лениво, при первом голосовом"
names = [h.callback.__name__ for h in bot.dp.message.handlers]
assert names.index("on_voice") < names.index("on_other"), "иначе голосовое уйдёт в заглушку"
assert names.index("on_photo") < names.index("on_other")

print("ok (голосовые)")

# --- альбомы ---
# несколько картинок в одном запросе, порядок сохраняется
line = bot.build_input("подпись", [(b"one", "image/jpeg"), (b"two", "image/png")])
blocks = _json.loads(line)["message"]["content"]
assert [b["type"] for b in blocks] == ["text", "image", "image"]
assert _b64.b64decode(blocks[1]["source"]["data"]) == b"one"
assert _b64.b64decode(blocks[2]["source"]["data"]) == b"two"

# суммарный объём режется, а не улетает в API целиком
imgs = [(b"x" * 6, "image/jpeg")] * 4
assert len(bot.fit_images(imgs, limit=20)) == 3
assert bot.fit_images(imgs, limit=1) == []
assert len(bot.fit_images(imgs, limit=10**9)) == 4

print("ok (альбомы)")

# сборщик альбома: три сообщения с общим media_group_id -> один вызов Claude
import asyncio as _aio, io as _io, types as _types

bot.ALBUM_WAIT = 0.05
captured = {}


original_respond = bot.respond


async def fake_respond(message, b, text, images=(), attachments=(), locked=False):
    captured["text"], captured["images"] = text, [(m.photo[-1].payload, "image/jpeg") for m in attachments]

bot.respond = fake_respond
fake_bot = _types.SimpleNamespace(
    download=lambda src: _aio.sleep(0, result=_io.BytesIO(src.payload)))


def fake_photo(payload, caption=None, gid="G1"):
    size = _types.SimpleNamespace(file_size=len(payload), payload=payload)
    msg = _types.SimpleNamespace(media_group_id=gid, photo=[size], caption=caption,
                                 document=None, chat=_types.SimpleNamespace(id=CHAT))
    msg.answer = lambda *a, **k: _aio.sleep(0)
    return msg


async def album_case():
    for i, payload in enumerate([b"aa", b"bb", b"cc"]):
        await bot.on_photo(fake_photo(payload, "что тут?" if i == 0 else None), fake_bot)
        await _aio.sleep(0.01)  # приходят вразнобой, но подряд
    await _aio.sleep(0.3)

_aio.run(album_case())
assert [d for d, _ in captured["images"]] == [b"aa", b"bb", b"cc"], captured["images"]
assert captured["text"] == "что тут?", "подпись альбома одна, у первого сообщения"
assert not bot.albums and not bot.album_deadline, "буферы за собой убираются"

print("ok (сборка альбома)")
bot.respond = original_respond


# смена движка: заводим чистую сессию и сразу показываем список на выбор
CHAT3 = 999
bot.set_state(CHAT3, "engine", "claude")
bot.set_active_session(CHAT3, "claude-old", "claude")
sent = []


async def fake_answer(text, **kw):
    sent.append(text)


def fake_call(data, user=111):
    msg = _types.SimpleNamespace(chat=_types.SimpleNamespace(id=CHAT3),
                                 answer=fake_answer)
    msg.edit_text = lambda *a, **kw: _aio.sleep(0)
    return _types.SimpleNamespace(data=data, message=msg,
                                  from_user=_types.SimpleNamespace(id=user),
                                  answer=lambda *a, **kw: _aio.sleep(0))


_aio.run(bot.on_model_pick(fake_call("model:codex:gpt-first")))
assert bot.get_engine(CHAT3) == "codex"
assert bot.active_session(CHAT3) == "", "у codex id появится только с первым ответом"
assert len(sent) == 1 and "выберите" in sent[0], sent
assert bot.active_session(CHAT3, "claude") == "claude-old", "сессия claude не тронута"

# смена модели внутри того же движка сессию не трогает
_aio.run(bot.on_model_pick(fake_call("model:codex:gpt-6-astra")))
assert len(sent) == 1, "список шлём только при смене движка"
assert bot.active_session(CHAT3) == ""

# обратно на claude — снова чистая сессия, прежняя цела в списке
_aio.run(bot.on_model_pick(fake_call("model:claude:opus")))
assert bot.get_engine(CHAT3) == "claude"
assert bot.active_session(CHAT3) not in ("", "claude-old"), "claude тоже начинает с чистой"
assert len(sent) == 2

print("ok (смена движка заводит новую сессию)")
