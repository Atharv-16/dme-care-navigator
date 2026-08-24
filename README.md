# DME Care Navigator

<video src="https://github.com/Atharv-16/dme-care-navigator/releases/download/live-demo/live-navigator-2x.mp4" controls playsinline width="100%"></video>

Live demo at 2x. [Open on GitHub](https://github.com/Atharv-16/dme-care-navigator/blob/main/demo/live-navigator-2x.mp4)

Local **multi-agent** simulation of Medicare DME care coordination for Eleanor’s wheelchair case.

Every party is an in-process agent on a message bus. The default stack is **free**:
- **Ollama** for LLM dialogue
- **edge-tts** for spoken playback
- **Gemini Live native audio** for full-duplex browser calls

## Agents on the bus

| Agent ID | Role |
|---|---|
| `navigator` | Care manager — ranks, decides, messages others |
| `eleanor` | Patient |
| `clinic` | Dr. Sarah Chen / Sunrise Family Medicine reception |
| `medicare` | Part B coverage oracle |
| `supplier:<id>` | One agent per row in `data/suppliers.json` |

## Quick start

```bash
cd ~/Projects/dme-care-navigator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# One-time: start Ollama + pull model
ollama serve          # if not already running
ollama pull llama3.2

# Deterministic e2e (no LLM)
python -m src

# Real local LLM dialogue (Ollama)
python -m src --llm

# LLM + free voice (edge-tts)
python -m src --llm --voice

# Speak scripted lines only (no chat LLM)
python -m src --voice-only-scripted

# Native live voice: impersonate each party the navigator calls in Chrome/Edge.
# Requires GEMINI_API_KEY. Gemini handles audio, VAD, context, and barge-in.
python -m src --llm --live-voice
# open http://127.0.0.1:8766/live.html, then Answer when it rings
# optional role filter: LIVE_VOICE_ROLES=clinic,suppliers,patient

python -m src --dry-check
```

Audio → `output/audio/`. Case + bus logs → `output/`.

Replay the last Ollama + voice recording in a party-highlight UI (does not re-run the 30 min job):

```bash
python -m src.demo
# open http://127.0.0.1:8765  → Play recording
```

Optional LLM backends in `.env`:
- **Gemini free tier:** `LLM_PROVIDER=gemini` + `GEMINI_API_KEY` from [Google AI Studio](https://aistudio.google.com/apikey)
- Paid OpenAI: `LLM_PROVIDER=openai` + `OPENAI_API_KEY`

## Real vs mocked

| Piece | Status |
|---|---|
| Orchestration / ranking / policy | Real code |
| Local agent bus | Real in-process |
| `--llm` dialogue | Real chat via **Ollama**, **Gemini**, or OpenAI |
| `--voice` | Real **edge-tts** (no key) |
| `--live-voice` | Real **Gemini Live native audio** with automatic barge-in |
| Phones / Twilio / portals | Not used |

See `WRITEUP.md` for sequencing, cuts, and what’s next.
