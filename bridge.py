#!/usr/bin/env python3
"""
Native Feishu Bridge — direct WS connection to Feishu, Claude-powered replies.

Usage:
    python bridge.py                # Run in foreground (WebSocket)
    python bridge.py --once "msg"   # One-shot test (text only)

Control:
    fq start / stop / status / logs
"""

import argparse
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

import yaml
import lark_oapi
from lark_oapi.ws import Client as WsClient
from lark_oapi.event.dispatcher_handler import (
    EventDispatcherHandler,
    EventDispatcherHandlerBuilder,
)
from lark_oapi.api.im.v1.model import (
    P2ImMessageReceiveV1,
    GetMessageResourceRequest,
)

# Local modules
from session_manager import SessionManager
from sender import FeishuSender
from intake import ActiveRuns, PendingQueue, process_audio_message
from claude_runner import build_prompt, run_claude, stream_events, kill_proc, collect_text

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(__file__).resolve().parent
CONFIG_PATH = CONFIG_DIR / "config.yaml"
PID_FILE = "/tmp/native-feishu-bridge.pid"


def load_config(path=None):
    path = Path(path or CONFIG_PATH)
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # Expand ~ in all path values
    _expand_paths(cfg)
    return cfg


def _expand_paths(cfg: dict):
    """Expand ~ to user home in path config values."""
    _path_keys = {
        "paths": ("sessions_dir", "logs_dir", "pid_file"),
        "messages": ("download_dir",),
        "claude": ("binary", "cwd"),
    }
    for section, keys in _path_keys.items():
        if section not in cfg:
            continue
        for key in keys:
            val = cfg[section].get(key)
            if isinstance(val, str) and val.startswith("~"):
                cfg[section][key] = os.path.expanduser(val)


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_cfg = None
_sender: Optional[FeishuSender] = None
_sessions: Optional[SessionManager] = None
_active_runs: Optional[ActiveRuns] = None
_pending: Optional[PendingQueue] = None


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def handle_message(event: P2ImMessageReceiveV1):
    """Handle an incoming IM message from Feishu."""
    try:
        msg_data = event.event

        msg_type = msg_data.message.message_type
        chat_id = msg_data.message.chat_id
        message_id = msg_data.message.message_id
        sender = msg_data.sender
        sender_id = sender.sender_id if sender else ""

        print(f"[bridge] In: type={msg_type} chat={chat_id[:20]}... "
              f"msg={message_id[:20]}...", file=sys.stderr)

        scope = chat_id

        # Preempt: interrupt any active Claude run for this chat
        _active_runs.interrupt(scope)

        if msg_type == "audio":
            _handle_audio(msg_data, scope)
        elif msg_type == "text":
            content_str = msg_data.message.content or "{}"
            try:
                text = json.loads(content_str).get("text", content_str)
            except (json.JSONDecodeError, TypeError):
                text = str(content_str)

            if text.strip():
                _pending.enqueue(scope, {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "sender_id": sender_id,
                    "text": text,
                    "kind": "text",
                })
        else:
            _pending.enqueue(scope, {
                "chat_id": chat_id,
                "message_id": message_id,
                "sender_id": sender_id,
                "text": f"[{msg_type} message]",
                "kind": "media",
            })

    except Exception:
        print(f"[bridge] Error: {traceback.format_exc()}", file=sys.stderr)


def _handle_audio(msg_data, scope: str):
    """Download and transcribe an audio message."""
    message = msg_data.message
    chat_id = message.chat_id
    message_id = message.message_id

    content_str = message.content or "{}"
    try:
        content = json.loads(content_str)
    except json.JSONDecodeError:
        content = {}
    print(f"[bridge] Audio content keys: {list(content.keys())}", file=sys.stderr)
    file_key = content.get("file_key", "")

    if not file_key:
        print("[bridge] Audio without file_key", file=sys.stderr)
        return

    download_dir = Path(_cfg.get("messages", {}).get(
        "download_dir", "/tmp/native-bridge-media"))
    download_dir.mkdir(parents=True, exist_ok=True)
    audio_path = str(download_dir / f"{message_id}.ogg")

    if not os.path.exists(audio_path):
        print(f"[bridge] Downloading audio {file_key} ...", file=sys.stderr)
        ok = _sender.download_media(message_id, file_key, "file", audio_path)
        if not ok:
            _pending.enqueue(scope, {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": "[语音消息 — 下载失败]",
                "kind": "voice_error",
            })
            return

    cleaned = process_audio_message(audio_path, _cfg)
    if cleaned:
        print(f"[bridge] Voice → text: {cleaned[:80]}...", file=sys.stderr)
        _pending.enqueue(scope, {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": f"[用户通过语音说: \"{cleaned}\"]\n请回应以上内容。",
            "transcript": cleaned,
            "kind": "voice",
        })
    else:
        _pending.enqueue(scope, {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": "[语音消息 — 转写失败，请重试或改为文字]",
            "kind": "voice_error",
        })


# ---------------------------------------------------------------------------
# Flush handler
# ---------------------------------------------------------------------------

def on_flush(scope: str, batch: list[dict]):
    """Called when debounce timer fires. Runs Claude and replies."""
    if not batch:
        return

    first = batch[0]
    last = batch[-1]
    chat_id = first["chat_id"]

    texts = [m["text"] for m in batch if m.get("text")]
    user_text = "\n\n".join(texts)

    print(f"[bridge] Flush: scope={scope} msgs={len(batch)} "
          f"text={len(user_text)}chars", file=sys.stderr)

    reply_to = last.get("message_id")
    sender_id = first.get("sender_id", "")

    # Load conversation history (survives bridge restarts)
    history = _sessions.get_history(chat_id, n=10)

    prompt = build_prompt(
        user_text=user_text,
        chat_id=chat_id,
        chat_type="p2p",
        sender_id=sender_id,
        sender_name="",
        history=history,
    )

    # Save user message(s) to history
    for m in batch:
        if m.get("text") and m.get("kind") in ("text", "voice"):
            _sessions.add_message(chat_id, "user", m["text"])

    existing = _sessions.get(chat_id)
    session_id = existing["session_id"] if existing else None
    claude_bin = _cfg["claude"]["binary"]
    cwd = _cfg["claude"].get("cwd", ".")

    # Always send a status card for progress visibility.
    # Structured content → rich card with native table (1 message, card patched)
    # Simple content → simple card (1 message, card patched)
    status_card = {
        "schema": "2.0",
        "config": {"streaming_mode": True},
        "body": {"elements": [{"tag": "markdown", "content": "🧠 思考中…"}]},
    }
    status_msg_id = _sender.send_card(chat_id, status_card)
    print(f"[bridge] Status card id: {status_msg_id}", file=sys.stderr)

    print(f"[bridge] Claude: session={session_id or 'new'} ...", file=sys.stderr)
    t0 = time.time()
    proc = run_claude(prompt, claude_bin=claude_bin, cwd=cwd, session_id=session_id)
    run = _active_runs.register(scope, proc)

    try:
        # Stream events with progressive status updates
        events = []
        text_parts = []
        tool_count = 0
        evt_types_seen = set()
        last_update = 0
        last_status = "正在思考…"
        update_interval = 1.5  # seconds between status updates

        for evt in stream_events(proc):
            events.append(evt)
            evt_type = evt.get("type", "")
            evt_types_seen.add(evt_type)
            now = time.time()

            # Collect assistant text + detect thinking/tools from content blocks
            new_status = last_status
            if evt_type == "assistant":
                msg = evt.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        block_type = block.get("type", "")
                        if block_type == "text":
                            text_parts.append(block.get("text", ""))
                        elif block_type == "thinking":
                            new_status = "🧠 思考中…"
                        elif block_type == "tool_use":
                            tool_count += 1
                            new_status = f"🔧 调用: {block.get('name','?')} ({tool_count})"
                        elif block_type == "tool_result":
                            new_status = f"🔧 工具完成 ({tool_count})"
                elif isinstance(content, str):
                    text_parts.append(content)

            # When text output starts, override thinking status
            if evt_type == "assistant":
                char_count = sum(len(p) for p in text_parts)
                if char_count > 0 and not new_status.startswith("🔧"):
                    new_status = f"✍️ 正在回复…"

            # Throttled status update
            if new_status != last_status and now - last_update > update_interval:
                print(f"[bridge] Status: {new_status}", file=sys.stderr)
                _update_status(status_msg_id, chat_id, new_status)
                last_status = new_status
                last_update = now

        text = "".join(text_parts)
        elapsed = time.time() - t0
        print(f"[bridge] Claude done: {len(events)} events, types={evt_types_seen}, "
              f"text={len(text)}chars, elapsed={elapsed:.1f}s", file=sys.stderr)

        if not text.strip():
            text = "（未返回内容）"

        # Save Claude reply to conversation history
        if text.strip() and text != "（未返回内容）":
            _sessions.add_message(chat_id, "assistant", text)

        new_sid = _extract_session_id(events) or session_id or ""
        if new_sid:
            _sessions.save(chat_id, new_sid, cwd=cwd)

        # Replace status with final reply
        if status_msg_id:
            _update_or_resend(status_msg_id, chat_id, text)
        else:
            _send_reply(chat_id, text)

        print(f"[bridge] Reply: {len(text)} chars ({elapsed:.1f}s)", file=sys.stderr)

    except Exception:
        print(f"[bridge] Claude error: {traceback.format_exc()}", file=sys.stderr)
        _sender.send_text(chat_id, "抱歉，处理你的消息时出错了。")
    finally:
        _active_runs.unregister(scope, run)
        if proc and proc.poll() is None:
            kill_proc(proc)


def _extract_session_id(events: list[dict]) -> Optional[str]:
    for e in events:
        sid = e.get("session_id", "")
        if sid:
            return sid
    return None


def _has_table(text: str) -> bool:
    """Detect if markdown contains a table."""
    lines = text.split("\n")
    pipe_lines = [l for l in lines if l.strip().startswith("|")]
    return len(pipe_lines) >= 2


def _send_reply(chat_id: str, text: str):
    """Send final reply, choosing format based on content structure.

    - Structured (tables, multi-heading, hr) → **post** (proper heading
      hierarchy, native table rendering, reasonable spacing)
    - Simple → post with markdown → plain text fallback

    Rationale: Feishu card markdown does NOT support heading font-size
    differentiation or native table rendering. Post format does both.
    """
    reply_mode = _cfg.get("messages", {}).get("reply_mode", "post")
    if reply_mode == "post":
        _sender.send_post(chat_id, text)
    else:
        _sender.send_text(chat_id, text)


def _update_status(msg_id: str, chat_id: str, status: str):
    """Update the status card in-place."""
    if not msg_id:
        return
    try:
        card = {
            "schema": "2.0",
            "config": {"streaming_mode": True},
            "body": {"elements": [{"tag": "markdown", "content": status}]},
        }
        content = json.dumps(card, ensure_ascii=False)
        body = lark_oapi.api.im.v1.model.PatchMessageRequestBody.builder() \
            .content(content) \
            .build()
        req = lark_oapi.api.im.v1.model.PatchMessageRequest.builder() \
            .message_id(msg_id) \
            .request_body(body) \
            .build()
        resp = _sender.client.im.v1.message.patch(req)
        if not resp.success():
            print(f"[bridge] Status update failed: code={resp.code} msg={resp.msg}", file=sys.stderr)
    except Exception as e:
        print(f"[bridge] Status update error: {e}", file=sys.stderr)


def _update_or_resend(old_msg_id: str, chat_id: str, text: str):
    """Patch the status card to become the final reply.

    Structured content → rich card with native table component
    Simple content → plain markdown card
    Falls back to _send_reply (post) if patch fails.
    """
    from sender import FeishuSender
    try:
        if FeishuSender.should_use_rich_card(text):
            card = _sender.build_rich_card(text)
        else:
            card = {
                "schema": "2.0",
                "config": {"streaming_mode": False},
                "body": {"elements": [{"tag": "markdown", "content": text}]},
            }

        content = json.dumps(card, ensure_ascii=False)
        body = lark_oapi.api.im.v1.model.PatchMessageRequestBody.builder() \
            .content(content) \
            .build()
        req = lark_oapi.api.im.v1.model.PatchMessageRequest.builder() \
            .message_id(old_msg_id) \
            .request_body(body) \
            .build()
        resp = _sender.client.im.v1.message.patch(req)
        if resp.success():
            print(f"[bridge] Status card → final card", file=sys.stderr)
            return
        print(f"[bridge] Card patch failed: code={resp.code}, sending new msg", file=sys.stderr)
    except Exception as e:
        print(f"[bridge] Card update error: {e}", file=sys.stderr)
    _send_reply(chat_id, text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _cfg, _sender, _sessions, _active_runs, _pending

    parser = argparse.ArgumentParser(description="Native Feishu Bridge")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--once", metavar="TEXT", help="One-shot test")
    args = parser.parse_args()

    _cfg = load_config(args.config)

    # Init voice pipeline
    if _cfg.get("voice", {}).get("enabled"):
        import voice_pipeline
        voice_pipeline.init(_cfg)

    # Init components
    fc = _cfg["feishu"]
    _sender = FeishuSender(fc["app_id"], fc["app_secret"], fc["domain"])
    _sessions = SessionManager(_cfg["paths"]["sessions_dir"])
    _sessions.clear_all()  # Start fresh each bridge run — old session IDs are dead
    _active_runs = ActiveRuns()
    _pending = PendingQueue(
        debounce_ms=_cfg.get("messages", {}).get("debounce_ms", 800),
        callback=on_flush,
    )

    # One-shot test
    if args.once:
        on_flush("test", [{
            "chat_id": "test",
            "message_id": "test",
            "sender_id": "test",
            "text": args.once,
            "kind": "text",
        }])
        return

    # WebSocket mode
    print("[bridge] Starting native Feishu bridge ...", file=sys.stderr)
    print(f"[bridge] Voice: {'enabled' if _cfg.get('voice', {}).get('enabled') else 'disabled'}",
          file=sys.stderr)

    # Write PID
    Path(_cfg["paths"].get("pid_file", PID_FILE)).write_text(str(os.getpid()))

    # Build event handler
    encrypt_key = fc.get("encrypt_key", "")
    verify_token = fc.get("verification_token", "")
    handler = EventDispatcherHandler.builder(
        encrypt_key=encrypt_key,
        verification_token=verify_token,
    )
    handler.register_p2_im_message_receive_v1(handle_message)
    dispatcher = handler.build()

    domain_url = (
        lark_oapi.LARK_DOMAIN if fc.get("domain") == "lark"
        else lark_oapi.FEISHU_DOMAIN
    )
    client = WsClient(
        app_id=fc["app_id"],
        app_secret=fc["app_secret"],
        domain=domain_url,
        event_handler=dispatcher,
        auto_reconnect=True,
    )

    def shutdown(sig=None, frame=None):
        print("[bridge] Shutting down ...", file=sys.stderr)
        try:
            os.remove(_cfg["paths"].get("pid_file", PID_FILE))
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("[bridge] Connected. Waiting for messages ...", file=sys.stderr)
    client.start()


if __name__ == "__main__":
    main()
