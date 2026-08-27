"""
tests/test_transcription.py — Transcription smoke tests.

The two WhisperX smoke tests are marked @slow because they load the model.
The summary-degradation test is fast (no models, no network beyond a dead port).

Run:  pytest -m slow tests/test_transcription.py -v
Skip: pytest -m "not slow"
"""

import asyncio
import json
import socket
import sys
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
TONE_WAV = FIXTURES / "audio" / "tone.wav"
TRANSCRIBE_PY = Path(__file__).parent.parent / "transcribe.py"


@pytest.mark.slow
def test_whisperx_transcribes_tone():
    """WhisperX tiny model transcribes a WAV without crashing."""
    whisperx = pytest.importorskip("whisperx", reason="ML stack not installed (CI)")

    model = whisperx.load_model("tiny", "cpu", compute_type="int8")
    audio = whisperx.load_audio(str(TONE_WAV))
    result = model.transcribe(audio, language="en")

    assert "segments" in result
    assert isinstance(result["segments"], list)
    # Segments may be empty for a pure tone — we only care it doesn't error


async def test_stream_summary_degrades_when_summary_role_unreachable(monkeypatch, tmp_path):
    """With the 'summary' role pointing at an unreachable base_url, _stream_summary
    must send the friendly error chunk + summary_done over the WS and not raise."""
    import context
    from routers import transcription as tr

    # A port with nothing listening — llm.stream raises ConnectionError fast
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]

    monkeypatch.setattr(context, "config", {
        "llm": {
            "providers": {
                "dead": {"kind": "openai-compat",
                         "base_url": f"http://127.0.0.1:{dead_port}/v1",
                         "api_key": "k"},
            },
            "roles": {
                "default": {"provider": "dead", "model": "m"},
                "summary": {"provider": "dead", "model": "m"},
            },
        }
    })

    sent: list[dict] = []

    class FakeWS:
        async def send_text(self, text: str) -> None:
            sent.append(json.loads(text))

    loop = asyncio.get_running_loop()
    output_path = tmp_path / "summary.md"
    await tr._stream_summary(
        FakeWS(), [{"speaker": "S1", "text": "hello world"}], "", "en", loop, output_path,
    )

    # The worker thread schedules sends via run_coroutine_threadsafe — let them flush
    for _ in range(100):
        if any(m.get("type") == "summary_done" for m in sent):
            break
        await asyncio.sleep(0.05)

    types = [m.get("type") for m in sent]
    assert types[0] == "summary_start"
    assert types[-1] == "summary_done"
    error_chunks = [m for m in sent if m.get("type") == "summary_chunk"]
    assert error_chunks, f"no summary_chunk sent: {sent}"
    assert "Summary unavailable" in error_chunks[-1]["text"]
    assert "role 'summary'" in error_chunks[-1]["text"]
    assert not output_path.exists()  # nothing streamed → nothing persisted


@pytest.mark.slow
def test_cli_transcribes_to_disk():
    """transcribe.py CLI runs end-to-end and writes transcript.txt to data/raw/."""
    pytest.importorskip("torch", reason="ML stack not installed (CI)")
    project_root = TRANSCRIBE_PY.parent
    raw_dir = project_root / "data" / "raw"
    before = set(raw_dir.iterdir()) if raw_dir.exists() else set()

    proc = subprocess.run(
        [
            sys.executable, str(TRANSCRIBE_PY),
            str(TONE_WAV),
            "--model", "tiny",
            "--no-diarize",
            "--no-summary",
        ],
        capture_output=True,
        text=True,
        cwd=str(project_root),
        timeout=120,
    )

    assert proc.returncode == 0, f"CLI exited {proc.returncode}:\n{proc.stderr}"

    after = set(raw_dir.iterdir()) if raw_dir.exists() else set()
    new_dirs = after - before
    assert new_dirs, f"No new session directory created under {raw_dir}"

    session_dir = next(iter(new_dirs))
    transcript = session_dir / "transcript.txt"
    assert transcript.exists(), f"transcript.txt not written to {session_dir}"
