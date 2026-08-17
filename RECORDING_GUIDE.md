# Recording Guide — Buzzowl

How to record test sessions, where to put audio files, and how to name them so the pipeline picks them up correctly.

---

## Method 1 — Record directly in the tool (recommended)

This is the fastest path. Everything is handled automatically — audio capture, file saving, and pipeline trigger.

**Steps:**

1. Start the server and open the UI:
   ```bash
   docker compose up -d
   source .venv/bin/activate
   python server.py
   # open http://localhost:8000
   ```

2. Click **Start Recording**. The microphone activates and live transcription begins in the left column.

3. Read your script at a natural talking pace. For dialogue scripts (1–5), pause briefly between speaker turns — this helps diarization distinguish speakers. For monologue scripts (6–7), just talk normally.

4. Click **Stop Recording** when done. WhisperX runs the post-pass transcription (takes 20–60 seconds depending on length). Ollama then generates a summary.

5. The session appears automatically in the **Pipeline panel** with status `staged`. The enrichment agent fires in the background within a few seconds.

**Where the files go (automatically):**
```
data/
  raw/{session_id}/
    audio.wav        ← 16 kHz mono WAV, immutable
    transcript.txt   ← WhisperX output, immutable
  staged/{session_id}/
    summary.md       ← Ollama summary
    metadata.json    ← pipeline state (status, entities, etc.)
```

The `session_id` is generated automatically in `YYYYMMDD-HHMMSS` format (e.g. `20260426-143022`).

---

## Method 2 — Record externally, feed via CLI

Use this when you want to record with a different app (QuickTime, Audacity, Voice Memos) or when you have an existing audio file.

### Audio format requirements

WhisperX accepts most common formats via ffmpeg. For best results:

| Property | Required | Recommended |
|---|---|---|
| Format | WAV, MP3, M4A, FLAC, MP4, OGG | WAV |
| Sample rate | Any (resampled internally) | **16 000 Hz** |
| Channels | Any (downmixed internally) | **Mono** |
| Bit depth | Any | 16-bit PCM |
| Duration | — | 60–120 seconds per script |

If you record with QuickTime or Voice Memos, the `.m4a` output works fine — no conversion needed.

### How to record with QuickTime (macOS)

1. Open QuickTime Player → **File → New Audio Recording**
2. Click the dropdown arrow next to the record button and select your microphone
3. Click record, read the script, click stop
4. **File → Save** — name it (see naming convention below), save anywhere convenient
5. Run `transcribe.py` (see below)

### Running transcribe.py

```bash
source .venv/bin/activate

# Basic — auto-detect language, large-v2 model, with diarization and summary
python transcribe.py path/to/your/recording.m4a

# Force language (faster, more accurate)
python transcribe.py path/to/your/recording.m4a --language de
python transcribe.py path/to/your/recording.m4a --language en

# Skip diarization (faster, no HF token needed)
python transcribe.py path/to/your/recording.m4a --no-diarize

# Skip Ollama summary (useful if Ollama is not running)
python transcribe.py path/to/your/recording.m4a --no-summary
```

The CLI writes output to `data/raw/{session_id}/` and `data/staged/{session_id}/` using the same pipeline as the web UI. The session will appear in the Pipeline panel on the next sweep (within 10 minutes, or immediately if you restart the server).

---

## Method 3 — Drop a pre-existing audio file into the pipeline manually

If you have an audio file you want to inject as a session without re-transcribing:

1. Create the session directories:
   ```bash
   SESSION_ID="20260426-120000"   # use any YYYYMMDD-HHMMSS timestamp
   mkdir -p data/raw/$SESSION_ID
   mkdir -p data/staged/$SESSION_ID
   ```

2. Copy your files:
   ```bash
   cp your_recording.wav data/raw/$SESSION_ID/audio.wav
   cp your_transcript.txt data/raw/$SESSION_ID/transcript.txt   # if you have one
   ```

3. Add a minimal `metadata.json` to trigger the pipeline:
   ```bash
   cat > data/staged/$SESSION_ID/metadata.json << EOF
   {
     "session_id": "$SESSION_ID",
     "status": "staged",
     "created_at": "2026-04-26T12:00:00",
     "title": null,
     "entities": null,
     "agent_run_id": null
   }
   EOF
   ```

4. The next pipeline sweep (every 10 minutes, or on server restart) picks it up automatically. Or trigger it immediately via the Pipeline panel's **"Promote now ↑"** button.

---

## File naming convention for test fixtures

When recording the numbered test scripts, use this naming scheme so test automation can find them:

```
tests/fixtures/audio/
  script_01_acme_gmbh.wav          ← Skript 1
  script_02_horizon_logistik.wav   ← Skript 2
  script_03_vertex_analytics.wav   ← Skript 3
  script_04_pipeline_review.wav    ← Skript 4
  script_05_solaris_tech.wav       ← Skript 5
  script_06_apex_digital_en.wav    ← Skript 6 (English monologue)
  script_07_meridian_de.wav        ← Skript 7 (German monologue)
```

To place a recording there:
```bash
# After recording with QuickTime, rename and move:
mv ~/Downloads/Recording.m4a tests/fixtures/audio/script_06_apex_digital_en.m4a

# Or convert to WAV first (optional — transcribe.py handles m4a directly):
ffmpeg -i ~/Downloads/Recording.m4a -ar 16000 -ac 1 tests/fixtures/audio/script_06_apex_digital_en.wav
```

These files are picked up by the Phase 5.5 integration test suite once it is built. Keep them in `tests/fixtures/audio/` and do not commit large WAV files to git — add them to `.gitignore` or use Git LFS.

---

## Tips for good recordings

- **Speak at a natural pace** — do not slow down artificially. WhisperX handles normal speech rate well.
- **Pause briefly between speaker turns** (dialogue scripts 1–5) — a 0.5–1 second pause is enough for diarization to pick up the switch.
- **No pause needed for monologues** (scripts 6–7) — just talk naturally as if dictating notes.
- **Quiet environment** — background noise degrades both transcription accuracy and diarization. A headset microphone is better than a room mic.
- **Language setting** — set `language: de` in `config.yaml` for scripts 1–5 and 7; set `language: en` for script 6. Or leave it on `auto` and WhisperX will detect it (slightly slower).
- **Script length** — each script is designed for ~60–90 seconds at a normal reading pace. Shorter is fine; the pipeline handles any duration.
- **First diarization run** downloads pyannote models (~1 GB). This is a one-time download — subsequent runs use the cache.
