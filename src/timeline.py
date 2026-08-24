from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
RECORDING = OUTPUT / "recordings" / "current"
DATA = ROOT / "data"


def _recording_paths() -> tuple[Path, Path]:
    rec_bus = RECORDING / "bus.json"
    rec_audio = RECORDING / "audio"
    if rec_bus.exists() and rec_audio.exists():
        return rec_bus, rec_audio
    return OUTPUT / "eleanor-martinez-wheelchair.bus.json", OUTPUT / "audio"


def audio_dir() -> Path:
    return _recording_paths()[1]


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name)[:60]


def _party_id(speaker: str, counterpart_of: str) -> str:
    s = (speaker or "").lower()
    if s.startswith("supplier"):
        return s
    if s == "counterpart":
        return "navigator"
    if s in {"navigator", "eleanor", "clinic", "medicare"}:
        return s
    return "navigator"


def _label(party: str, supplier_name: str | None) -> str:
    return {
        "navigator": "Navigator",
        "eleanor": "Eleanor",
        "clinic": "Clinic",
        "medicare": "Medicare",
        "supplier": supplier_name or "Supplier",
    }.get(party, party)


def _conversation_name(agent_id: str, supplier_names: dict[str, str]) -> str:
    if agent_id.startswith("supplier:"):
        sid = agent_id.split(":", 1)[1]
        name = supplier_names.get(sid, sid)
        return f"Local talk — {name}"
    return agent_id


def _audio_url(conversation_name: str, index: int, speaker: str) -> str | None:
    stem = f"{_safe(conversation_name)}_{index:02d}_{speaker.replace(':', '_')}.mp3"
    path = AUDIO / stem
    if path.exists():
        return f"/audio/{stem}"
    return None


def build_timeline() -> dict:
    bus_path, audio_dir_path = _recording_paths()
    global BUS_PATH, AUDIO
    BUS_PATH, AUDIO = bus_path, audio_dir_path
    suppliers = json.loads((DATA / "suppliers.json").read_text())
    names = {s["id"]: s["name"] for s in suppliers}
    if not BUS_PATH.exists():
        return {"error": "No recording yet. Run: python -m src --llm --voice", "scenes": []}

    bus = json.loads(BUS_PATH.read_text())
    scenes: list[dict] = []
    scene_i = 0
    clinic_i = 0
    for env in bus:
        if env.get("dir") != "in":
            continue
        body = env.get("body") or {}
        transcript = body.get("transcript") or []
        if not transcript:
            continue
        agent = env.get("from") or ""
        if agent == "clinic":
            conv = f"clinic_{clinic_i}"
            clinic_i += 1
        else:
            conv = _conversation_name(agent, names)
        supplier_name = None
        supplier_id = None
        if agent.startswith("supplier:"):
            supplier_id = agent.split(":", 1)[1]
            supplier_name = names.get(supplier_id, agent)
        title = {
            "clinic": "Chase written order — Sunrise Family Medicine",
            "medicare": "Medicare Part B coverage check",
            "eleanor": "Patient update — Eleanor Martinez",
        }.get(agent, conv.replace("Local talk — ", "Supplier call — "))

        turns = []
        for i, turn in enumerate(transcript):
            speaker = str(turn.get("speaker", "?"))
            party = _party_id(speaker, agent)
            audio = _audio_url(conv, i, speaker)
            turns.append(
                {
                    "speaker": speaker,
                    "party": party,
                    "on_call": supplier_id,
                    "label": _label("supplier" if party.startswith("supplier") else party, supplier_name),
                    "text": str(turn.get("text", "")).strip(),
                    "audio": audio,
                }
            )
        scenes.append(
            {
                "id": scene_i,
                "agent": agent,
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "title": title,
                "outcome": body.get("outcome") or body.get("order_status") or body.get("summary"),
                "summary": body.get("summary"),
                "turns": turns,
            }
        )
        scene_i += 1

    recorded = sum(1 for s in scenes for t in s["turns"] if t["audio"])
    total = sum(len(s["turns"]) for s in scenes)
    contacted = {s["supplier_id"] for s in scenes if s.get("supplier_id")}
    outcomes = {
        s["supplier_id"]: s.get("outcome")
        for s in scenes
        if s.get("supplier_id")
    }
    directory = [
        {
            "id": s["id"],
            "name": s["name"],
            "phone": s.get("phone", ""),
            "contacted": s["id"] in contacted,
            "outcome": outcomes.get(s["id"]),
        }
        for s in suppliers
    ]
    rec_meta = {}
    meta_path = RECORDING / "meta.json"
    if meta_path.exists():
        rec_meta = json.loads(meta_path.read_text())
    dialogue = rec_meta.get("dialogue") or "llm"
    return {
        "case": "Eleanor Martinez — K0001 wheelchair",
        "source": str(BUS_PATH.name),
        "recording": {
            "dialogue": f"{dialogue} (real generated talk, not the scripted lines)",
            "voice": "saved mp3s from python -m src --llm --voice",
            "note": "This page replays those files. It does not call the LLM again.",
        },
        "scenes": scenes,
        "suppliers": directory,
        "called": [s for s in directory if s["contacted"]],
        "stats": {"turns": total, "with_audio": recorded, "tts_fallback": total - recorded},
    }
