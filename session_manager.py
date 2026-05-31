"""Session manager — per-chat session persistence + conversation history.

Stores both:
- Claude session_id (for --resume within same process lifetime)
- Recent message history (survives bridge restarts, injected into prompt)
"""

import json
import time
from pathlib import Path
from typing import Optional


MAX_HISTORY = 20  # messages per chat


class SessionManager:
    """Manages per-chat Claude session IDs and conversation history."""

    def __init__(self, sessions_dir: str):
        self.dir = Path(sessions_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._sessions_file = self.dir / "sessions.json"
        self._history_file = self.dir / "history.json"
        self._sessions: dict = self._load(self._sessions_file)
        self._history: dict = self._load(self._history_file)

    def _load(self, file: Path) -> dict:
        if file.exists():
            try:
                return json.loads(file.read_text())
            except Exception:
                pass
        return {}

    def _save_sessions(self):
        self._sessions_file.write_text(
            json.dumps(self._sessions, indent=2, ensure_ascii=False))

    def _save_history(self):
        self._history_file.write_text(
            json.dumps(self._history, indent=2, ensure_ascii=False))

    # ---- session_id (Claude --resume, valid only within process lifetime) ----

    def get(self, chat_id: str) -> Optional[dict]:
        entry = self._sessions.get(chat_id)
        if entry:
            return dict(entry)
        return None

    def save(self, chat_id: str, session_id: str, cwd: str = "."):
        self._sessions[chat_id] = {
            "session_id": session_id,
            "cwd": cwd,
            "updated_at": int(time.time()),
        }
        self._save_sessions()

    def clear(self, chat_id: str):
        self._sessions.pop(chat_id, None)
        self._save_sessions()

    def clear_all(self):
        """Clear all session_ids (call on bridge startup).

        Conversation history is preserved separately — only Claude session_ids
        are cleared since they refer to dead processes.
        """
        self._sessions = {}
        self._save_sessions()

    # ---- conversation history (survives restarts, injected into prompt) ----

    def add_message(self, chat_id: str, role: str, content: str):
        """Append a message to the chat history."""
        if chat_id not in self._history:
            self._history[chat_id] = []
        # Truncate long messages in history to keep prompt lean
        truncated = content[:2000] if len(content) > 2000 else content
        self._history[chat_id].append({
            "role": role,
            "content": truncated,
            "ts": int(time.time()),
        })
        # Keep only last N messages
        if len(self._history[chat_id]) > MAX_HISTORY:
            self._history[chat_id] = self._history[chat_id][-MAX_HISTORY:]
        self._save_history()

    def get_history(self, chat_id: str, n: int = 10) -> list[dict]:
        """Get the last N messages for a chat."""
        msgs = self._history.get(chat_id, [])
        return msgs[-n:] if n > 0 else msgs

    def clear_history(self, chat_id: str):
        self._history.pop(chat_id, None)
        self._save_history()

    def active_count(self) -> int:
        return len(self._sessions)

    def history_count(self) -> int:
        return len(self._history)
