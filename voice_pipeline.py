"""Voice pipeline — hot Whisper model + DeepSeek summarization.

The Whisper model is loaded once at import time and stays resident.
This eliminates the ~1s cold-start penalty on every transcription."""

import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Globals — loaded once
# ---------------------------------------------------------------------------

_whisper_model = None
_config = None


def init(config: dict):
    """Load the Whisper model. Call once at bridge startup."""
    global _whisper_model, _config
    _config = config

    from faster_whisper import WhisperModel

    vc = config["voice"]["whisper"]
    print(f"[voice] Loading faster-whisper '{vc['model']}' (device={vc['device']}) ...",
          file=sys.stderr)
    t0 = time.time()
    _whisper_model = WhisperModel(
        vc["model"],
        device=vc["device"],
        compute_type=vc["compute_type"],
    )
    print(f"[voice] Model ready ({time.time() - t0:.1f}s)", file=sys.stderr)


def is_ready() -> bool:
    return _whisper_model is not None


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

def transcribe(audio_path: str) -> Tuple[str, str, float]:
    """Transcribe an OGG file. Returns (text, language, duration_seconds)."""
    assert _whisper_model, "voice_pipeline.init() must be called first"

    vc = _config["voice"]["whisper"]
    language = vc.get("language") or None

    t0 = time.time()
    segments, info = _whisper_model.transcribe(
        audio_path,
        language=language,
        beam_size=5,
        vad_filter=True,
    )
    text = " ".join(seg.text.strip() for seg in segments)

    elapsed = time.time() - t0
    print(f"[voice] Transcribed: lang={info.language} dur={info.duration:.1f}s "
          f"text={len(text)}chars ({elapsed:.1f}s)",
          file=sys.stderr)
    return text, info.language, info.duration


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

def summarize(raw_text: str) -> str:
    """Clean the transcript with DeepSeek."""
    sc = _config["voice"]["summarize"]
    print(f"[voice] Summarizing ({len(raw_text)} chars) with {sc['model']} ...",
          file=sys.stderr)
    t0 = time.time()

    resp = requests.post(
        f"{sc['base_url'].rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {sc['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": sc["model"],
            "messages": [
                {"role": "system", "content": sc["prompt"]},
                {"role": "user", "content": raw_text},
            ],
            "temperature": 0.3,
            "max_tokens": 2048,
        },
        timeout=60,
    )
    resp.raise_for_status()
    cleaned = resp.json()["choices"][0]["message"]["content"].strip()

    print(f"[voice] Summarized: {len(raw_text)} → {len(cleaned)} chars "
          f"({time.time() - t0:.1f}s)",
          file=sys.stderr)
    return cleaned


# ---------------------------------------------------------------------------
# Main entry: transcribe + summarize → cleaned text
# ---------------------------------------------------------------------------

def process(audio_path: str) -> Optional[str]:
    """Full voice pipeline: transcribe + summarize.
    Returns the cleaned text, or None if empty."""
    if not os.path.exists(audio_path):
        print(f"[voice] ERROR: file not found: {audio_path}", file=sys.stderr)
        return None

    fname = os.path.basename(audio_path)
    print(f"[voice] Processing: {fname}", file=sys.stderr)

    try:
        raw_text, lang, duration = transcribe(audio_path)
        if not raw_text.strip():
            print(f"[voice] WARNING: empty transcript", file=sys.stderr)
            return None

        if _config["voice"]["summarize"]["enabled"] and len(raw_text) >= 10:
            return summarize(raw_text)
        return raw_text

    except Exception as e:
        print(f"[voice] ERROR: {e}", file=sys.stderr)
        return None
