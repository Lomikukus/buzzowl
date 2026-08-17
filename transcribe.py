#!/usr/bin/env python3
"""
Buzzowl — post-pass transcription CLI
Usage: python transcribe.py <audio_or_video_file> [--language en] [--model large-v2]
"""

import argparse
import os
import sys
import wave
from datetime import datetime
from pathlib import Path

import yaml
import torch
from dotenv import load_dotenv
import whisperx
from rich.console import Console
from rich.text import Text

import llm

console = Console()

BASE_DIR = Path(__file__).parent

SPEAKER_COLOURS = [
    "bold cyan", "bold yellow", "bold magenta", "bold green",
    "bold blue", "bold red", "bold white",
]


# ---------------------------------------------------------------------------
# Output helpers (shared with server.py)
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    for d in ("data/raw", "data/staged", "data/sorted"):
        (BASE_DIR / d).mkdir(parents=True, exist_ok=True)


def _fmt_ts(s: float) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def format_transcript(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        ts = f"[{_fmt_ts(seg['start'])} → {_fmt_ts(seg['end'])}]"
        speaker = f"  [{seg['speaker']}]" if seg.get("speaker") else ""
        lines.append(f"{ts}{speaker}  {seg['text'].strip()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    load_dotenv(BASE_DIR / ".env")
    config_path = BASE_DIR / "config.yaml"
    defaults = {
        "model": "large-v2",
        "language": "auto",
        "compute_type": "int8",
        "hf_token": "",
        "ollama_model": "llama3.2",
    }
    if config_path.exists():
        with open(config_path) as f:
            loaded = yaml.safe_load(f) or {}
        defaults.update(loaded)
    if not defaults.get("hf_token"):
        defaults["hf_token"] = os.environ.get("HFTOKEN", "")
    return defaults


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def speaker_colour(label: str, colour_map: dict) -> str:
    if label not in colour_map:
        colour_map[label] = SPEAKER_COLOURS[len(colour_map) % len(SPEAKER_COLOURS)]
    return colour_map[label]


def print_segment(start: float, end: float, text: str, speaker: str = "", colour_map: dict | None = None) -> None:
    colour_map = colour_map or {}
    ts = Text()
    ts.append(f"[{_fmt_ts(start)} → {_fmt_ts(end)}]", style="bold cyan")
    if speaker:
        colour = speaker_colour(speaker, colour_map)
        ts.append(f"  [{speaker}]", style=colour)
    ts.append(f"  {text.strip()}")
    console.print(ts)


# ---------------------------------------------------------------------------
# AI summary
# ---------------------------------------------------------------------------

def summarize(transcript: str, model: str, language: str, output_path: Path) -> None:
    """Generate a summary via the LLM provider layer (role 'summary'), save to disk.

    `model` optionally overrides the role's configured model (empty → role default).
    """
    prompt = (
        "You are a meeting and lecture summarizer. "
        f"The following transcript is in {language}. "
        "Produce a structured summary in the same language with these sections:\n"
        "**Title** (one line, auto-generated)\n"
        "**TL;DR** (3–5 sentences)\n"
        "**Key Takeaways** (bullet points)\n"
        "**Action Items** (bullet points, write 'None' if there are none)\n\n"
        f"Transcript:\n{transcript}"
    )

    try:
        text = llm.complete(prompt, role="summary", model=model or None, timeout=120)
    except Exception as e:
        console.print(
            f"\n[yellow]Summary failed: {e} — skipping summary "
            "(summary model unreachable — role 'summary' in llm config).[/yellow]"
        )
        return

    console.rule("[bold yellow]AI Summary[/bold yellow]")
    console.print(text)
    console.rule()

    if text:
        output_path.write_text(text, encoding="utf-8")
        console.print(f"  [dim]Summary saved → {output_path}[/dim]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ensure_dirs()
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    config = load_config()

    parser = argparse.ArgumentParser(description="Transcribe an audio or video file using WhisperX.")
    parser.add_argument("file", help="Path to audio/video file (mp4, m4a, mp3, wav, ...)")
    parser.add_argument("--language", default=config["language"],
                        help="Language code (e.g. 'en', 'de') or 'auto' for detection")
    parser.add_argument("--model", default=config["model"],
                        help="WhisperX model size (e.g. tiny, base, large-v2)")
    parser.add_argument("--no-diarize", action="store_true",
                        help="Skip speaker diarization even if hf_token is set")
    parser.add_argument("--no-summary", action="store_true",
                        help="Skip AI summarization")
    args = parser.parse_args()

    file_path = args.file
    if not os.path.isfile(file_path):
        console.print(f"[bold red]Error:[/bold red] File not found: {file_path}")
        sys.exit(1)

    language = None if args.language == "auto" else args.language
    hf_token = config.get("hf_token", "").strip()
    ollama_model = config.get("ollama_model", "").strip()

    if torch.cuda.is_available():
        device = "cuda"
        compute_type = config.get("compute_type", "float16")
    else:
        device = "cpu"
        compute_type = "int8"

    diarize = bool(hf_token) and not args.no_diarize
    do_summary = bool(ollama_model) and not args.no_summary

    console.print(f"\n[bold]Buzzowl[/bold] — post-pass transcription")
    console.print(f"  Session : [yellow]{session_id}[/yellow]")
    console.print(f"  File    : [green]{file_path}[/green]")
    console.print(f"  Model   : [yellow]{args.model}[/yellow]")
    console.print(f"  Device  : [yellow]{device}[/yellow]  (compute: {compute_type})")
    console.print(f"  Language: [yellow]{'auto-detect' if language is None else language}[/yellow]")
    console.print(f"  Diarize : [yellow]{'yes' if diarize else 'no (set hf_token in config.yaml)'}[/yellow]")
    console.print(f"  Summary : [yellow]{'yes — ' + ollama_model if do_summary else 'no'}[/yellow]\n")

    console.print("[bold]Loading model...[/bold]")
    model = whisperx.load_model(args.model, device, compute_type=compute_type)

    console.print("[bold]Loading audio...[/bold]")
    audio = whisperx.load_audio(file_path)

    console.print("[bold]Transcribing...[/bold]\n")
    result = model.transcribe(audio, language=language)

    detected_language = result.get("language", language or "unknown")
    console.print(f"Detected language: [cyan]{detected_language}[/cyan]\n")

    console.print("[bold]Aligning timestamps...[/bold]\n")
    model_a, metadata = whisperx.load_align_model(
        language_code=detected_language, device=device
    )
    result = whisperx.align(
        result["segments"], model_a, metadata, audio, device,
        return_char_alignments=False
    )

    if diarize:
        console.print("[bold]Diarizing speakers...[/bold]\n")
        try:
            diarize_model = whisperx.DiarizationPipeline(use_auth_token=hf_token, device=device)
            diarize_segments = diarize_model(audio)
            result = whisperx.assign_word_speakers(diarize_segments, result)
        except Exception as e:
            console.print(f"[yellow]Diarization failed: {e}[/yellow]")
            console.print("[yellow]Continuing without speaker labels.[/yellow]\n")

    # Print transcript
    colour_map: dict = {}
    console.rule("[bold cyan]Transcript[/bold cyan]")
    segments = result["segments"]
    for segment in segments:
        speaker = segment.get("speaker", "")
        print_segment(segment["start"], segment["end"], segment["text"], speaker, colour_map)

    console.rule()
    console.print(f"\n[bold green]Done.[/bold green] {len(segments)} segments transcribed.\n")

    # Save transcript to data/raw/{session_id}/
    raw_dir = BASE_DIR / "data" / "raw" / session_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    tx_path = raw_dir / "transcript.txt"
    tx_path.write_text(format_transcript(segments), encoding="utf-8")
    console.print(f"  [dim]Transcript saved → data/raw/{session_id}/transcript.txt[/dim]")

    # AI summary → data/staged/{session_id}/
    if do_summary and segments:
        full_transcript = format_transcript(segments)
        staged_dir = BASE_DIR / "data" / "staged" / session_id
        staged_dir.mkdir(parents=True, exist_ok=True)
        summary_path = staged_dir / "summary.md"
        summarize(full_transcript, ollama_model, detected_language, summary_path)


if __name__ == "__main__":
    main()
