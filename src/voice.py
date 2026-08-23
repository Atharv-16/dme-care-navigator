from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path

import edge_tts
from rich.console import Console

console = Console()
ROOT = Path(__file__).resolve().parents[1]


def audio_dir() -> Path:
    return Path(os.getenv("DME_AUDIO_DIR", str(ROOT / "output" / "audio")))

# Free Microsoft Edge neural voices (no API key)
VOICE_BY_SPEAKER = {
    "navigator": "en-US-GuyNeural",
    "eleanor": "en-US-JennyNeural",
    "clinic": "en-US-AriaNeural",
    "medicare": "en-US-ChristopherNeural",
}
DEFAULT_SUPPLIER_VOICE = "en-US-BrianNeural"
FALLBACK_VOICE = "en-US-EricNeural"


def _voice_for(speaker: str) -> str:
    s = (speaker or "").lower()
    if s in VOICE_BY_SPEAKER:
        return VOICE_BY_SPEAKER[s]
    if s.startswith("supplier"):
        return DEFAULT_SUPPLIER_VOICE
    return FALLBACK_VOICE


def _clean(text: str) -> str:
    text = re.sub(r"\[(?:END|END_CALL|NO_ANSWER|HANGUP)\]", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _play_audio(path: Path) -> None:
    players = [
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
        ["mpv", "--no-terminal", "--really-quiet", str(path)],
        ["mpg123", "-q", str(path)],
        ["paplay", str(path)],
        ["aplay", "-q", str(path)],
    ]
    for cmd in players:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            subprocess.run(cmd, check=False, capture_output=True)
            return
        except Exception:  # noqa: BLE001
            continue
    console.print(f"[dim]Saved audio (no player found): {path}[/dim]")


async def speak_text(
    text: str,
    *,
    speaker: str,
    stem: str,
    play: bool = True,
) -> Path | None:
    cleaned = _clean(text)
    if not cleaned:
        return None

    AUDIO_DIR = audio_dir()
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out = AUDIO_DIR / f"{stem}.mp3"
    voice = _voice_for(speaker)

    communicate = edge_tts.Communicate(cleaned, voice)
    try:
        await communicate.save(str(out))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]TTS skipped for {speaker}: {exc}[/yellow]")
        return None

    console.print(
        f"[blue]🔊[/blue] ({voice}) {speaker}: "
        f"{cleaned[:80]}{'…' if len(cleaned) > 80 else ''}"
    )
    if play:
        await asyncio.to_thread(_play_audio, out)
    return out


async def speak_transcript(
    transcript: list[dict[str, str]],
    *,
    conversation_name: str,
    play: bool = True,
) -> list[Path]:
    """Speak each turn via edge-tts (free)."""
    paths: list[Path] = []
    safe = re.sub(r"[^\w\-]+", "_", conversation_name)[:60]
    for i, turn in enumerate(transcript):
        speaker = str(turn.get("speaker", "unknown"))
        text = str(turn.get("text", ""))
        path = await speak_text(
            text,
            speaker=speaker,
            stem=f"{safe}_{i:02d}_{speaker.replace(':', '_')}",
            play=play,
        )
        if path:
            paths.append(path)
    return paths
