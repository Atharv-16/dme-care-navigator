"""Native, full-duplex Gemini Live voice bridge.

The browser streams 16 kHz PCM microphone audio to this server. One persistent
Gemini Live session handles speech understanding, dialogue, voice generation,
turn detection, and barge-in. Gemini's 24 kHz PCM output streams back to the
browser for immediate playback.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console

console = Console()
ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
load_dotenv(ROOT / ".env")


@dataclass
class CallSpec:
    role: str
    title: str
    human_hint: str
    navigator_system: str
    opener: str = ""
    navigator_speaks_first: bool = False
    first_navigator_line: str = ""
    max_turns: int = 8


@dataclass
class CallResult:
    transcript: list[dict[str, str]] = field(default_factory=list)
    ended_reason: str = "hangup"


def _append_delta(current: str, delta: str) -> str:
    """Join streaming transcription chunks without duplicating cumulative text."""
    raw_delta = delta or ""
    stripped_delta = raw_delta.strip()
    if not stripped_delta:
        return current
    if not current:
        return stripped_delta
    if stripped_delta.startswith(current):
        return stripped_delta
    if current.endswith(stripped_delta):
        return current
    # Gemini emits true text deltas, including any meaningful leading space.
    # Adding our own separator corrupts fragments such as "voi" + "ce".
    return f"{current}{raw_delta}".strip()


def _english_transcript(text: str) -> str:
    """Remove non-English script noise from displayed and stored transcripts."""
    cleaned = re.sub(r"[^\x00-\x7F]+", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned.lower().strip(" <>[]().,!?:;-'\"") in {"", "noise"}:
        return ""
    if len(re.sub(r"[^A-Za-z0-9]", "", cleaned)) < 2:
        return ""
    return cleaned


class WebRtcTurnDetector:
    """Noise-robust 16 kHz speech boundaries using WebRTC VAD.

    Mode 3 is WebRTC's most aggressive rejection setting. A short voting window
    prevents one noisy frame from opening a turn, while pre-roll preserves the
    beginning of real speech. Sustained non-speech closes the turn.
    """

    sample_rate = 16_000
    frame_ms = 20
    frame_bytes = sample_rate * 2 * frame_ms // 1000

    def __init__(self) -> None:
        import webrtcvad

        mode = max(0, min(3, int(os.getenv("WEBRTC_VAD_MODE", "3"))))
        self._vad = webrtcvad.Vad(mode)
        self._min_rms = max(0, int(os.getenv("VAD_MIN_RMS", "650")))
        start_frames = max(5, int(os.getenv("VAD_START_FRAMES", "14")))
        self._start_vote_target = min(
            start_frames,
            max(1, int(os.getenv("VAD_START_VOTES", "10"))),
        )
        self._buffer = bytearray()
        self._pre_roll: deque[bytes] = deque(maxlen=15)
        self._start_votes: deque[bool] = deque(maxlen=start_frames)
        self.active = False
        self._quiet_frames = 0
        self._active_frames = 0

    def feed(self, pcm: bytes) -> list[tuple[str, bytes | None]]:
        self._buffer.extend(pcm)
        events: list[tuple[str, bytes | None]] = []
        while len(self._buffer) >= self.frame_bytes:
            frame = bytes(self._buffer[: self.frame_bytes])
            del self._buffer[: self.frame_bytes]
            samples = memoryview(frame).cast("h")
            rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
            voiced = (
                rms >= self._min_rms
                and self._vad.is_speech(frame, self.sample_rate)
            )

            if not self.active:
                self._pre_roll.append(frame)
                self._start_votes.append(voiced)
                if (
                    len(self._start_votes) == self._start_votes.maxlen
                    and sum(self._start_votes) >= self._start_vote_target
                ):
                    self.active = True
                    self._quiet_frames = 0
                    self._active_frames = 0
                    events.append(("start", None))
                    events.extend(("audio", chunk) for chunk in self._pre_roll)
                    self._pre_roll.clear()
                    self._start_votes.clear()
                continue

            events.append(("audio", frame))
            self._active_frames += 1
            self._quiet_frames = 0 if voiced else self._quiet_frames + 1

            # 360 ms of classified non-speech ends a turn. The maximum prevents
            # pathological background audio from holding a turn forever.
            if self._quiet_frames >= 18 or self._active_frames >= 2250:
                events.append(("end", None))
                self.active = False
                self._quiet_frames = 0
                self._active_frames = 0
                self._pre_roll.clear()
                self._start_votes.clear()
        return events


class LiveVoiceServer:
    def __init__(
        self,
        *,
        http_host: str = "127.0.0.1",
        http_port: int = 8766,
        ws_port: int = 8767,
    ):
        self.http_host = http_host
        self.http_port = http_port
        self.ws_port = ws_port
        self._clients: set[Any] = set()
        self._incoming: CallSpec | None = None
        self._active_ws: Any = None
        self._session_task: asyncio.Task | None = None
        self._browser_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._result_fut: asyncio.Future[CallResult] | None = None
        self._call_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._analysis_state: dict[str, Any] | None = None
        self._ws_server: Any = None
        self._httpd: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.http_host}:{self.http_port}/live.html"

    async def start(self) -> None:
        self._start_http()
        import websockets

        async def handler(websocket: Any, path: str | None = None) -> None:  # noqa: ARG001
            await self._ws_handler(websocket)

        self._ws_server = await websockets.serve(
            handler,
            self.http_host,
            self.ws_port,
            max_size=2 * 1024 * 1024,
            reuse_address=True,
        )
        console.print(
            f"[green]Gemini Live[/green] earpiece at {self.url} "
            f"(ws {self.http_host}:{self.ws_port})"
        )

    def _start_http(self) -> None:
        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(*args, directory=str(UI), **kwargs)

            def do_POST(self) -> None:
                if self.path != "/api/restart":
                    self.send_error(404)
                    return
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self.send_error(403)
                    return

                body = b'{"status":"restarting"}'
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

                def restart() -> None:
                    os.chdir(ROOT)
                    os.execv(
                        sys.executable,
                        [sys.executable, "-m", "src", *sys.argv[1:]],
                    )

                threading.Timer(0.25, restart).start()

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def end_headers(self) -> None:
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

        class LiveHTTPServer(ThreadingHTTPServer):
            allow_reuse_address = True

        self._httpd = LiveHTTPServer((self.http_host, self.http_port), Handler)
        self._http_thread = threading.Thread(
            target=self._httpd.serve_forever,
            daemon=True,
        )
        self._http_thread.start()

    async def stop(self) -> None:
        if self._session_task and not self._session_task.done():
            self._session_task.cancel()
        if self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        if self._httpd:
            self._httpd.shutdown()

    async def run_call(self, spec: CallSpec, *, timeout: float = 3600.0) -> CallResult:
        async with self._call_lock:
            loop = asyncio.get_running_loop()
            self._incoming = spec
            self._result_fut = loop.create_future()
            self._browser_events = asyncio.Queue()
            self._session_task = None
            self._analysis_state = None
            await self._broadcast(self._incoming_msg(spec))
            console.print(
                f"[bold green]LIVE CALL[/bold green] {spec.title}, answer at {self.url}"
            )

            async def keep_ringing() -> None:
                while (
                    self._incoming is spec
                    and not self._result_fut.done()
                    and not (self._session_task and not self._session_task.done())
                ):
                    await asyncio.sleep(1)
                    if (
                        self._incoming is spec
                        and not self._result_fut.done()
                        and not (self._session_task and not self._session_task.done())
                    ):
                        await self._broadcast(self._incoming_msg(spec))

            ring_task = asyncio.create_task(keep_ringing())
            try:
                return await asyncio.wait_for(self._result_fut, timeout=timeout)
            except asyncio.TimeoutError:
                if self._session_task and not self._session_task.done():
                    self._session_task.cancel()
                result = CallResult(ended_reason="timeout")
                await self._finish(result)
                return result
            finally:
                ring_task.cancel()
                self._incoming = None

    def _incoming_msg(self, spec: CallSpec) -> dict[str, Any]:
        return {
            "type": "incoming",
            "role": spec.role,
            "title": spec.title,
            "hint": spec.human_hint,
        }

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload)
        dead = []
        async with self._send_lock:
            for ws in list(self._clients):
                try:
                    await ws.send(raw)
                except Exception:  # noqa: BLE001
                    dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def notify_analysis_status(self, payload: dict[str, Any]) -> None:
        self._analysis_state = {"type": "analysis_status", **payload}
        await self._broadcast(self._analysis_state)

    async def _send(self, ws: Any, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await ws.send(json.dumps(payload))

    async def _ws_handler(self, websocket: Any) -> None:
        self._clients.add(websocket)
        try:
            if self._incoming:
                await self._send(websocket, self._incoming_msg(self._incoming))
            else:
                await self._send(
                    websocket,
                    self._analysis_state
                    or {
                        "type": "idle",
                        "hint": "Waiting for the navigator to place a call.",
                    },
                )
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._on_client(websocket, message)
        finally:
            self._clients.discard(websocket)
            if websocket is self._active_ws:
                await self._browser_events.put({"type": "hangup"})

    async def _on_client(self, websocket: Any, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "hello":
            payload = (
                self._incoming_msg(self._incoming)
                if self._incoming
                else self._analysis_state
                or {
                    "type": "idle",
                    "hint": "Keep this tab open. The navigator will ring here.",
                }
            )
            await self._send(websocket, payload)
            return

        if kind == "answer":
            if not self._incoming:
                await self._send(websocket, {"type": "idle", "hint": "No incoming call."})
                return
            if self._session_task and not self._session_task.done():
                return
            self._active_ws = websocket
            self._session_task = asyncio.create_task(
                self._session(websocket, self._incoming)
            )
            return

        if websocket is self._active_ws and kind in {
            "audio",
            "activity_start",
            "activity_end",
            "user_text",
            "hangup",
        }:
            await self._browser_events.put(message)

    async def _finish(self, result: CallResult) -> None:
        if self._result_fut and not self._result_fut.done():
            self._result_fut.set_result(result)
        if self._active_ws:
            try:
                await self._send(
                    self._active_ws,
                    {"type": "ended", "reason": result.ended_reason},
                )
            except Exception:  # noqa: BLE001
                pass
        self._active_ws = None

    def _live_config(self, spec: CallSpec) -> dict[str, Any]:
        instruction = (
            f"{spec.navigator_system}\n\n"
            "You are in a live full-duplex phone call. Speak naturally and briefly. "
            "Never emit JSON, markdown, labels, stage directions, or control tokens. "
            "The other speaker may interrupt you. Stop immediately when interrupted, "
            "listen to the new statement, and continue from the same conversation. "
            "Treat the call as English-only. Ignore speech that is not English and "
            "respond only in English. Do not repeat your introduction. "
            "Ask one question at a time."
        )
        return {
            "response_modalities": ["AUDIO"],
            "system_instruction": instruction,
            "speech_config": {
                "language_code": "en-US",
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": os.getenv("GEMINI_LIVE_VOICE", "Aoede")
                    }
                }
            },
            "input_audio_transcription": {
                "language_codes": ["en-US"],
                "custom_vocabulary": [
                    "Original Medicare",
                    "Medicare Part B",
                    "durable medical equipment",
                    "standard manual wheelchair",
                    "K0001",
                    "written order",
                    "Eleanor Martinez",
                    "Sunrise Family Medicine",
                ],
            },
            "output_audio_transcription": {},
            "realtime_input_config": {
                "automatic_activity_detection": {
                    "disabled": True,
                },
                "activity_handling": "START_OF_ACTIVITY_INTERRUPTS",
            },
        }

    async def _session(self, ws: Any, spec: CallSpec) -> None:
        transcript: list[dict[str, str]] = []
        reason = "hangup"
        stop_event = asyncio.Event()
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        model = os.getenv(
            "GEMINI_NATIVE_AUDIO_MODEL",
            "gemini-2.5-flash-native-audio-preview-12-2025",
        )

        if not api_key:
            await self._send(
                ws,
                {"type": "call_error", "error": "GEMINI_API_KEY is required."},
            )
            await self._finish(CallResult(ended_reason="error:missing_api_key"))
            return

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            await self._send(
                ws,
                {
                    "type": "in_call",
                    "title": spec.title,
                    "hint": spec.human_hint,
                    "role": spec.role,
                    "model": model,
                },
            )

            async with client.aio.live.connect(
                model=model,
                config=self._live_config(spec),
            ) as live:

                async def browser_to_gemini() -> None:
                    nonlocal reason
                    detector = WebRtcTurnDetector()
                    while not stop_event.is_set():
                        event = await self._browser_events.get()
                        kind = event.get("type")
                        if kind == "hangup":
                            reason = "hangup"
                            stop_event.set()
                            return
                        if kind == "audio":
                            try:
                                pcm = base64.b64decode(event.get("b64") or "")
                            except Exception:  # noqa: BLE001
                                continue
                            for vad_event, frame in detector.feed(pcm):
                                if vad_event == "start":
                                    await live.send_realtime_input(
                                        activity_start=types.ActivityStart()
                                    )
                                    await self._send(
                                        ws,
                                        {
                                            "type": "vad_state",
                                            "state": "speaking",
                                            "engine": "webrtc",
                                        },
                                    )
                                    await self._send(
                                        ws,
                                        {"type": "activity_ack", "activity": "start"},
                                    )
                                elif vad_event == "audio" and frame:
                                    await live.send_realtime_input(
                                        audio=types.Blob(
                                            data=frame,
                                            mime_type="audio/pcm;rate=16000",
                                        )
                                    )
                                elif vad_event == "end":
                                    await live.send_realtime_input(
                                        activity_end=types.ActivityEnd()
                                    )
                                    await self._send(
                                        ws,
                                        {
                                            "type": "vad_state",
                                            "state": "ended",
                                            "engine": "webrtc",
                                        },
                                    )
                                    await self._send(
                                        ws,
                                        {"type": "activity_ack", "activity": "end"},
                                    )
                        elif kind == "user_text":
                            text = str(event.get("text") or "").strip()
                            if text:
                                await live.send_client_content(
                                    turns={
                                        "role": "user",
                                        "parts": [{"text": text}],
                                    },
                                    turn_complete=True,
                                )

                async def gemini_to_browser() -> None:
                    nonlocal reason
                    input_text = ""
                    output_text = ""
                    turns = 0
                    while not stop_event.is_set():
                        async for response in live.receive():
                            content = getattr(response, "server_content", None)
                            if not content:
                                continue

                            if getattr(content, "interrupted", False):
                                await self._send(ws, {"type": "interrupted"})
                                if output_text.strip():
                                    spoken = f"{output_text.strip()} [interrupted]"
                                    transcript.append(
                                        {"speaker": "navigator", "text": spoken}
                                    )
                                    await self._send(
                                        ws,
                                        {
                                            "type": "transcript",
                                            "speaker": "navigator",
                                            "text": spoken,
                                        },
                                    )
                                output_text = ""

                            input_transcription = getattr(
                                content,
                                "input_transcription",
                                None,
                            )
                            if input_transcription:
                                input_text = _append_delta(
                                    input_text,
                                    getattr(input_transcription, "text", ""),
                                )
                                clean_input = _english_transcript(input_text)
                                if clean_input:
                                    await self._send(
                                        ws,
                                        {
                                            "type": "partial_transcript",
                                            "speaker": spec.role,
                                            "text": clean_input,
                                        },
                                    )

                            output_transcription = getattr(
                                content,
                                "output_transcription",
                                None,
                            )
                            if output_transcription:
                                output_text = _append_delta(
                                    output_text,
                                    getattr(output_transcription, "text", ""),
                                )
                                await self._send(
                                    ws,
                                    {
                                        "type": "partial_transcript",
                                        "speaker": "navigator",
                                        "text": output_text,
                                    },
                                )

                            model_turn = getattr(content, "model_turn", None)
                            for part in getattr(model_turn, "parts", []) or []:
                                inline = getattr(part, "inline_data", None)
                                audio = getattr(inline, "data", None)
                                if audio:
                                    await self._send(
                                        ws,
                                        {
                                            "type": "audio",
                                            "mime": "audio/pcm;rate=24000",
                                            "b64": base64.b64encode(audio).decode("ascii"),
                                        },
                                    )

                            if getattr(content, "turn_complete", False):
                                clean_input = _english_transcript(input_text)
                                if clean_input:
                                    transcript.append(
                                        {
                                            "speaker": spec.role,
                                            "text": clean_input,
                                        }
                                    )
                                    await self._send(
                                        ws,
                                        {
                                            "type": "transcript",
                                            "speaker": spec.role,
                                            "text": clean_input,
                                        },
                                    )
                                if output_text.strip():
                                    transcript.append(
                                        {
                                            "speaker": "navigator",
                                            "text": output_text.strip(),
                                        }
                                    )
                                    await self._send(
                                        ws,
                                        {
                                            "type": "transcript",
                                            "speaker": "navigator",
                                            "text": output_text.strip(),
                                        },
                                    )
                                input_text = ""
                                output_text = ""
                                turns += 1
                                await self._send(ws, {"type": "turn_complete"})
                                # Native VAD can split one spoken sentence into
                                # multiple turns around natural pauses. Keep the
                                # call long-running instead of treating those
                                # fragments as the old text-mode turn limit.
                                live_turn_limit = max(spec.max_turns * 4, 24)
                                if turns >= live_turn_limit:
                                    reason = "max_turns"
                                    stop_event.set()
                                    return

                sender = asyncio.create_task(browser_to_gemini())
                receiver = asyncio.create_task(gemini_to_browser())
                stopped = asyncio.create_task(stop_event.wait())
                tasks = (sender, receiver, stopped)
                try:
                    if spec.navigator_speaks_first:
                        seed = (spec.first_navigator_line or spec.opener).strip()
                        if seed:
                            await live.send_client_content(
                                turns={
                                    "role": "user",
                                    "parts": [
                                        {
                                            "text": (
                                                "Begin the call now. Say exactly this message, "
                                                f"then wait for the reply: {seed}"
                                            )
                                        }
                                    ],
                                },
                                turn_complete=True,
                            )

                    done, _ = await asyncio.wait(
                        tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        if task is not stopped:
                            error = task.exception()
                            if error:
                                raise error
                finally:
                    stop_event.set()
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            reason = "cancelled"
        except Exception as exc:  # noqa: BLE001
            reason = f"error:{type(exc).__name__}"
            console.print(f"[red]Gemini Live session error:[/red] {exc}")
            try:
                await self._send(
                    ws,
                    {"type": "call_error", "error": str(exc)},
                )
            except Exception:  # noqa: BLE001
                pass

        await self._finish(CallResult(transcript=transcript, ended_reason=reason))
