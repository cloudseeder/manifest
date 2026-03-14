"""Local speech-to-text via faster-whisper (CTranslate2)."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

from faster_whisper import WhisperModel

log = logging.getLogger("oap.agent.transcribe")

_model: WhisperModel | None = None
_noise_suppress: bool = True


def init(
    model_size: str = "base",
    device: str = "auto",
    compute_type: str = "auto",
    noise_suppress: bool = True,
) -> None:
    """Load a Whisper model. Call once at startup."""
    global _model, _noise_suppress
    _model = WhisperModel(model_size, device=device, compute_type=compute_type)
    _noise_suppress = noise_suppress
    if noise_suppress:
        try:
            import noisereduce  # noqa: F401
            log.info("Noise suppression enabled (noisereduce)")
        except ImportError:
            log.warning("noisereduce not installed — noise suppression disabled")
            _noise_suppress = False


def _suppress_noise(audio_path: str) -> str | None:
    """Apply noise reduction to an audio file.

    Converts to WAV via ffmpeg, applies noisereduce, writes cleaned WAV.
    Returns path to cleaned file, or None on failure.
    """
    try:
        import numpy as np
        import noisereduce as nr
        import soundfile as sf
    except ImportError:
        return None

    # Convert input to WAV (16kHz mono) for processing
    wav_path = audio_path + ".nr.wav"
    try:
        # Find ffmpeg — Homebrew path not in launchd PATH
        ffmpeg = "ffmpeg"
        for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
            if os.path.exists(p):
                ffmpeg = p
                break
        subprocess.run(
            [ffmpeg, "-i", audio_path, "-ar", "16000", "-ac", "1",
             "-f", "wav", "-y", wav_path],
            capture_output=True, timeout=30,
        )
        if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 100:
            return None

        # Read, suppress noise, write
        data, sr = sf.read(wav_path, dtype="float32")
        reduced = nr.reduce_noise(y=data, sr=sr, stationary=True, prop_decrease=0.4)
        sf.write(wav_path, reduced, sr)
        log.debug("Noise suppression applied: %s", wav_path)
        return wav_path
    except Exception:
        log.warning("Noise suppression failed", exc_info=True)
        # Clean up on failure
        if os.path.exists(wav_path):
            os.unlink(wav_path)
        return None


def transcribe(audio_path: str, language: str | None = None, initial_prompt: str | None = None) -> str:
    """Transcribe an audio file to text. Returns the full transcript."""
    if _model is None:
        raise RuntimeError("Whisper model not loaded — call init() first")

    # Apply noise suppression if enabled
    clean_path = None
    if _noise_suppress:
        clean_path = _suppress_noise(audio_path)

    target = clean_path or audio_path
    kwargs: dict = {"language": language, "beam_size": 5}
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt
    try:
        segments, _ = _model.transcribe(target, **kwargs)
        return " ".join(seg.text.strip() for seg in segments)
    finally:
        if clean_path and os.path.exists(clean_path):
            os.unlink(clean_path)
