"""Устойчивые идентификаторы и метаданные тем. История движков хранится отдельно."""
import json
import os
import tempfile
from pathlib import Path


def scope(message):
    if getattr(message.chat, "is_forum", False):
        # General — отдельная тема, даже когда Telegram не присылает thread_id.
        return message.chat.id, getattr(message, "message_thread_id", None) or 1
    return message.chat.id


def is_topic(key):
    return isinstance(key, tuple)


def storage_path(root, key):
    if is_topic(key):
        chat_id, thread_id = key
        return Path(root) / str(int(chat_id)) / "topics" / str(int(thread_id))
    return Path(root) / str(int(key))


def thread_id(message):
    key = scope(message)
    return key[1] if is_topic(key) and key[1] != 1 else None


class Store:
    def __init__(self, root):
        self.root = Path(root)

    @staticmethod
    def read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".topic-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def group_enabled(self, chat_id):
        return self.read(self.root / str(chat_id) / "forum.json").get("enabled", False)

    def enable_group(self, chat_id):
        self.write(self.root / str(chat_id) / "forum.json", {"enabled": True})

    def get(self, key):
        return self.read(storage_path(self.root, key) / "topic.json")

    def update(self, key, **values):
        data = self.get(key)
        data.update(values)
        self.write(storage_path(self.root, key) / "topic.json", data)
        return data

    def ensure(self, key, name=None, owner_id=0):
        data = self.get(key)
        if not data:
            data = self.update(key, name=name or ("Общая" if key[1] == 1 else f"Тема {key[1]}"),
                               owner_id=owner_id, engine=None, closed=False, deleted=False)
        return data

    def list(self, chat_id):
        result = []
        for path in (self.root / str(chat_id) / "topics").glob("*/topic.json"):
            if path.parent.name.isdigit():
                data = self.read(path)
                if not data.get("deleted"):
                    result.append((int(path.parent.name), data))
        return sorted(result, key=lambda item: item[0])


def topic_link(chat_id, thread_id):
    # Внутренние ссылки Telegram работают и для закрытых групп у их участников.
    internal_id = str(chat_id).removeprefix("-100")
    return f"https://t.me/c/{internal_id}/{thread_id}"
