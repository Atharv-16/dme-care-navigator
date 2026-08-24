# DME Care Navigator: Writeup

## Sequencing: what I built and how I decided

The coordination problem is multi-party: patient, PCP clinic, DME suppliers, and Medicare rules. Instead of wiring phones first, I built a **local multi-agent world** where every party is an in-process agent on a message bus. That makes the system testable end-to-end without Twilio, and still shows the automated care-advocate job.

**Parties**
- `navigator`: manager brain (rank, decide, fan-out, merge, escalate)
- `eleanor`: patient
- `clinic`: Dr. Chen reception (two-touch written order)
- `medicare`: Part B coverage oracle
- `supplier:*`: one agent per directory row (Chicago list)

**Build order**
1. Sparse supplier directory + case memory
2. Deterministic ranking + policy gate
3. Local bus + party agents
4. Parallel navigator fan-out (suppliers ∥ clinic)
5. Medicare check + patient notify → complete / escalate
6. Gemini Live browser calls with durable post-call context

## Technology & architecture

Python, asyncio, Pydantic, Rich.
**LLM:** Gemini for chat JSON and post-call analysis (`LLM_PROVIDER=gemini`).
**Voice:** Gemini Live native audio in the browser. You impersonate each party; Gemini is the navigator. After hangup, analysis patches `output/{case}.context.json` and the analyzer's `next_call` plan routes the next ring.

```
navigator ──ask──► supplier agents (sequential live calls)
    │────────ask──► clinic
    │────────ask──► medicare
    └────────ask──► eleanor
         ▲
         └── LocalBus (in-process envelopes)
```

**AI in:** free-form live dialogue, then structured extraction.
**AI out:** ranking, sequencing, escalation rules, Medicare blocker checklist, coinsurance fact (~20%).

## Cut list

| Cut | Why |
|---|---|
| Twilio / real phone numbers | Browser Gemini Live is the channel for this slice |
| Real portals / fax / claims filing | Mocked as clinic + medicare agents |
| Auth / DB | Out of scope |
| Learned supplier ranking | Heuristic scores are auditable |

## What's next

**+1 day:** transcript eval harness, MBI redaction in bus logs.
**+2 weeks:** callback SLAs, human-in-the-loop resume, tighter clinic/supplier memory evals.
