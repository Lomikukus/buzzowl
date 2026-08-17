"""
Transcription router — live mic capture, post-pass, and LLM summarisation.

Covers:
- SentenceBuffer: adaptive VAD and audio chunking
- Audio/transcript save helpers
- WhisperX model cache wrappers
- _stream_post: post-pass (transcribe → align → diarize) with WebSocket progress
- _stream_summary: streaming LLM summary (llm role 'summary') saved to disk
- WebSocket /ws: main recording endpoint
- GET /api/status, POST /api/settings, GET /api/llm/status (+ legacy GET /api/ollama)
- GET/POST /api/llm/config: admin-only provider/role editor (keys always masked)
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
import yaml
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
            for token in llm.stream(prompt, role="summary", model=model or None,
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


@router.get("/api/llm/status")          # canonical route
@router.get("/api/ollama")              # legacy route kept for the UI
async def llm_provider_status(user: dict = Depends(current_user)):
    """Generalized LLM provider status (was the Ollama-only /api/ollama probe).

    Back-compat: knowledge.html and product.html populate model dropdowns from
    `models` (+ `default_model`), so those keys are kept — filled from the
    configured llm role models instead of the live Ollama tag list.
    """
    try:
        providers = llm.status()
    except Exception:
        providers = []
    ok = any(p.get("reachable") or p.get("has_key") for p in providers)

    default_model = ""
    try:
        _, default_model = llm.resolve("summary")
    except Exception:
        pass

    models: list[str] = []
    role_names = sorted({r for p in providers for r in p.get("roles", [])})
    for role_name in role_names:
        try:
            _, m = llm.resolve(role_name)
        except Exception:
            continue
        # Exclude embedding models — they can't generate chat responses
        if m and "embed" not in m.lower() and m not in models:
            models.append(m)

    return {
        "providers": providers,
        "ok": ok,
        "status": "ok" if ok else "offline",
        "models": models,
        "default_model": default_model,
    }


# ---------------------------------------------------------------------------
# LLM provider configuration (admin) — edits the llm: block in config.yaml
# ---------------------------------------------------------------------------

_CONFIG_YAML_PATH = BASE_DIR / "config.yaml"
_KEY_MASK = "•••"
_LLM_KINDS = ("openai-compat", "anthropic", "pi")


def _masked_llm_block() -> dict:
    """Current effective llm block with inline api_key values masked."""
    block = llm._effective_config()
    providers = {}
    for name, raw in (block.get("providers") or {}).items():
        entry = {k: v for k, v in raw.items() if k != "api_key"}
        try:
            # Full resolution chain (inline > env var > legacy env fallbacks)
            entry["has_key"] = bool(llm._get_provider(name).resolve_key())
        except llm.LLMError:
            entry["has_key"] = False
        if raw.get("api_key"):
            entry["api_key"] = _KEY_MASK
        providers[name] = entry
    return {"providers": providers, "roles": dict(block.get("roles") or {}),
            "explicit": isinstance(config.get("llm"), dict)}


@router.get("/api/llm/config")
async def get_llm_config(user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return _masked_llm_block()


@router.post("/api/llm/config")
async def save_llm_config(body: dict, user: dict = Depends(current_user)):
    """Replace the llm: block. api_key semantics: empty or masked value on a
    provider keeps the existing inline key; a real value overwrites it. Keys
    are never echoed back (responses mask them)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    providers = body.get("providers")
    roles = body.get("roles")
    if not isinstance(providers, dict) or not providers:
        raise HTTPException(status_code=400, detail="providers must be a non-empty object")
    if not isinstance(roles, dict) or not roles:
        raise HTTPException(status_code=400, detail="roles must be a non-empty object")

    existing = (config.get("llm") or {}).get("providers", {}) if isinstance(config.get("llm"), dict) else {}
    clean_providers: dict = {}
    for name, raw in providers.items():
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"provider {name!r} must be an object")
        kind = raw.get("kind", "openai-compat")
        if kind not in _LLM_KINDS:
            raise HTTPException(status_code=400, detail=f"provider {name!r}: unknown kind {kind!r}")
        base_url = (raw.get("base_url") or "").strip()
        if kind == "openai-compat" and not base_url:
            raise HTTPException(status_code=400, detail=f"provider {name!r}: base_url required for openai-compat")
        entry: dict = {"kind": kind}
        if base_url:
            entry["base_url"] = base_url
        api_key_env = (raw.get("api_key_env") or "").strip()
        if api_key_env:
            entry["api_key_env"] = api_key_env
        api_key = raw.get("api_key") or ""
        if api_key and api_key != _KEY_MASK:
            entry["api_key"] = api_key
        elif existing.get(name, {}).get("api_key"):
            entry["api_key"] = existing[name]["api_key"]      # keep stored key
        clean_providers[name] = entry

    clean_roles: dict = {}
    for role_name, entry in roles.items():
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail=f"role {role_name!r} must be an object")
        provider_name = entry.get("provider", "")
        model = (entry.get("model") or "").strip()
        if provider_name not in clean_providers:
            raise HTTPException(status_code=400,
                                detail=f"role {role_name!r} references unknown provider {provider_name!r}")
        if not model:
            raise HTTPException(status_code=400, detail=f"role {role_name!r}: model required")
        clean_roles[role_name] = {"provider": provider_name, "model": model}
    if "default" not in clean_roles:
        raise HTTPException(status_code=400, detail="a 'default' role is required")

    new_block = {"providers": clean_providers, "roles": clean_roles}

    # Persist to config.yaml (whole-file rewrite — comments are lost, accepted)
    try:
        raw_cfg = yaml.safe_load(_CONFIG_YAML_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw_cfg = {}
    raw_cfg["llm"] = new_block
    _CONFIG_YAML_PATH.write_text(
        yaml.safe_dump(raw_cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")

    config["llm"] = new_block   # live config is mutated in place, never replaced
    return {"ok": True, **_masked_llm_block()}


# ---------------------------------------------------------------------------
# OpenRouter OAuth (PKCE) — "Connect" flow that provisions a user-controlled
# API key (no copy-paste). The only fully-permitted subscription-style login;
# key lands in llm providers.openrouter.api_key via the same persistence path
# as POST /api/llm/config.
# ---------------------------------------------------------------------------

_OR_AUTH_URL = "https://openrouter.ai/auth"
_OR_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
_or_pending: dict = {}   # user_id → (code_verifier, expires_monotonic)


@router.post("/api/llm/oauth/openrouter/start")
async def openrouter_oauth_start(body: dict, user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    import base64
    import hashlib
    import secrets
    import time as _time
    callback_url = (body.get("callback_url") or "").strip()
    if not callback_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="callback_url required")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _or_pending[user["id"]] = (verifier, _time.monotonic() + 600)
    auth_url = (f"{_OR_AUTH_URL}?callback_url={callback_url}"
                f"&code_challenge={challenge}&code_challenge_method=S256")
    return {"auth_url": auth_url}


@router.post("/api/llm/oauth/openrouter/complete")
async def openrouter_oauth_complete(body: dict, user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    import time as _time
    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code required")
    pending = _or_pending.pop(user["id"], None)
    if not pending or pending[1] < _time.monotonic():
        raise HTTPException(status_code=400, detail="No pending connect — start again")
    verifier = pending[0]

    import httpx
    try:
        async with httpx.AsyncClient(timeout=20) as hc:
            r = await hc.post(_OR_KEYS_URL, json={
                "code": code, "code_verifier": verifier,
                "code_challenge_method": "S256",
            })
            r.raise_for_status()
            key = (r.json() or {}).get("key", "")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"OpenRouter key exchange failed: {exc}")
    if not key:
        raise HTTPException(status_code=502, detail="OpenRouter returned no key")

    # Write the provisioned key into the llm block (create provider if absent)
    block = llm._effective_config()
    providers = {n: dict(p) for n, p in (block.get("providers") or {}).items()}
    entry = providers.get("openrouter") or {
        "kind": "openai-compat", "base_url": "https://openrouter.ai/api/v1"}
    entry["api_key"] = key
    providers["openrouter"] = entry
    new_block = {"providers": providers, "roles": dict(block.get("roles") or {})}

    try:
        raw_cfg = yaml.safe_load(_CONFIG_YAML_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw_cfg = {}
    raw_cfg["llm"] = new_block
    _CONFIG_YAML_PATH.write_text(
        yaml.safe_dump(raw_cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    config["llm"] = new_block
    return {"ok": True, "connected": "openrouter", **_masked_llm_block()}


# ---------------------------------------------------------------------------
# Subscription-login proxy → Pi service /oauth/* (ChatGPT-Codex, GitHub
# Copilot). Gray-zone flows: only exposed when llm_oauth_gray_flows is true
# (neither is officially sanctioned for third-party apps without a whitelist;
# Anthropic's flow is banned outright and never exposed).
# ---------------------------------------------------------------------------

_GRAY_OAUTH_PROVIDERS = ("openai-codex", "github-copilot")


async def _pi_oauth_forward(method: str, path: str, payload: Optional[dict],
                            user: dict) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    provider = (payload or {}).get("provider", "")
    if provider in _GRAY_OAUTH_PROVIDERS and not config.get("llm_oauth_gray_flows"):
        raise HTTPException(
            status_code=403,
            detail="Subscription logins are disabled — set llm_oauth_gray_flows: true "
                   "in config.yaml after reviewing the provider's terms of service.")
    import httpx
    base = config.get("agent_service_url_pi") or config.get("agent_service_url", "http://localhost:8001")
    token = config.get("agent_service_token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=70) as hc:
            if method == "GET":
                r = await hc.get(f"{base}{path}", headers=headers)
            else:
                r = await hc.post(f"{base}{path}", json=payload or {}, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Pi service unreachable: {exc}")
    body = r.json() if r.content else {}
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code,
                            detail=body.get("error") or body.get("detail") or "Pi OAuth error")
    return body


@router.get("/api/llm/oauth/pi/status")
async def pi_oauth_status(user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    enabled = bool(config.get("llm_oauth_gray_flows"))
    providers = {}
    if enabled:
        try:
            providers = await _pi_oauth_forward("GET", "/oauth/status", None, user)
        except HTTPException:
            providers = {}
    return {"enabled": enabled, "providers": providers}


@router.post("/api/llm/oauth/pi/start")
async def pi_oauth_start(body: dict, user: dict = Depends(current_user)):
    return await _pi_oauth_forward("POST", "/oauth/start", body, user)


@router.post("/api/llm/oauth/pi/complete")
async def pi_oauth_complete(body: dict, user: dict = Depends(current_user)):
    return await _pi_oauth_forward("POST", "/oauth/complete", body, user)


@router.post("/api/llm/oauth/pi/disconnect")
async def pi_oauth_disconnect(body: dict, user: dict = Depends(current_user)):
    return await _pi_oauth_forward("POST", "/oauth/disconnect", body, user)


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
                            ws, segments, ollama_model, detected_language, loop, summary_path
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
                            ws, last_segments, model, detected_language, loop, summary_path
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
