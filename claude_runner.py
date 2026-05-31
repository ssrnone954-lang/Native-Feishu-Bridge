"""Claude runner — spawns Claude CLI subprocess, streams output."""

import json
import os
import signal
import subprocess
import sys
import time
from typing import Optional, Generator


def build_prompt(user_text: str, chat_id: str, chat_type: str,
                 sender_id: str, sender_name: str = "",
                 thread_id: str = "", history: list = None) -> str:
    """Build the prompt with bridge_context and optional conversation history."""
    parts = [
        "<bridge_context>",
        f"chat_id: {chat_id}",
        f"chat_type: {chat_type}",
        f"sender_id: {sender_id}",
    ]
    if sender_name:
        parts.append(f"sender_name: {sender_name}")
    if thread_id:
        parts.append(f"thread_id: {thread_id}")
    parts.append("</bridge_context>")

    # Inject conversation history (survives bridge restarts)
    if history:
        parts.append("")
        parts.append("<conversation_history>")
        parts.append("Recent messages in this chat. Use this to maintain continuity:")
        for msg in history:
            role_label = "用户" if msg["role"] == "user" else "Claude"
            parts.append(f"[{role_label}]: {msg['content']}")
        parts.append("</conversation_history>")

    parts.append("")
    parts.append(user_text)
    return "\n".join(parts)


def run_claude(prompt: str, claude_bin: str = "claude",
               cwd: str = None, session_id: str = None,
               timeout_sec: int = 600) -> subprocess.Popen:
    """Start a Claude subprocess. Returns the Popen handle.

    Pass session_id to resume an existing session.
    """
    args = [
        claude_bin,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
    ]
    if session_id:
        args.extend(["--resume", session_id])

    env = os.environ.copy()
    # Feishu domains bypass proxy
    env.setdefault("NO_PROXY", ".feishu.cn,.larksuite.com")

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd or ".",
        env=env,
        text=True,
    )
    return proc


def stream_events(proc: subprocess.Popen, timeout_sec: int = 600
                  ) -> Generator[dict, None, None]:
    """Yield stream-json events from a running Claude process."""
    deadline = time.time() + timeout_sec
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            yield event
        except json.JSONDecodeError:
            continue
        if time.time() > deadline:
            proc.terminate()
            break


def kill_proc(proc: subprocess.Popen):
    """Gracefully kill a Claude process."""
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def extract_session_id(events: list[dict]) -> Optional[str]:
    """Extract the session ID from stream-json events."""
    for e in events:
        if e.get("type") == "system" and "session_id" in str(e):
            # Try to extract from system message
            sid = e.get("session_id", "")
            if sid:
                return sid
        if "session_id" in e:
            return e["session_id"]
    return None


def collect_text(events: list[dict]) -> str:
    """Extract assistant text from stream-json events."""
    parts = []
    for e in events:
        if e.get("type") == "assistant":
            content = e.get("message", {}).get("content", "")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
            elif isinstance(content, str):
                parts.append(content)
    return "".join(parts)
