#!/usr/bin/env python3
"""Tiny JSON-file map: Feishu chat_id -> Claude session_id, for context resume."""
import json
import os
import threading

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.json")
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get(chat_id: str) -> str | None:
    with _lock:
        return _load().get(chat_id)


def put(chat_id: str, session_id: str) -> None:
    with _lock:
        data = _load()
        data[chat_id] = session_id
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PATH)  # atomic


def clear(chat_id: str) -> None:
    with _lock:
        data = _load()
        if chat_id in data:
            del data[chat_id]
            with open(_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
