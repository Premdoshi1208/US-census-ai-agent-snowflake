from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

MEMORY_FILE = DATA_DIR / "chat_memory.json"

_LOCK = threading.Lock()

DEFAULT_MAX_MESSAGES_PER_SESSION = 30
DEFAULT_CONTEXT_WINDOW = 8


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_store() -> Dict[str, Any]:
    return {
        "sessions": {}
    }


def _ensure_memory_file_exists() -> None:
    if not MEMORY_FILE.exists():
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(_default_store(), f, indent=2)


def _load_store() -> Dict[str, Any]:
    _ensure_memory_file_exists()

    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return _default_store()

        if "sessions" not in data or not isinstance(data["sessions"], dict):
            data["sessions"] = {}

        return data
    except Exception:
        return _default_store()


def _save_store(store: Dict[str, Any]) -> None:
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)


def _generate_session_title(first_user_message: Optional[str]) -> str:
    if not first_user_message:
        return "New Chat"

    text = " ".join(first_user_message.strip().split())
    if not text:
        return "New Chat"

    return text[:50] + ("..." if len(text) > 50 else "")


def create_session(title: Optional[str] = None) -> str:
    session_id = str(uuid.uuid4())

    with _LOCK:
        store = _load_store()

        store["sessions"][session_id] = {
            "session_id": session_id,
            "title": title or "New Chat",
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
            "messages": [],
        }

        _save_store(store)

    return session_id


def list_sessions() -> List[Dict[str, Any]]:
    with _LOCK:
        store = _load_store()
        sessions = list(store["sessions"].values())

    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)

    return [
        {
            "session_id": s["session_id"],
            "title": s.get("title", "New Chat"),
            "created_at": s.get("created_at"),
            "updated_at": s.get("updated_at"),
            "message_count": len(s.get("messages", [])),
        }
        for s in sessions
    ]


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        store = _load_store()
        session = store["sessions"].get(session_id)

    return session


def _trim_messages(messages: List[Dict[str, Any]], max_messages: int) -> List[Dict[str, Any]]:
    if len(messages) <= max_messages:
        return messages
    return messages[-max_messages:]


def append_message(
    session_id: str,
    role: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
    max_messages: int = DEFAULT_MAX_MESSAGES_PER_SESSION,
) -> Dict[str, Any]:
    if role not in {"user", "assistant", "system"}:
        raise ValueError("role must be one of: user, assistant, system")

    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")

    with _LOCK:
        store = _load_store()

        if session_id not in store["sessions"]:
            store["sessions"][session_id] = {
                "session_id": session_id,
                "title": "New Chat",
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
                "messages": [],
            }

        session = store["sessions"][session_id]

        message = {
            "message_id": str(uuid.uuid4()),
            "role": role,
            "content": content.strip(),
            "timestamp": _utc_now_iso(),
            "metadata": metadata or {},
        }

        session["messages"].append(message)
        session["messages"] = _trim_messages(session["messages"], max_messages)
        session["updated_at"] = _utc_now_iso()

        if (
            session.get("title") == "New Chat"
            and role == "user"
            and len(session["messages"]) == 1
        ):
            session["title"] = _generate_session_title(content)

        _save_store(store)

    return message


def get_session_messages(session_id: str) -> List[Dict[str, Any]]:
    with _LOCK:
        store = _load_store()
        session = store["sessions"].get(session_id)

    if not session:
        return []

    return session.get("messages", [])


def get_recent_messages(
    session_id: str,
    limit: int = DEFAULT_CONTEXT_WINDOW,
) -> List[Dict[str, Any]]:
    messages = get_session_messages(session_id)
    return messages[-limit:]


def build_chat_context(
    session_id: str,
    limit: int = DEFAULT_CONTEXT_WINDOW,
) -> str:
    messages = get_recent_messages(session_id, limit=limit)

    if not messages:
        return ""

    lines: List[str] = []

    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        lines.append(f"{role}: {content}")

    return "\n".join(lines)


def update_session_title(session_id: str, title: str) -> bool:
    if not title.strip():
        return False

    with _LOCK:
        store = _load_store()
        session = store["sessions"].get(session_id)

        if not session:
            return False

        session["title"] = title.strip()
        session["updated_at"] = _utc_now_iso()
        _save_store(store)

    return True


def clear_session(session_id: str) -> bool:
    with _LOCK:
        store = _load_store()
        session = store["sessions"].get(session_id)

        if not session:
            return False

        session["messages"] = []
        session["updated_at"] = _utc_now_iso()
        _save_store(store)

    return True


def delete_session(session_id: str) -> bool:
    with _LOCK:
        store = _load_store()

        if session_id not in store["sessions"]:
            return False

        del store["sessions"][session_id]
        _save_store(store)

    return True