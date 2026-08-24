"""Exercise the running browser voice bridge with synthesized clinic speech.

Run while ``python -m src --llm --live-voice`` is ringing:

    python scripts/simulate_live_call.py

The script answers the call, streams real 16 kHz PCM speech at real-time speed,
asks a normal follow-up, interrupts a model response, asks another follow-up,
and verifies audio, transcripts, turn completion, and barge-in.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from collections import Counter
from typing import Any, Callable

import av
import websockets

from src.live_voice import CallSpec, LiveVoiceServer
from src.voice import synthesize_mp3_bytes

PCM_RATE = 16_000
CHUNK_MS = 20
CHUNK_BYTES = PCM_RATE * 2 * CHUNK_MS // 1000

UTTERANCES = [
    "Good morning, Sunrise Family Medicine. How can I help you?",
    "Can you tell me the patient's date of birth?",
    "Sorry to interrupt. What is your callback number?",
    "And what equipment is the patient requesting?",
]


async def synthesize_pcm(text: str) -> bytes:
    mp3 = await synthesize_mp3_bytes(text, speaker="clinic")
    if not mp3:
        raise RuntimeError(f"Speech synthesis returned no audio for: {text}")

    container = av.open(io.BytesIO(mp3), format="mp3")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=PCM_RATE)
    chunks: list[bytes] = []
    for frame in container.decode(audio=0):
        for converted in resampler.resample(frame):
            chunks.append(bytes(converted.planes[0])[: converted.samples * 2])
    for converted in resampler.resample(None):
        chunks.append(bytes(converted.planes[0])[: converted.samples * 2])
    return b"".join(chunks)


async def send_pcm(ws: Any, pcm: bytes, *, pre_silence_chunks: int = 15) -> None:
    silence = b"\0" * CHUNK_BYTES
    for _ in range(pre_silence_chunks):
        await _send_audio(ws, silence)
        await asyncio.sleep(CHUNK_MS / 1000)
    for offset in range(0, len(pcm), CHUNK_BYTES):
        chunk = pcm[offset : offset + CHUNK_BYTES]
        if len(chunk) < CHUNK_BYTES:
            chunk += b"\0" * (CHUNK_BYTES - len(chunk))
        await _send_audio(ws, chunk)
        await asyncio.sleep(CHUNK_MS / 1000)
    for _ in range(22):
        await _send_audio(ws, silence)
        await asyncio.sleep(CHUNK_MS / 1000)
    for _ in range(18):
        await _send_audio(ws, silence)
        await asyncio.sleep(CHUNK_MS / 1000)


async def _send_audio(ws: Any, pcm: bytes) -> None:
    await ws.send(
        json.dumps(
            {
                "type": "audio",
                "mime": "audio/pcm;rate=16000",
                "b64": base64.b64encode(pcm).decode("ascii"),
            }
        )
    )


async def wait_for(
    observed: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    *,
    after: int = 0,
    timeout: float = 30,
) -> tuple[int, dict[str, Any]]:
    try:
        async with asyncio.timeout(timeout):
            while True:
                for index, message in enumerate(observed[after:], start=after):
                    if message.get("type") == "call_error":
                        raise RuntimeError(message.get("error"))
                    if predicate(message):
                        return index, message
                await asyncio.sleep(0.02)
    except TimeoutError:
        print(
            json.dumps(
                {
                    "wait_after": after,
                    "recent_events": observed[max(after, len(observed) - 30) :],
                },
                indent=2,
            )
        )
        raise


async def run_simulation(ws_url: str) -> dict[str, Any]:
    pcm = await asyncio.gather(*(synthesize_pcm(text) for text in UTTERANCES))
    observed: list[dict[str, Any]] = []
    barge_boundary_confirmed = False

    async with websockets.connect(ws_url, max_size=4 * 1024 * 1024) as ws:
        incoming = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if incoming.get("type") != "incoming":
            raise RuntimeError(f"Expected a ringing call, got: {incoming}")
        await ws.send(json.dumps({"type": "answer"}))

        async def receive() -> None:
            async for raw in ws:
                message = json.loads(raw)
                observed.append(message)

        receiver = asyncio.create_task(receive())
        await wait_for(observed, lambda item: item.get("type") == "in_call")

        # Opening greeting and first model response.
        start = len(observed)
        await send_pcm(ws, pcm[0])
        greeting_index, _ = await wait_for(
            observed,
            lambda item: item.get("type") == "partial_transcript"
            and item.get("speaker") == "clinic"
            and "help you" in item.get("text", "").lower(),
            after=start,
        )
        _, _ = await wait_for(
            observed,
            lambda item: item.get("type") == "audio",
            after=start,
        )
        await wait_for(
            observed,
            lambda item: item.get("type") == "turn_complete",
            after=greeting_index,
        )

        # A normal follow-up after the model finishes.
        start = len(observed)
        await send_pcm(ws, pcm[1])
        birth_index, _ = await wait_for(
            observed,
            lambda item: item.get("type") == "partial_transcript"
            and item.get("speaker") == "clinic"
            and (
                "birth" in item.get("text", "").lower()
                or "patient" in item.get("text", "").lower()
            ),
            after=start,
        )
        await wait_for(
            observed,
            lambda item: item.get("type") == "audio",
            after=birth_index,
        )

        # Interrupt while model audio is streaming.
        await asyncio.sleep(0.15)
        start = len(observed)
        await send_pcm(ws, pcm[2], pre_silence_chunks=0)
        start_ack_index, _ = await wait_for(
            observed,
            lambda item: item.get("type") == "activity_ack"
            and item.get("activity") == "start",
            after=start,
        )
        end_index, _ = await wait_for(
            observed,
            lambda item: item.get("type") == "activity_ack"
            and item.get("activity") == "end",
            after=start,
        )
        barge_boundary_confirmed = True
        callback_index, _ = await wait_for(
            observed,
            lambda item: item.get("type") == "partial_transcript"
            and item.get("speaker") == "clinic"
            and "callback"
            in re.sub(r"[^a-z0-9]", "", item.get("text", "").lower()),
            after=start,
        )
        await wait_for(
            observed,
            lambda item: item.get("type") == "audio",
            after=max(end_index, start_ack_index, callback_index) + 1,
        )
        await wait_for(
            observed,
            lambda item: item.get("type") == "turn_complete",
            after=max(end_index, start_ack_index, callback_index) + 1,
        )

        # Confirm another follow-up still registers after the interruption.
        start = len(observed)
        await send_pcm(ws, pcm[3])
        equipment_index, _ = await wait_for(
            observed,
            lambda item: item.get("type") == "partial_transcript"
            and item.get("speaker") == "clinic"
            and "equipment"
            in re.sub(r"[^a-z0-9]", "", item.get("text", "").lower()),
            after=start,
        )
        await wait_for(
            observed,
            lambda item: item.get("type") == "audio",
            after=equipment_index,
        )
        await wait_for(
            observed,
            lambda item: item.get("type") == "turn_complete",
            after=equipment_index,
        )

        await ws.send(json.dumps({"type": "hangup"}))
        await wait_for(observed, lambda item: item.get("type") == "ended")
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)

    counts = Counter(item.get("type") for item in observed)
    clinic_lines = [
        item.get("text", "")
        for item in observed
        if item.get("type") == "transcript" and item.get("speaker") == "clinic"
    ]
    navigator_lines = [
        item.get("text", "")
        for item in observed
        if item.get("type") == "transcript" and item.get("speaker") == "navigator"
    ]
    joined_clinic = " ".join(clinic_lines).lower()
    joined_navigator = " ".join(navigator_lines).lower()
    compact_clinic = re.sub(r"[^a-z0-9]", "", joined_clinic)

    checks = {
        "model audio streamed": counts["audio"] >= 3,
        "explicit barge-in boundary confirmed": barge_boundary_confirmed,
        "speech-end acknowledgements returned": counts["activity_ack"] >= 8,
        "multiple turns completed": counts["turn_complete"] >= 3,
        "clinic turns transcribed": len(clinic_lines) >= 3,
        "navigator turns transcribed": len(navigator_lines) >= 2,
        "date-of-birth follow-up registered": (
            ("birth" in joined_clinic or "patient" in joined_clinic)
            and ("1958" in joined_navigator or "date of birth" in joined_navigator)
        ),
        "callback barge-in registered": "callback" in compact_clinic,
        "post-barge follow-up registered": (
            "equipment" in compact_clinic
            and ("wheelchair" in joined_navigator or "k0001" in joined_navigator)
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    print(
        json.dumps(
            {
                "checks": checks,
                "counts": counts,
                "clinic_transcript": clinic_lines,
                "navigator_transcript": navigator_lines,
            },
            indent=2,
            default=dict,
        )
    )
    if failed:
        raise AssertionError(f"Live-call simulation failed: {', '.join(failed)}")
    return {"checks": checks, "counts": dict(counts)}


async def main() -> None:
    server = LiveVoiceServer(http_port=8876, ws_port=8877)
    spec = CallSpec(
        role="clinic",
        title="Synthetic Sunrise Family Medicine test",
        human_hint="Automated clinic speech simulation.",
        navigator_system=(
            "You are an independent care coordinator calling Sunrise Family "
            "Medicine about Eleanor Martinez and a K0001 wheelchair written order. "
            "Her date of birth is May 15, 1958. Answer clinic questions directly. "
            "Your callback number is 312-555-0199."
        ),
        max_turns=8,
    )
    await server.start()
    call_task = asyncio.create_task(server.run_call(spec, timeout=120))
    try:
        report = await run_simulation("ws://127.0.0.1:8877")
        result = await call_task
        if result.ended_reason != "hangup":
            raise AssertionError(f"Unexpected call end: {result.ended_reason}")
        if len(result.transcript) < 5:
            raise AssertionError("Orchestrator received an incomplete transcript.")
        print(
            json.dumps(
                {
                    "standalone_server": "passed",
                    "orchestrator_transcript_turns": len(result.transcript),
                    **report,
                },
                indent=2,
            )
        )
    finally:
        if not call_task.done():
            call_task.cancel()
        await asyncio.gather(call_task, return_exceptions=True)
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
