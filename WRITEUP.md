# DME Care Navigator — Writeup

## Sequencing — what I built and how I decided

The coordination problem is multi-party: patient, PCP clinic, DME suppliers, and Medicare rules. Instead of wiring phones first, I built a **local multi-agent world** where every party is an in-process agent on a message bus. That makes the system testable end-to-end without Twilio, and still shows the automated care-advocate job.

**Parties**
- `navigator` — manager brain (rank, decide, fan-out, merge, escalate)
- `eleanor` — patient
- `clinic` — Dr. Chen reception (two-touch written order)
- `medicare` — Part B coverage oracle
- `supplier:*` — one agent per directory row (your Chicago list)

**Build order**
1. Sparse supplier directory + case memory  
2. Deterministic ranking + policy gate  
3. Local bus + party agents  
4. Parallel navigator fan-out (suppliers ∥ clinic)  
5. Medicare check + patient notify → complete / escalate  

## Technology & architecture

Python, asyncio, Pydantic, Rich.
**LLM:** Ollama (`llama3.2`) local, or Gemini Flash free tier (`LLM_PROVIDER=gemini`).  
**Voice:** `edge-tts` for recorded playback. Live browser calls use one
persistent Gemini Live native-audio session for speech, dialogue, turn
detection, context, and automatic barge-in.

```
navigator ──ask──► supplier agents (parallel in simulate / sequential in LLM)
    │────────ask──► clinic
    │────────ask──► medicare
    └────────ask──► eleanor
         ▲
         └── LocalBus (in-process envelopes)
```

**AI in:** free-form dialogue between local agents (`--llm`), optional extraction.  
**AI out:** ranking, sequencing, escalation rules, Medicare blocker checklist, coinsurance fact (~20%).


## Cut list

| Cut | Why |
|---|---|
| Twilio / STT / TTS / Realtime voice | Channel deferred; multi-agent coordination is the product slice |
| Real portals / fax / claims filing | Mocked as clinic + medicare agents |
| Auth / DB / UI | Out of scope |
| Learned supplier ranking | Heuristic scores are auditable for a 3-hour slice |

## What’s next

**+1 day:** OpenAI Realtime or STT+TTS behind the same agent interface (`availability_check` stays stable).  
**+2 weeks:** callback SLAs, human-in-the-loop resume, transcript eval harness, MBI redaction in bus logs.
