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


def list_all() -> dict[str, str]:
    """Return all stored key->session_id pairs (copy)."""
    with _lock:
        return dict(_load())


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
            # Atomic write (tmp + os.replace), matching put(): a crash mid-write
            # must never truncate sessions.json to a partial/empty file.
            tmp = _PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _PATH)
