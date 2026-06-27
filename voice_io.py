"""
voice_io.py
-----------
Voice I/O layer for the negotiation training tool.

Public API:
  check_voice_deps()              — raise RuntimeError if required packages missing
  list_audio_devices()            — print available PortAudio devices
  record_user_input(config)       — record mic until Enter → Gemini STT → str
                                    falls back to text input if STT fails
  speak_ai_response(config, text) — Google Cloud TTS → play audio (any key to skip)

── Authentication ────────────────────────────────────────────────────────────────
  Uses a Google Cloud service account JSON key.
  Set config["google"]["service_account_key"] to the path of your key file.
  The key file must NOT be committed to git (it is in .gitignore).

── STT (speech-to-text) ─────────────────────────────────────────────────────────
  Primary:  Vertex AI Gemini audio understanding — fast, accurate, uses service account.
            pip install google-genai google-auth
  Fallback: faster-whisper (local, offline).
            pip install faster-whisper

── TTS (text-to-speech) ─────────────────────────────────────────────────────────
  Primary:  Google Cloud Text-to-Speech — sub-second, Neural2/Studio voices.
            pip install google-cloud-texttospeech
  Fallback: edge-tts (Microsoft Edge neural voices, no auth needed).
            pip install edge-tts
  Playback: pygame (bundles SDL2, no PortAudio required for output).
            pip install pygame

── Mic recording ────────────────────────────────────────────────────────────────
  sounddevice + numpy — pip install sounddevice numpy
  Needs libportaudio2 at runtime.
  conda: conda install -c conda-forge portaudio (no sudo)

── config.json keys ─────────────────────────────────────────────────────────────
  config["google"]["service_account_key"]  path to JSON key     (required)
  config["google"]["project_id"]           GCP project ID       (required)
  config["google"]["location"]             Vertex AI region     default: "us-central1"
  config["google"]["tts_voice"]            Cloud TTS voice name default: "en-GB-Neural2-C"
  config["google"]["tts_speaking_rate"]    speech rate 0.5–2.0  default: 0.95
  config["voice"]["whisper_model"]         fallback Whisper size default: "tiny"
  config["voice"]["edge_voice"]            fallback TTS voice   default: "en-GB-SoniaNeural"
  config["voice"]["input_device"]          PortAudio device idx default: null
"""

import asyncio
import base64
import io
import os
import sys
import tempfile
import threading
import wave

# ── Optional packages — checked lazily so missing ones don't crash at import ───

try:
    import numpy as np
    import sounddevice as sd
    _SD_AVAILABLE = True
except ImportError:
    _SD_AVAILABLE = False

try:
    from google.oauth2 import service_account as _sa
    import google.auth.transport.requests as _gtr
    _GAUTH_AVAILABLE = True
except ImportError:
    _GAUTH_AVAILABLE = False

try:
    from google import genai as _genai
    from google.genai import types as _genai_types
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

try:
    from google.cloud import texttospeech as _tts_module
    _CLOUD_TTS_AVAILABLE = True
except ImportError:
    _CLOUD_TTS_AVAILABLE = False

try:
    from faster_whisper import WhisperModel as _WhisperModel
    _WHISPER_AVAILABLE = True
except ImportError:
    _WHISPER_AVAILABLE = False

try:
    import edge_tts as _edge_tts_module
    _EDGE_TTS_AVAILABLE = True
except ImportError:
    _EDGE_TTS_AVAILABLE = False

try:
    import pygame
    _PYGAME_AVAILABLE = True
except ImportError:
    _PYGAME_AVAILABLE = False

# ── Audio / Whisper constants ──────────────────────────────────────────────────

RECORD_RATE   = 16_000
CHANNELS      = 1
DTYPE         = "int16"
_whisper_cache: dict = {}


# ── Dependency check ───────────────────────────────────────────────────────────

def check_voice_deps() -> None:
    """
    Check that the minimum packages are installed and PortAudio is reachable.
    Raises RuntimeError with install instructions on failure.
    """
    missing = []
    if not _SD_AVAILABLE:
        missing.append("sounddevice numpy           — mic recording")
    if not _PYGAME_AVAILABLE:
        missing.append("pygame                      — audio playback")
    if not _GAUTH_AVAILABLE:
        missing.append("google-auth                 — Google service account auth")

    # At least one TTS engine must be available
    if not _CLOUD_TTS_AVAILABLE and not _EDGE_TTS_AVAILABLE:
        missing.append("google-cloud-texttospeech   — Google Cloud TTS (primary)")
        missing.append("  OR  edge-tts              — Microsoft Edge TTS (fallback)")

    # At least one STT engine must be available
    if not _GENAI_AVAILABLE and not _WHISPER_AVAILABLE:
        missing.append("google-genai                — Gemini STT on Vertex AI (primary)")
        missing.append("  OR  faster-whisper        — local Whisper STT (fallback)")

    if missing:
        items = "\n    ".join(missing)
        raise RuntimeError(
            f"Voice mode is missing packages:\n    {items}\n\n"
            "Install the full voice stack:\n"
            "    pip install sounddevice numpy pygame google-auth "
            "google-genai google-cloud-texttospeech\n\n"
            "Offline fallbacks (no Google auth):\n"
            "    pip install faster-whisper edge-tts"
        )

    # Probe PortAudio
    try:
        sd.query_devices()
    except Exception as e:
        raise RuntimeError(
            f"PortAudio cannot find any audio devices: {e}\n\n"
            "Fix without sudo:\n"
            "    conda install -c conda-forge portaudio\n"
            "Then confirm: python -c \"import sounddevice; print(sounddevice.query_devices())\""
        ) from e


def list_audio_devices() -> None:
    """Print PortAudio devices — use to find your mic device index."""
    if not _SD_AVAILABLE:
        print("sounddevice not installed.")
        return
    try:
        print(sd.query_devices())
        print(f"\nDefault input  device: {sd.default.device[0]}")
        print(f"Default output device: {sd.default.device[1]}")
    except Exception as e:
        print(f"Could not query devices: {e}")


# ── Internal: WAV helper ───────────────────────────────────────────────────────

def _pcm_to_wav(pcm: bytes, rate: int = RECORD_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


# ── Internal: Google auth ──────────────────────────────────────────────────────

def _get_google_credentials(config: dict):
    """
    Load service account credentials from the key file specified in config.
    The key file path is resolved relative to the script's directory if not absolute.
    """
    if not _GAUTH_AVAILABLE:
        raise RuntimeError("google-auth not installed — run: pip install google-auth")

    google_cfg = config.get("google", {})
    key_path   = google_cfg.get("service_account_key", "service_account_key.json")

    # Resolve relative paths from the directory containing voice_io.py
    if not os.path.isabs(key_path):
        key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), key_path)

    if not os.path.exists(key_path):
        raise RuntimeError(
            f"Service account key not found: {key_path}\n"
            "Set config[\"google\"][\"service_account_key\"] to the correct path."
        )

    return _sa.Credentials.from_service_account_file(
        key_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


# ── Internal: mic recording ────────────────────────────────────────────────────

def _record_until_enter(input_device=None) -> bytes:
    """Record mic until Enter. Returns raw 16-bit mono PCM, or b"" if empty."""
    chunks     = []
    stop_event = threading.Event()

    def _wait():
        input()
        stop_event.set()

    def _callback(indata, frames, time_info, status):
        chunks.append(indata.copy())

    t = threading.Thread(target=_wait, daemon=True)
    t.start()

    with sd.InputStream(samplerate=RECORD_RATE, channels=CHANNELS,
                        dtype=DTYPE, device=input_device,
                        callback=_callback):
        stop_event.wait()

    return b"" if not chunks else np.concatenate(chunks, axis=0).tobytes()


# ── Internal: Gemini STT (Vertex AI) ──────────────────────────────────────────

def _transcribe_gemini(config: dict, pcm: bytes) -> str:
    """
    Send WAV audio to Gemini on Vertex AI for transcription.
    Uses service account credentials — no API key required.
    """
    google_cfg  = config.get("google", {})
    project_id  = google_cfg.get("project_id", "")
    location    = google_cfg.get("location", "us-central1")
    stt_model   = google_cfg.get("stt_model", "gemini-2.0-flash")

    credentials = _get_google_credentials(config)

    client = _genai.Client(
        vertexai=True,
        project=project_id,
        location=location,
        credentials=credentials,
    )

    wav_bytes = _pcm_to_wav(pcm)
    response  = client.models.generate_content(
        model=stt_model,
        contents=[
            _genai_types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
            (
                "Transcribe the spoken words in this audio exactly and verbatim. "
                "Output ONLY the transcription — no preamble, no commentary. "
                "Punctuation corrections are fine. "
                "If the audio is silent or contains no intelligible speech, "
                "output exactly: [SILENCE]"
            ),
        ],
    )

    text = response.text.strip()
    return "" if (text == "[SILENCE]" or not text) else text


# ── Internal: Whisper STT fallback ────────────────────────────────────────────

def _get_whisper_model(model_size: str = "tiny"):
    if model_size not in _whisper_cache:
        print(f"  [Loading Whisper '{model_size}' — downloading on first use…]", flush=True)
        _whisper_cache[model_size] = _WhisperModel(model_size, device="cpu", compute_type="int8")
    return _whisper_cache[model_size]


def _transcribe_whisper(pcm: bytes, model_size: str = "tiny") -> str:
    wav_bytes = _pcm_to_wav(pcm)
    model     = _get_whisper_model(model_size)
    tmp_path  = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            tmp_path = f.name
        segments, _ = model.transcribe(tmp_path, beam_size=1, language="en")
        return " ".join(seg.text for seg in segments).strip()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ── Public STT entry point ─────────────────────────────────────────────────────

def record_user_input(config: dict, prompt: str = "") -> str:
    """
    Record mic until Enter, then transcribe.
    If transcription fails or returns empty, falls back to text input.

    In voice mode you can ALWAYS type instead of speaking — just press Enter
    immediately (to skip recording) and then type at the text fallback prompt.

    Returns the final text string (never empty — loops until something is provided).
    """
    voice_cfg    = config.get("voice",  {})
    input_device = voice_cfg.get("input_device",  None)
    model_size   = voice_cfg.get("whisper_model", "tiny")

    if prompt:
        print(prompt, end="", flush=True)
    print("  [🎙  Speak then press Enter  |  or just press Enter to type]", flush=True)

    pcm = _record_until_enter(input_device=input_device)

    # Empty recording → straight to text fallback
    if not pcm:
        return input("  ✏  Type your message: ").strip()

    # Try Gemini STT first
    if _GENAI_AVAILABLE and _GAUTH_AVAILABLE:
        print("  [Transcribing via Gemini…]", end="\r", flush=True)
        try:
            transcript = _transcribe_gemini(config, pcm)
            print(" " * 40, end="\r", flush=True)
            if transcript:
                return transcript
        except Exception as e:
            print(f"\n  [Gemini STT error: {e}]")

    # Fallback: faster-whisper
    if _WHISPER_AVAILABLE:
        print("  [Transcribing via Whisper…]", end="\r", flush=True)
        try:
            transcript = _transcribe_whisper(pcm, model_size=model_size)
            print(" " * 40, end="\r", flush=True)
            if transcript:
                return transcript
        except Exception as e:
            print(f"\n  [Whisper STT error: {e}]")

    # Both STT engines failed → text input
    print("  [Could not transcribe speech]", flush=True)
    return input("  ✏  Type your message: ").strip()


# ── Internal: interruptible playback ──────────────────────────────────────────

def _play_audio_interruptible(audio_bytes: bytes) -> None:
    """
    Play audio through pygame. Press any key to skip.
    Uses non-blocking keypress detection that does NOT touch stdin,
    so mic recording immediately afterwards works correctly.
    """
    if not pygame.mixer.get_init():
        pygame.mixer.init()

    pygame.mixer.music.load(io.BytesIO(audio_bytes))
    pygame.mixer.music.play()
    print("  [Press any key to skip]", end="\r", flush=True)

    if sys.platform == "win32":
        import msvcrt
        while pygame.mixer.music.get_busy():
            if msvcrt.kbhit():
                msvcrt.getch()
                pygame.mixer.music.stop()
                break
            pygame.time.Clock().tick(10)
    else:
        try:
            import select, tty, termios
            fd  = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            try:
                while pygame.mixer.music.get_busy():
                    if select.select([sys.stdin], [], [], 0.05)[0]:
                        sys.stdin.read(1)
                        pygame.mixer.music.stop()
                        break
                    pygame.time.Clock().tick(10)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            while pygame.mixer.music.get_busy():
                pygame.time.Clock().tick(10)

    print(" " * 25, end="\r", flush=True)


# ── Internal: Google Cloud TTS ────────────────────────────────────────────────

def _speak_google_tts(config: dict, text: str) -> bool:
    """
    TTS via Google Cloud Text-to-Speech using service account credentials.
    Returns True on success, False on failure.

    Recommended voices for a senior British lawyer persona:
      en-GB-Neural2-C    — female, professional, natural    (default)
      en-GB-Neural2-A    — female, clear
      en-GB-Neural2-B    — male, authoritative
      en-GB-Neural2-D    — male, warm
      en-GB-Studio-C     — female, highest quality (Studio tier)
      en-GB-Studio-B     — male, highest quality (Studio tier)
    See full list: https://cloud.google.com/text-to-speech/docs/voices
    """
    if not _CLOUD_TTS_AVAILABLE or not _PYGAME_AVAILABLE:
        return False

    try:
        credentials  = _get_google_credentials(config)
        google_cfg   = config.get("google", {})
        voice_name   = google_cfg.get("tts_voice",        "en-GB-Neural2-C")
        speaking_rate = google_cfg.get("tts_speaking_rate", 0.95)

        client = _tts_module.TextToSpeechClient(credentials=credentials)

        synthesis_input = _tts_module.SynthesisInput(text=text)
        voice_params    = _tts_module.VoiceSelectionParams(
            language_code="en-GB",
            name=voice_name,
        )
        audio_config = _tts_module.AudioConfig(
            audio_encoding=_tts_module.AudioEncoding.MP3,
            speaking_rate=speaking_rate,
        )

        response  = client.synthesize_speech(
            input=synthesis_input,
            voice=voice_params,
            audio_config=audio_config,
        )
        _play_audio_interruptible(response.audio_content)
        return True

    except Exception as e:   # pylint: disable=broad-except
        print(f"\n  [Google TTS error: {e}]")
        return False


# ── Internal: edge-tts fallback ───────────────────────────────────────────────

def _speak_edge(text: str, voice: str = "en-GB-SoniaNeural") -> bool:
    """Fallback TTS via Microsoft Edge neural voices. No auth required."""
    if not _EDGE_TTS_AVAILABLE or not _PYGAME_AVAILABLE:
        return False

    async def _fetch() -> bytes:
        communicate = _edge_tts_module.Communicate(text, voice)
        mp3 = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3 += chunk["data"]
        return mp3

    try:
        mp3_bytes = asyncio.run(_fetch())
        _play_audio_interruptible(mp3_bytes)
        return True
    except Exception as e:
        print(f"\n  [edge-tts error: {e}]")
        return False


# ── Public TTS entry point ─────────────────────────────────────────────────────

def speak_ai_response(config: dict, text: str) -> None:
    """
    Convert text to speech. Tries Google Cloud TTS first, falls back to edge-tts.
    Press any key during playback to skip.
    """
    voice_cfg  = config.get("voice", {})
    edge_voice = voice_cfg.get("edge_voice", "en-GB-SoniaNeural")

    if not _speak_google_tts(config, text):
        if not _speak_edge(text, voice=edge_voice):
            print("  [No TTS available — install google-cloud-texttospeech or edge-tts]")
