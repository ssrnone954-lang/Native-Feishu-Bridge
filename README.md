# Native Feishu Bridge

A lightweight Python bridge that connects [Feishu/Lark](https://www.feishu.cn) messenger to [Claude Code](https://claude.ai/claude-code). Send text or voice messages in Feishu, get Claude-powered replies — with real-time progress, rich formatting, and built-in speech-to-text.

## Features

- **Text & Voice** — Send text or voice messages; voice is auto-transcribed (Whisper) and polished (DeepSeek)
- **Real-time Progress** — Interactive card shows Claude's thinking → tool calls → replying
- **Rich Formatting** — Structured replies get headings, tables, section dividers in a clean card layout
- **Session Continuity** — Conversation history persists across bridge restarts
- **Preemptive Replies** — New message interrupts old Claude run (no queue buildup)
- **Debounced Batching** — Rapid-fire messages are merged (800ms window)
- **Single Binary** — One Python process, no database, no Docker, no framework

## Prerequisites

| Requirement | How to Get It |
|---|---|
| **Python 3.9+** | `python3 --version` (macOS comes with it) |
| **Claude Code CLI** | `npm install -g @anthropic-ai/claude-code` |
| **Feishu App** (free) | Create at [open.feishu.cn](https://open.feishu.cn) — see below |
| **DeepSeek API Key** (optional) | [platform.deepseek.com](https://platform.deepseek.com) — for voice polishing |
| **macOS / Linux** | Windows works but launchd plist is macOS-only |

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/YOUR_USERNAME/native-feishu-bridge.git
cd native-feishu-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Create a Feishu App

1. Go to [Feishu Open Platform](https://open.feishu.cn) → **Create Custom App**
2. Go to **Credentials & Basic Info** → copy `App ID` and `App Secret`
3. Go to **Permission Management** → add these permissions:
   - `im:message` — Read/send messages
   - `im:message:send_as_bot` — Send messages as bot
   - `im:resource` — Download media files (for voice)
4. Go to **Event Subscription** → enable, then subscribe to `im.message.receive_v1`
5. Go to **App Release** → **Create Version** → **Release** (only you need to approve it)

### 3. Configure

```bash
cp config.yaml.example config.yaml
nano config.yaml  # or use any text editor
```

Fill in at minimum:
- `feishu.app_id` and `feishu.app_secret` (from step 2)
- `voice.summarize.api_key` (from DeepSeek, or set `voice.enabled: false`)

### 4. Run

```bash
./bin/fq start     # Start in background
./bin/fq status    # Check if it's running
./bin/fq logs      # Watch logs
```

Send a message to your Feishu app — Claude should reply.

### Auto-start on Login (macOS)

```bash
./bin/fq load      # Loads launchd plist — bridge starts on boot
./bin/fq unload    # Disable auto-start
```

## Configuration Reference

See [`config.yaml.example`](config.yaml.example) for every option with comments.

Key sections:

| Section | Required | Purpose |
|---|---|---|
| `feishu` | **Yes** | App credentials from Feishu Open Platform |
| `claude` | **Yes** | Path to Claude CLI binary |
| `voice` | No | Speech-to-text (Whisper) + polish (DeepSeek) |
| `messages` | No | Debounce timing, reply format preference |
| `paths` | No | Where to store sessions, logs, media |

## Architecture

```
Feishu WS Event (im.message.receive_v1)
  │
  ▼
intake.py          ← queue (800ms debounce), preemption, concurrency
  │
  ├─ text → enqueue directly
  └─ voice → download OGG → Whisper (1-2s) → DeepSeek polish (5-10s) → enqueue
  │
  ▼
claude_runner.py   ← build prompt (with conversation history), spawn Claude CLI
  │
  ▼
bridge.py          ← stream-json parse, status card updates (🧠→🔧→✍️)
  │
  ▼
sender.py          ← send reply as post (simple) or interactive card (structured)
```

### Modules (~1,150 lines total)

| File | Lines | What It Does |
|---|---|---|
| `bridge.py` | ~310 | Main: WS connection, lifecycle, status cards, reply strategy |
| `sender.py` | ~290 | Send text/post/card, build rich cards, download media |
| `claude_runner.py` | ~85 | Spawn Claude subprocess, build prompt, parse stream-json |
| `intake.py` | ~85 | Message queue, debounce, preemption management |
| `voice_pipeline.py` | ~100 | Hot Whisper model + DeepSeek summarization |
| `session_manager.py` | ~85 | Session IDs + conversation history persistence |
| `bin/fq` | ~140 | CLI: start/stop/restart/status/logs/test/load/unload |

## Known Limitations

| Issue | Cause | Workaround |
|---|---|---|
| Tables don't scroll on mobile | Feishu message API has no native table component | Keep tables narrow (≤3 columns) |
| Card markdown lacks heading font-size hierarchy | Feishu card markdown renders `##` as bold, not as h2 | Use post mode for heading-heavy content |
| Voice recognition accuracy varies for technical terms | Whisper `base` model (~140MB); `small` model is better but ~1.2GB | Switch to `small` model in config if accuracy matters |
| Voice polish adds 5-10s latency | DeepSeek API round-trip (vs. local model) | Disable `voice.summarize.enabled` for faster turnaround |
| Short voice clips (<2s) may fail transcription | Not enough audio data for Whisper | Speak for at least 3 seconds |
| Bridge restart = cold first message | Claude `--resume` invalid after restart | `history.json` provides recent context; second message onward is warm |

## CLI Reference

```bash
fq start              # Start the bridge in background
fq stop               # Stop the bridge
fq restart            # Stop + start
fq status             # Show process, sessions, history, launchd status
fq logs               # Tail live logs
fq test "hello"       # One-shot test (no Feishu needed)
fq load               # Install launchd plist (auto-start on login)
fq unload             # Remove launchd plist
```

## License

MIT — see [LICENSE](LICENSE).

## Credits

Built as a native alternative to the third-party `lark-channel-bridge` npm package. Architecture inspired by Hermes (voice pipeline, WebSocket stability) and OpenClaw (rich formatting direction). All code written from scratch in Python — no framework, no build step, fully transparent.
