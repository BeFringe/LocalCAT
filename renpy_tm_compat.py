"""Strict compatibility helpers for speaker-wrapped Ren'Py TM records."""

from __future__ import annotations

import json
import re


_SAFE_SPEAKER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def build_dialogue_alias(speaker: str, text: str) -> str | None:
    """Render the one unambiguous Ren'Py dialogue key supported by the MVP."""

    clean_speaker = speaker.strip()
    if not _SAFE_SPEAKER.fullmatch(clean_speaker) or not text:
        return None
    return f"{clean_speaker} {json.dumps(text, ensure_ascii=False)}"


def unwrap_dialogue_target(rendered: str, speaker: str) -> str | None:
    """Return a target payload only when it is wrapped by the same speaker."""

    clean_speaker = speaker.strip()
    if not _SAFE_SPEAKER.fullmatch(clean_speaker):
        return None
    prefix = f"{clean_speaker} "
    if not rendered.startswith(prefix):
        return None
    try:
        payload = json.loads(rendered[len(prefix) :])
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, str) and payload.strip() else None
