from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from src.world import World

console = Console()
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="DME Care Navigator — local multi-agent world (Ollama + edge-tts)"
    )
    p.add_argument(
        "--dry-check",
        action="store_true",
        help="Boot world and print agents + first decision",
    )
    p.add_argument(
        "--llm",
        action="store_true",
        help="Real LLM dialogue (Ollama, Gemini, or OpenAI via LLM_PROVIDER)",
    )
    p.add_argument(
        "--voice",
        action="store_true",
        help="Speak conversations with edge-tts (implies --llm unless --voice-only-scripted)",
    )
    p.add_argument(
        "--voice-only-scripted",
        action="store_true",
        help="TTS scripted simulate run (no LLM chat; still uses edge-tts)",
    )
    p.add_argument("--max-parallel", type=int, default=None)
    return p.parse_args()


async def _ollama_ready() -> tuple[bool, str]:
    import httpx

    base = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").replace("/v1", "")
    model = os.getenv("OLLAMA_MODEL", "llama3.2")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base}/api/tags")
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            if any(model in n or n.startswith(model) for n in names):
                return True, f"Ollama up; model '{model}' present"
            return False, (
                f"Ollama is running but model '{model}' is missing. "
                f"Run: ollama pull {model}\nInstalled: {names or 'none'}"
            )
    except Exception as exc:  # noqa: BLE001
        return False, (
            "Cannot reach Ollama at 127.0.0.1:11434.\n"
            "Start it with: ollama serve\n"
            f"Then: ollama pull {model}\n"
            f"Detail: {exc}"
        )


async def async_main() -> int:
    args = parse_args()
    use_llm = args.llm or (args.voice and not args.voice_only_scripted)
    simulate = not use_llm
    voice = args.voice or args.voice_only_scripted
    provider = os.getenv("LLM_PROVIDER", "ollama").lower()

    if use_llm and provider == "ollama":
        ok, msg = await _ollama_ready()
        if not ok:
            console.print(f"[red]{msg}[/red]")
            return 2
        console.print(f"[dim]{msg}[/dim]")
    elif use_llm and provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        console.print("[red]OPENAI_API_KEY missing for LLM_PROVIDER=openai[/red]")
        return 2
    elif use_llm and provider == "gemini":
        if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
            console.print(
                "[red]GEMINI_API_KEY missing for LLM_PROVIDER=gemini[/red]\n"
                "Get a free key at https://aistudio.google.com/apikey then put it in .env"
            )
            return 2
        console.print(
            f"[dim]Gemini {os.getenv('GEMINI_MODEL', 'gemini-flash-lite-latest')} "
            "(free tier, paced)[/dim]"
        )

    world = World.load(simulate=simulate, voice=voice)
    if args.max_parallel:
        world.max_parallel = args.max_parallel

    if args.dry_check:
        from src.policy import decide

        console.print("case:", world.case.case_id)
        console.print("agents:", world.bus.agents())
        console.print("suppliers:", len(world.case.suppliers))
        console.print("first decision:", decide(world.case, max_parallel=world.max_parallel))
        return 0

    bits = []
    bits.append("SIMULATE" if simulate else f"LLM({provider})")
    if voice:
        bits.append("VOICE/edge-tts")
    console.print(f"[yellow]Mode: {' + '.join(bits)}[/yellow]")
    final = await world.run()

    console.rule("Final status")
    console.print(f"status={final.status} human_needed={final.human_needed}")
    if final.selected_supplier_id:
        s = final.get_supplier(final.selected_supplier_id)
        console.print(f"selected_supplier={s.name if s else final.selected_supplier_id}")
    console.print(f"pcp.order_status={final.pcp.order_status}")
    console.print(f"delivery={final.delivery.status} ({final.delivery.scheduled_for})")
    ok = final.status == "completed" or (
        final.delivery.status == "scheduled" and final.pcp.order_status == "received"
    )
    return 0 if ok else 1


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        console.print("\nInterrupted")
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Fatal:[/red] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
