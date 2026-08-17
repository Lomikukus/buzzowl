"""Generate a 3-second 440 Hz sine-wave WAV at 16 kHz mono for use as a transcription smoke-test fixture."""
import numpy as np
import scipy.io.wavfile
from pathlib import Path

SAMPLE_RATE = 16_000
DURATION_S = 3
FREQUENCY_HZ = 440

t = np.linspace(0, DURATION_S, SAMPLE_RATE * DURATION_S, endpoint=False)
samples = (np.sin(2 * np.pi * FREQUENCY_HZ * t) * 32767).astype(np.int16)

out = Path(__file__).parent / "tone.wav"
scipy.io.wavfile.write(str(out), SAMPLE_RATE, samples)
print(f"Written: {out}  ({len(samples)} samples, {DURATION_S}s @ {SAMPLE_RATE} Hz)")
