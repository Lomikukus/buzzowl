"""
Transcription router — live mic capture, post-pass, and LLM summarisation.

Covers:
- SentenceBuffer: adaptive VAD and audio chunking
- Audio/transcript save helpers
- WhisperX model cache wrappers
- _stream_post: post-pass (transcribe → align → diarize) with WebSocket progress
- _stream_summary: streaming LLM summary (llm role 'summary') saved to disk
- WebSocket /ws: main recording endpoint
- GET /api/status, POST /api/settings

LLM provider status/config/OAuth endpoints moved to routers/llm_config.py.
"""

import asyncio
import json
import re
import threading
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

# Guarded separately: the Docker `transcribe` variant ships faster-whisper
# only (CTranslate2, no PyTorch) — whisperx/diarization stay host-only and the
# post-pass degrades to a plain faster-whisper pass when whisperx is absent.
try:
    import whisperx
except ImportError:
    whisperx = None  # type: ignore
try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None  # type: ignore

import context
import llm
from routers.auth import current_user
from context import (
    BASE_DIR,
    SAMPLE_RATE,
    _model_cache,
    _model_lock,
    config,
    console,
    executor,
    _default_org_id,
)
from routers.pipeline import _trigger_enrichment, _write_session_metadata, _read_session_metadata

router = APIRouter()


# ---------------------------------------------------------------------------
# SentenceBuffer — adaptive VAD
# ---------------------------------------------------------------------------

class SentenceBuffer:
    """Accumulates audio frames and flushes at natural speech pauses.

    Two modes:
    - Fixed  (adaptive=False): silence = RMS < silence_rms threshold.
    - Adaptive (adaptive=True): noise floor is estimated from quiet frames
      and the threshold tracks the room/mic environment automatically.
    """

    FRAME         = 512    # ~32 ms at 16 kHz
    _SPEECH_RATIO = 3.0
    _NOISE_INIT   = 0.008
    _NOISE_ALPHA  = 0.02

    def __init__(
        self,
        sample_rate: int    = SAMPLE_RATE,
        silence_rms: float  = 0.015,
        min_silence_ms: int = 600,
        max_duration_s: int = 15,
        adaptive: bool      = False,
    ) -> None:
        self.silence_rms        = silence_rms
        self.adaptive           = adaptive
        self.min_silence_frames = int(min_silence_ms / 1000 * sample_rate / self.FRAME)
        self.max_samples        = max_duration_s * sample_rate
        self._buf               = np.array([], dtype=np.float32)
        self._silence_frames    = 0
        self._has_speech        = False
        self._noise_floor       = self._NOISE_INIT

    @property
    def current_threshold(self) -> float:
        return self._noise_floor * self._SPEECH_RATIO if self.adaptive else self.silence_rms

    def push(self, samples: np.ndarray) -> np.ndarray | None:
        self._buf = np.concatenate([self._buf, samples])
        if len(self._buf) >= self.FRAME:
            rms       = float(np.sqrt(np.mean(self._buf[-self.FRAME:] ** 2)))
            threshold = self.current_threshold

            if self.adaptive and rms < threshold:
                self._noise_floor = (
                    (1 - self._NOISE_ALPHA) * self._noise_floor + self._NOISE_ALPHA * rms
                )
                self._noise_floor = max(self._noise_floor, 0.001)

            if rms > threshold:
                self._has_speech     = True
                self._silence_frames = 0
            elif self._has_speech:
                self._silence_frames += 1

        should_flush = (
            self._has_speech and self._silence_frames >= self.min_silence_frames
        ) or len(self._buf) >= self.max_samples

        return self._take() if should_flush else None

    def flush(self) -> np.ndarray | None:
        if self._has_speech and len(self._buf) > 0:
            return self._take()
        return None

    def _take(self) -> np.ndarray:
        chunk                = self._buf.copy()
        self._buf            = np.array([], dtype=np.float32)
        self._silence_frames = 0
        self._has_speech     = False
        return chunk


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def save_wav(path: Path, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    audio_int16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())


def _fmt_ts(s: float) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def format_transcript(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        ts      = f"[{_fmt_ts(seg['start'])} → {_fmt_ts(seg['end'])}]"
        speaker = f"  [{seg['speaker']}]" if seg.get("speaker") else ""
        lines.append(f"{ts}{speaker}  {seg['text'].strip()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Model cache wrappers
# ---------------------------------------------------------------------------

def get_live_model(name: str) -> WhisperModel:
    """Return a faster-whisper live model, loading it on first use."""
    key = f"live:{name}"
    with _model_lock:
        if key not in _model_cache:
            console.print(f"[bold]Loading live model: {name}...[/bold]")
            _model_cache[key] = WhisperModel(name, device="cpu", compute_type=config["compute_type"])
            console.print(f"[green]Live model {name} ready.[/green]")
        return _model_cache[key]


def get_post_model(name: str):
    """Return a WhisperX post-pass model, loading it on first use."""
    key = f"post:{name}"
    with _model_lock:
        if key not in _model_cache:
            console.print(f"[bold]Loading post model: {name}...[/bold]")
            _model_cache[key] = whisperx.load_model(name, "cpu", compute_type=config["compute_type"])
            console.print(f"[green]Post model {name} ready.[/green]")
        return _model_cache[key]


def get_align_model(language_code: str):
    """Return the WhisperX alignment model for a language, loading it on first use."""
    key = f"align:{language_code}"
    with _model_lock:
        if key not in _model_cache:
            console.print(f"[bold]Loading align model for {language_code}...[/bold]")
            model_a, metadata = whisperx.load_align_model(language_code=language_code, device="cpu")
            _model_cache[key] = (model_a, metadata)
            console.print(f"[green]Align model ({language_code}) ready.[/green]")
        return _model_cache[key]


def get_diarize_model(hf_token: str):
    """Return the WhisperX diarization pipeline, loading it on first use."""
    key = "diarize"
    with _model_lock:
        if key not in _model_cache:
            console.print("[bold]Loading diarization model...[/bold]")
            _model_cache[key] = whisperx.DiarizationPipeline(use_auth_token=hf_token, device="cpu")
            console.print("[green]Diarization model ready.[/green]")
        return _model_cache[key]


# ---------------------------------------------------------------------------
# Core transcription / summary helpers
# ---------------------------------------------------------------------------

def _transcribe_sentence(audio: np.ndarray, model_name: str, language: str | None) -> list[dict]:
    model = get_live_model(model_name)
    segs, _ = model.transcribe(audio, beam_size=5, language=language)
    return [
        {"start": s.start, "end": s.end, "text": s.text.strip()}
        for s in segs if s.text.strip()
    ]


async def _stream_post(
    ws: WebSocket,
    audio: np.ndarray,
    model_name: str,
    language: str | None,
    hf_token: str,
    loop: asyncio.AbstractEventLoop,
) -> list[dict]:
    """Run WhisperX post-pass in a worker thread, streaming stage updates over WebSocket."""
    total_duration         = len(audio) / SAMPLE_RATE
    queue: asyncio.Queue   = asyncio.Queue()
    result_holder: list[list[dict]] = []

    def worker() -> None:
        try:
            if whisperx is None:
                # transcribe-lite (Docker profile): plain faster-whisper full-
                # buffer pass — no alignment, no diarization. Same wire shapes.
                asyncio.run_coroutine_threadsafe(
                    queue.put({"type": "post_stage",
                               "stage": "Transcribing (lite — no diarization)…"}), loop)
                lite_segs, _info = get_live_model(model_name).transcribe(
                    audio, language=language)
                segments = [{"start": s.start, "end": s.end, "text": s.text}
                            for s in lite_segs if s.text.strip()]
                result_holder.append(segments)
                for seg in segments:
                    progress = round(min(seg["end"] / total_duration, 1.0), 3)
                    asyncio.run_coroutine_threadsafe(
                        queue.put({"type": "post", "start": seg["start"],
                                   "end": seg["end"], "text": seg["text"].strip(),
                                   "speaker": "", "progress": progress}), loop)
                return
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "post_stage", "stage": "Transcribing…"}), loop
            )
            model = get_post_model(model_name)
            raw   = model.transcribe(audio, language=language)
            detected_lang = raw.get("language", language or "en")

            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "post_stage", "stage": "Aligning timestamps…"}), loop
            )
            model_a, metadata = get_align_model(detected_lang)
            aligned = whisperx.align(
                raw["segments"], model_a, metadata, audio, "cpu",
                return_char_alignments=False,
            )

            if hf_token:
                asyncio.run_coroutine_threadsafe(
                    queue.put({"type": "post_stage", "stage": "Diarizing speakers…"}), loop
                )
                try:
                    diarize_model = get_diarize_model(hf_token)
                    diarize_segs  = diarize_model(audio)
                    aligned = whisperx.assign_word_speakers(diarize_segs, aligned)
                except Exception as e:
                    console.print(f"[yellow]Diarization error: {e}[/yellow]")

            segments = aligned.get("segments", [])
            result_holder.append(segments)
            for seg in segments:
                if not seg.get("text", "").strip():
                    continue
                progress = round(min(seg["end"] / total_duration, 1.0), 3)
                asyncio.run_coroutine_threadsafe(
                    queue.put({
                        "type": "post", "start": seg["start"], "end": seg["end"],
                        "text": seg["text"].strip(), "speaker": seg.get("speaker", ""),
                        "progress": progress,
                    }),
                    loop,
                )
        except Exception as e:
            console.print(f"[red]Post-pass error: {e}[/red]")
            result_holder.append([])
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        msg = await queue.get()
        if msg is None:
            break
        if msg["type"] == "post":
            console.print(
                f"  [green][post {int(msg['progress'] * 100):3d}%][/green] "
                f"[{msg['speaker']}] {msg['text']}" if msg["speaker"]
                else f"  [green][post {int(msg['progress'] * 100):3d}%][/green] {msg['text']}"
            )
        await ws.send_text(json.dumps(msg))

    return result_holder[0] if result_holder else []


async def _stream_summary(
    ws: WebSocket,
    segments: list[dict],
    model: str,
    language: str,
    loop: asyncio.AbstractEventLoop,
    output_path: Path,
    org_id: Optional[int] = None,
) -> None:
    """Stream a structured summary (llm role 'summary') to the client, save to disk.

    `model` optionally overrides the role's configured model (empty → role default).
    """
    lines = []
    for seg in segments:
        speaker = seg.get("speaker", "")
        text    = seg.get("text", "").strip()
        lines.append(f"[{speaker}] {text}" if speaker else text)
    transcript = "\n".join(lines)

    prompt = (
        "You are a meeting and lecture summarizer. "
        f"The following transcript is in '{language}'. "
        "Produce a structured summary in the same language with these sections:\n"
        "**Title** (one line, auto-generated)\n"
        "**TL;DR** (3–5 sentences)\n"
        "**Key Takeaways** (bullet points)\n"
        "**Action Items** (bullet points, write 'None' if there are none)\n\n"
        f"Transcript:\n{transcript}"
    )

    await ws.send_text(json.dumps({"type": "summary_start"}))

    def worker() -> None:
        tokens: list[str] = []
        try:
            # llm.stream raises fast (no retry) so we can degrade gracefully here
            for token in llm.stream(prompt, role="summary", model=model or None, org_id=org_id,
                                    timeout=120):
                if token:
                    tokens.append(token)
                    asyncio.run_coroutine_threadsafe(
                        ws.send_text(json.dumps({"type": "summary_chunk", "text": token})),
                        loop,
                    )
        except Exception as e:
            console.print(f"[yellow]Summary error: {e}[/yellow]")
            asyncio.run_coroutine_threadsafe(
                ws.send_text(json.dumps({
                    "type": "summary_chunk",
                    "text": "\n\n*(Summary unavailable — summary model unreachable "
                            "(role 'summary' in llm config))*",
                })),
                loop,
            )
        finally:
            if tokens:
                output_path.write_text("".join(tokens), encoding="utf-8")
                console.print(f"  [dim]Summary saved → {output_path.name}[/dim]")
            asyncio.run_coroutine_threadsafe(
                ws.send_text(json.dumps({"type": "summary_done"})), loop
            )

    await loop.run_in_executor(executor, worker)


# ---------------------------------------------------------------------------
# Status + settings routes
# ---------------------------------------------------------------------------

@router.get("/api/status")
async def app_status(user: dict = Depends(current_user)):
    return {
        "hf_token_set": bool(config.get("hf_token", "").strip()),
        "transcription_mode": config.get("transcription_mode", "local"),
        "whisper_available": WhisperModel is not None,
        "diarization_available": whisperx is not None,
    }


@router.post("/api/settings")
async def save_settings(body: dict, user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    hf_token = body.get("hf_token", "").strip()
    env_path = BASE_DIR / ".env"
    lines    = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pattern  = re.compile(r'^HFTOKEN\s*=', re.IGNORECASE)
    new_line = f'HFTOKEN="{hf_token}"' if hf_token else 'HFTOKEN=""'
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = new_line
            replaced  = True
            break
    if not replaced:
        lines.append(new_line)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config["hf_token"] = hf_token
    _model_cache.pop("diarize", None)  # evict so next session reloads with the new token
    return {"ok": True, "hf_token_set": bool(hf_token)}


# ---------------------------------------------------------------------------
# WebSocket /ws — main recording endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # Token auth via ?token= — fail-open only when the DB layer is absent (local dev)
    from context import DB_AVAILABLE as _dba, db_module as _dbm
    if _dba and _dbm is not None:
        token = ws.query_params.get("token", "")
        ws_user = await _dbm.get_user_by_token(token) if token else None
        if ws_user is None:
            await ws.close(code=4401)
            return
    await ws.accept()
    if config.get("transcription_mode", "local") != "local":
        await ws.send_text(json.dumps({"type": "error", "text": "Server transcription disabled (transcription_mode: app)"}))
        await ws.close()
        return
    ws_org_id = ws_user["org_id"] if (_dba and _dbm is not None and ws_user) else None
    context._live_ws_connections[ws] = ws_org_id
    console.print("[cyan]Client connected[/cyan]")

    language: str | None    = None
    detected_language       = "en"
    live_model_name         = config["live_model"]
    post_model_name         = config["model"]
    adaptive_vad            = False
    audio_buffer            = np.array([], dtype=np.float32)
    sentence_buf            = SentenceBuffer()
    total_received: int     = 0
    sentence_buf_start: int = 0
    session_id: str         = ""
    last_segments: list[dict] = []
    last_session_id: str    = ""
    hf_token                = config.get("hf_token", "").strip()
    ollama_model            = config.get("ollama_model", "").strip()
    loop                    = asyncio.get_event_loop()

    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                break

            # --- Binary: incoming mic audio ---
            if msg.get("bytes"):
                samples      = np.frombuffer(msg["bytes"], dtype=np.float32).copy()
                audio_buffer = np.concatenate([audio_buffer, samples])
                chunk        = sentence_buf.push(samples)
                total_received += len(samples)

                if chunk is not None:
                    offset_s           = sentence_buf_start / SAMPLE_RATE
                    sentence_buf_start = total_received
                    segs = await loop.run_in_executor(
                        executor, _transcribe_sentence, chunk, live_model_name, language
                    )
                    for seg in segs:
                        seg["start"] += offset_s
                        seg["end"]   += offset_s
                        console.print(f"  [dim][live][/dim] {seg['text']}")
                        await ws.send_text(json.dumps({**seg, "type": "live"}))

            # --- Text: control messages ---
            elif msg.get("text"):
                cmd    = json.loads(msg["text"])
                action = cmd.get("action")

                if action == "start":
                    session_id        = datetime.now().strftime("%Y%m%d-%H%M%S")
                    lang              = cmd.get("language", "auto")
                    language          = None if lang == "auto" else lang
                    detected_language = language or "en"
                    live_model_name   = cmd.get("live_model", config["live_model"])
                    post_model_name   = cmd.get("post_model", config["model"])
                    adaptive_vad      = bool(cmd.get("adaptive_vad", False))
                    console.print(
                        f"  Session: [yellow]{session_id}[/yellow]  "
                        f"Language: [yellow]{lang}[/yellow]  "
                        f"live=[yellow]{live_model_name}[/yellow]  "
                        f"post=[yellow]{post_model_name}[/yellow]  "
                        f"vad=[yellow]{'adaptive' if adaptive_vad else 'fixed'}[/yellow]"
                    )
                    audio_buffer       = np.array([], dtype=np.float32)
                    sentence_buf       = SentenceBuffer(adaptive=adaptive_vad)
                    total_received     = 0
                    sentence_buf_start = 0
                    loop.run_in_executor(executor, get_live_model, live_model_name)
                    loop.run_in_executor(executor, get_post_model, post_model_name)

                elif action == "stop":
                    # Flush any remaining audio buffered by the VAD
                    chunk = sentence_buf.flush()
                    if chunk is not None:
                        offset_s = sentence_buf_start / SAMPLE_RATE
                        segs = await loop.run_in_executor(
                            executor, _transcribe_sentence, chunk, live_model_name, language
                        )
                        for seg in segs:
                            seg["start"] += offset_s
                            seg["end"]   += offset_s
                            console.print(f"  [dim][live][/dim] {seg['text']}")
                            await ws.send_text(json.dumps({**seg, "type": "live"}))

                    if len(audio_buffer) > SAMPLE_RATE * 1.0:
                        duration = len(audio_buffer) / SAMPLE_RATE
                        console.print(f"[bold]Post-pass ({post_model_name}): {duration:.1f}s[/bold]")
                        await ws.send_text(json.dumps({
                            "type": "post_start",
                            "duration": round(duration, 1),
                            "model": post_model_name,
                        }))
                        segments = await _stream_post(
                            ws, audio_buffer, post_model_name, language, hf_token, loop
                        )

                        if session_id and len(audio_buffer) > 0:
                            raw_dir = BASE_DIR / "data" / "raw" / session_id
                            raw_dir.mkdir(parents=True, exist_ok=True)
                            save_wav(raw_dir / "audio.wav", audio_buffer)
                            console.print(f"  [dim]Audio saved → data/raw/{session_id}/audio.wav[/dim]")

                        if session_id and segments:
                            raw_dir = BASE_DIR / "data" / "raw" / session_id
                            raw_dir.mkdir(parents=True, exist_ok=True)
                            tx_path = raw_dir / "transcript.txt"
                            tx_path.write_text(format_transcript(segments), encoding="utf-8")
                            console.print(f"  [dim]Transcript saved → data/raw/{session_id}/transcript.txt[/dim]")
                            speakers_found = {s.get("speaker", "") for s in segments if s.get("speaker")}
                            _write_session_metadata(session_id, {
                                "session_id":  session_id,
                                "status":      "staged",
                                "created_at":  datetime.now(timezone.utc).isoformat(),
                                "duration_s":  round(len(audio_buffer) / SAMPLE_RATE),
                                "speakers":    len(speakers_found) if speakers_found else 1,
                                "language":    detected_language,
                                "title":       None,
                                "entities":    {"companies": [], "people": [], "topics": []},
                                "agent_run_id": None,
                                "promoted_at": None,
                                "error":       None,
                            })
                        last_segments   = segments
                        last_session_id = session_id
                    else:
                        segments = []

                    await ws.send_text(json.dumps({"type": "post_done"}))

                    if ollama_model and segments and session_id:
                        staged_dir   = BASE_DIR / "data" / "staged" / session_id
                        staged_dir.mkdir(parents=True, exist_ok=True)
                        summary_path = staged_dir / "summary.md"
                        console.print(f"[bold]Summarizing with {ollama_model}...[/bold]")
                        await _stream_summary(
                            ws, segments, ollama_model, detected_language, loop, summary_path,
                            org_id=ws_org_id,
                        )
                        org_id = ws_org_id if ws_org_id is not None else await _default_org_id()
                        asyncio.create_task(_trigger_enrichment(session_id, org_id))

                elif action == "summarize":
                    model = cmd.get("model", "").strip() or ollama_model
                    if model and last_segments:
                        staged_dir   = BASE_DIR / "data" / "staged" / last_session_id
                        staged_dir.mkdir(parents=True, exist_ok=True)
                        summary_path = staged_dir / "summary.md"
                        console.print(f"[bold]Re-summarizing with {model}...[/bold]")
                        await _stream_summary(
                            ws, last_segments, model, detected_language, loop, summary_path,
                            org_id=ws_org_id,
                        )
                        meta = _read_session_metadata(last_session_id)
                        if not meta or meta.get("status") != "promoted":
                            org_id = ws_org_id if ws_org_id is not None else await _default_org_id()
                            asyncio.create_task(_trigger_enrichment(last_session_id, org_id))

                    audio_buffer       = np.array([], dtype=np.float32)
                    sentence_buf       = SentenceBuffer(adaptive=adaptive_vad)
                    total_received     = 0
                    sentence_buf_start = 0

    except WebSocketDisconnect:
        pass
    finally:
        context._live_ws_connections.pop(ws, None)

    console.print("[yellow]Client disconnected[/yellow]")
