# Native Feishu Bridge

[中文文档](./README.zh.md)

A transparent Python bridge connecting [Feishu/Lark](https://www.feishu.cn) to [Claude Code](https://claude.ai/claude-code). Send text or voice messages in Feishu, get Claude-powered replies — with real-time progress, rich formatting, and built-in speech-to-text. ~1,150 lines of readable source — no framework, no compiled code.

## Features

- **Text & Voice** — Send text or voice messages; voice is auto-transcribed (Whisper) and polished (DeepSeek)
- **Real-time Progress** — Interactive card shows Claude thinking → tool calls → replying (🧠→🔧→✍️)
- **Structured Formatting** — Replies with tables or multiple headings auto-render as rich cards (blue header, section dividers, footer)
- **Session Continuity** — Conversation history persists across bridge restarts (via `history.json` injection)
- **Preemptive Replies** — New message interrupts old Claude run (no queue buildup)
- **Debounced Batching** — Rapid-fire messages are merged (800ms window)

## Prerequisites

| Requirement | How to Get It |
|---|---|
| **Python 3.9+** | `python3 --version` (macOS comes with it) |
| **Claude Code CLI** | `npm install -g @anthropic-ai/claude-code` |
| **Feishu App** (free) | Create at [open.feishu.cn](https://open.feishu.cn) — see below |
| **DeepSeek API Key** (optional) | [platform.deepseek.com](https://platform.deepseek.com) — for voice polishing |
| **macOS / Linux** | Windows works but launchd is macOS-only |

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/ssrnone954-lang/Native-Feishu-Bridge.git
cd Native-Feishu-Bridge
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

See [`config.yaml.example`](config.yaml.example) for every option with Chinese comments.

Key sections:

| Section | Required | Purpose |
|---|---|---|
| `feishu` | **Yes** | App credentials from Feishu Open Platform |
| `claude` | **Yes** | Path to Claude CLI binary |
| `voice` | No | Speech-to-text (Whisper) + polish (DeepSeek) |
| `messages` | No | Debounce timing, reply format preference |
| `paths` | No | Where to store sessions, logs, media |

## Data Directories

| Path | Default | Purpose |
|---|---|---|
| `paths.sessions_dir` | `~/native-bridge-sessions/` | Session IDs + conversation history |
| `paths.logs_dir` | `~/native-bridge-logs/` | Runtime logs |
| `messages.download_dir` | `~/native-bridge-media/` | Voice OGG files (temporary) |
| `paths.pid_file` | `/tmp/native-feishu-bridge.pid` | Process PID file |

These files are **never committed to Git** (excluded by `.gitignore`):

| File | Purpose |
|---|---|
| `config.yaml` | Contains your real credentials |
| `sessions/*.json` | Contains chat IDs and conversation content |
| `logs/*.log` | Runtime logs |

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

## FAQ & Known Issues

### Common Questions

**Q: Bot doesn't reply to my messages?**
A: Run `fq status` first. If stopped, `fq start`. If running, check `fq logs` for errors.

**Q: Voice transcription failed?**
A: Check: 1) Did you speak for at least 2 seconds? 2) Can you reach DeepSeek API? 3) To disable voice: set `voice.enabled: false` in config.yaml.

**Q: How do I update?**
A: `git pull` then `./bin/fq restart`.

**Q: Can I run this on multiple machines?**
A: No. Feishu allows only one WebSocket connection per App. Create separate apps for each machine.

**Q: Does it work with Lark (international)?**
A: Yes. Set `feishu.domain: "lark"` in config.yaml.

### Known Limitations

| Issue | Cause | Workaround |
|---|---|---|
| Tables don't scroll on mobile | Feishu message API has no native table component | Keep tables narrow (≤3 columns) |
| Card markdown lacks heading font-size hierarchy | Feishu card markdown renders `##` as bold, not as h2 | Use post mode for heading-heavy content |
| Voice recognition accuracy varies for technical terms | Whisper `base` model (~140MB); `small` is better but ~1.2GB | Switch to `small` model for better accuracy |
| Voice polish adds 5-10s latency | DeepSeek API round-trip | Disable `voice.summarize.enabled` |
| Short voice clips (<2s) may fail | Not enough audio data | Speak for at least 3 seconds |
| Bridge restart = cold first message | Claude `--resume` invalid after restart | `history.json` provides context; second message onward is warm |

## Inspiration

This project draws ideas from three excellent projects:

- [lark-channel-bridge](https://github.com/zarazhangrui/feishu-claude-code-bridge) — WebSocket connection model, message queue, debounce patterns
- **Hermes** — Voice pipeline architecture (hot Whisper model + LLM polish, both in-process)
- **OpenClaw** — Rich card formatting direction (mixed content in a single card)

## License

MIT — see [LICENSE](LICENSE).

---

Built with [Claude Code](https://claude.ai/claude-code).
