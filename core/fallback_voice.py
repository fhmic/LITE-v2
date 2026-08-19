#fallback_voice.py
"""
A degraded-but-functional voice loop for when Gemini Live is unavailable
(no key, connection down, or quota exhausted) — so LITE isn't reduced to
text-only just because one provider is having a bad day.

This is NOT the same experience as Gemini Live:
  - Turn-based (record → transcribe → think → speak), not full-duplex
    streaming — you can't interrupt mid-sentence the way you can normally.
  - No tool-calling (computer control, search, etc.) in this mode — it's
    conversational only. Building full function-calling parity against a
    second provider is a much bigger project; this covers "I still want to
    talk to LITE" while Gemini is down, not feature parity.
  - Uses whatever text provider IS available: Claude, then Groq, then a
    custom/local endpoint (via core.ai_client) — never Gemini, since if
    Gemini's down/exhausted, that's the whole reason this mode exists.

Speech pipeline, entirely local/offline where possible:
  STT: faster-whisper (offline, downloads once) — falls back to Vosk if
       whisper isn't installed.
  TTS: EdgeTTS (free, needs internet) by default — Kokoro (fully offline)
       or ElevenLabs if configured in api_keys.json.
"""
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

SAMPLE_RATE   = 16000
CHANNELS      = 1
MAX_UTTERANCE_S   = 20      # hard cap per recording, so a stuck mic can't hang forever
SILENCE_MS        = 900     # stop recording after this much continuous quiet
SILENCE_THRESHOLD = 0.015   # RMS below this counts as silence
MAX_IDLE_S        = 45      # auto-pause fallback mode after this long with no speech

EXIT_PHRASES = ("stop listening", "go to sleep", "exit fallback", "stop fallback")


def _load_config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _has_fallback_text_provider() -> bool:
    """True if Claude, Groq, or a custom endpoint is configured — the things this mode needs."""
    cfg = _load_config()
    return bool((cfg.get("anthropic_api_key") or "").strip()) or \
           bool((cfg.get("groq_api_key")      or "").strip()) or \
           bool((cfg.get("fallback_api_url")  or "").strip())


# ── Recording (simple energy-based VAD — no extra dependency) ──────────────

def record_utterance(is_muted=None) -> np.ndarray | None:
    """
    Blocks until the user finishes speaking (or MAX_UTTERANCE_S elapses).
    Returns float32 mono 16kHz audio, or None if nothing was said (or
    muting engaged mid-recording, in which case whatever was captured so
    far is discarded, not just left unsent — muted means muted).
    """
    chunks: list[np.ndarray] = []
    speaking_started = False
    silence_run_ms    = 0
    block_ms          = 30
    block_frames      = int(SAMPLE_RATE * block_ms / 1000)

    q: "list[np.ndarray]" = []
    lock = threading.Lock()

    def callback(indata, frames, time_info, status):
        with lock:
            q.append(indata.copy())

    start = time.time()
    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS,
        dtype="float32", blocksize=block_frames, callback=callback,
    ):
        while time.time() - start < MAX_UTTERANCE_S:
            if is_muted and is_muted():
                return None   # discard — don't send anything captured before muting
            time.sleep(block_ms / 1000)
            with lock:
                pending, q[:] = q, []
            for block in pending:
                rms = float(np.sqrt(np.mean(block ** 2) + 1e-12))
                if rms > SILENCE_THRESHOLD:
                    speaking_started = True
                    silence_run_ms = 0
                    chunks.append(block)
                elif speaking_started:
                    silence_run_ms += block_ms
                    chunks.append(block)
                    if silence_run_ms >= SILENCE_MS:
                        return np.concatenate(chunks).flatten() if chunks else None
                # else: silence before speech even started — discard, keep waiting

    return np.concatenate(chunks).flatten() if chunks else None


def wait_for_speech(should_stop: threading.Event, is_muted=None, max_wait_s: float = MAX_IDLE_S) -> bool:
    """
    Listens quietly until it detects the start of speech. Returns True if
    speech was detected, False if should_stop fired or nothing was said for
    max_wait_s (genuine idle — caller should pause the session in that case).

    While muted, the microphone stream is fully closed, not just ignored —
    and muted time never counts toward max_wait_s or gets mistaken for idle
    timeout, so muting mid-listen pauses quietly rather than ending the
    fallback session.
    """
    block_ms     = 30
    block_frames = int(SAMPLE_RATE * block_ms / 1000)
    idle_start   = time.time()

    while True:
        if should_stop.is_set():
            return False

        if is_muted and is_muted():
            time.sleep(0.2)
            idle_start = time.time()   # muted time doesn't count toward idle timeout
            continue

        if time.time() - idle_start > max_wait_s:
            return False

        # Not muted/stopped/timed out — listen with the mic open until
        # speech is detected, should_stop fires, muting engages, or the
        # overall idle timeout is hit — whichever comes first.
        detected     = threading.Event()
        muted_midway = threading.Event()

        def callback(indata, frames, time_info, status):
            rms = float(np.sqrt(np.mean(indata ** 2) + 1e-12))
            if rms > SILENCE_THRESHOLD:
                detected.set()

        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="float32", blocksize=block_frames, callback=callback,
        ):
            while not detected.is_set():
                if should_stop.is_set():
                    return False
                if is_muted and is_muted():
                    muted_midway.set()
                    break   # close the stream, then re-enter the outer wait-while-muted state
                if time.time() - idle_start > max_wait_s:
                    return False
                time.sleep(0.05)

        if detected.is_set():
            return True
        # else: muted mid-listen — loop back to the top and wait quietly
        # with the mic closed until unmuted (idle_start resets there).


# ── The fallback session ────────────────────────────────────────────────────

class FallbackVoice:
    def __init__(self, ui, assistant_name: str = "LITE"):
        self.ui             = ui
        self.assistant_name = assistant_name
        self._stt           = None
        self._tts           = None
        self._history: list[tuple[str, str]] = []   # [(role, text), ...] — kept short

    def _log(self, msg: str):
        print(f"[FallbackVoice] {msg}")
        try:
            self.ui.write_log(f"VOICE(fallback): {msg}")
        except Exception:
            pass

    def _load_stt(self):
        if self._stt is not None:
            return self._stt
        cfg = _load_config()
        try:
            from core.stt import WhisperSTT
            self._stt = WhisperSTT(model_name=cfg.get("fallback_stt_model", "base"))
        except Exception as e:
            self._log(f"Whisper unavailable ({e}) — trying Vosk...")
            try:
                from core.stt import VoskSTT
                self._stt = VoskSTT()
            except Exception as e2:
                raise RuntimeError(
                    f"No offline speech-to-text engine available (Whisper: {e}; Vosk: {e2}). "
                    f"Install one: pip install faster-whisper  OR  pip install vosk"
                )
        return self._stt

    def _load_tts(self):
        if self._tts is not None:
            return self._tts
        from core.tts import create_tts_player
        cfg = _load_config()
        self._tts = create_tts_player(cfg)
        return self._tts

    def _transcribe(self, audio: np.ndarray) -> str:
        stt = self._load_stt()
        if hasattr(stt, "transcribe"):          # WhisperSTT
            return stt.transcribe(audio).strip()
        # VoskSTT — feed as int16 PCM bytes
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        text, _ = stt.process_chunk(pcm)
        return text.strip()

    def _think(self, user_text: str) -> str:
        from core.ai_client import generate_content
        history_txt = "\n".join(f"{role}: {text}" for role, text in self._history[-6:])
        prompt = (
            f"You are {self.assistant_name}, an efficient, professional, slightly witty AI "
            f"assistant (in the style of Lite from Iron Man). Address the user as 'sir' unless "
            f"told otherwise. You are currently running in a reduced fallback voice mode "
            f"(your primary engine is temporarily unavailable) — conversational only, no tool "
            f"use. Keep responses concise and natural to speak aloud.\n\n"
            f"{history_txt}\n"
            f"User: {user_text}\n"
            f"{self.assistant_name}:"
        )
        # Gemini is deliberately not in this call's chain — if it's down/exhausted,
        # ai_client's own attempt at it will just fail fast and move on to Claude/custom.
        return generate_content(prompt).text.strip()

    def _speak(self, text: str):
        tts = self._load_tts()
        tts.speak(text)

    def run(self, should_stop: threading.Event):
        """
        Blocking loop — call from a dedicated background thread. Returns
        when should_stop is set, or after MAX_IDLE_S of silence (caller can
        decide whether to restart it or leave the app idle).
        """
        if not _has_fallback_text_provider():
            self._log(
                "No fallback text provider configured (need anthropic_api_key or "
                "fallback_api_url in config/api_keys.json) — can't run fallback voice."
            )
            return

        self._log(
            f"Fallback voice active — turn-based, conversational only (no tool use) "
            f"until the primary engine reconnects. Say 'stop listening' to pause it."
        )
        try:
            self._speak(
                f"{self.assistant_name} fallback mode active, sir. I can talk, but tool use "
                f"is unavailable until I reconnect to my primary engine."
            )
        except Exception as e:
            self._log(f"Couldn't announce fallback mode via TTS ({e}) — continuing silently.")

        while not should_stop.is_set():
            is_muted = lambda: getattr(self.ui, "muted", False)

            if not wait_for_speech(should_stop, is_muted=is_muted):
                if should_stop.is_set():
                    break
                self._log("No speech detected for a while — pausing fallback voice.")
                return

            audio = record_utterance(is_muted=is_muted)
            if audio is None or len(audio) < SAMPLE_RATE * 0.3:
                continue

            try:
                text = self._transcribe(audio)
            except Exception as e:
                self._log(f"Transcription failed: {e}")
                continue

            if not text:
                continue

            self._log(f"Heard: {text!r}")
            if any(p in text.lower() for p in EXIT_PHRASES):
                self._log("Exit phrase heard — pausing fallback voice.")
                try:
                    self._speak("Pausing fallback mode, sir.")
                except Exception:
                    pass
                return

            try:
                reply = self._think(text)
            except Exception as e:
                self._log(f"Thinking failed — all fallback providers unavailable too: {e}")
                try:
                    self._speak("Sir, I'm unable to reach any configured provider right now.")
                except Exception:
                    pass
                continue

            self._history.append(("User", text))
            self._history.append((self.assistant_name, reply))

            try:
                self._speak(reply)
            except Exception as e:
                self._log(f"TTS failed ({e}) — reply was: {reply}")
