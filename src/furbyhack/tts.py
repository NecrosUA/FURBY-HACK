"""Offline text-to-speech with a Furby-like voice (Piper + pitch effect).

English, Ukrainian and Czech, picked automatically from the text. Voice models live
in <repo>/models, download them once with:
  uv run python -m piper.download_voices --data-dir models \\
      en_US-kristin-medium uk_UA-ukrainian_tts-medium cs_CZ-kasandra-medium
"""

import argparse
import dataclasses
import functools
import io
import logging
import re
import shutil
import subprocess
import wave
import winsound
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from piper import PiperVoice
from piper.config import SynthesisConfig

log = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).resolve().parents[2] / "models"


@dataclass(frozen=True)
class Voice:
    model: str
    speaker: int | None = None  # for multi-speaker models
    # >1 raises pitch and speeds up together. Per voice, so every language ends up at
    # the same Furby pitch (~291 Hz, see VOICES).
    pitch: float = 1.4
    # Piper length_scale: >1 speaks slower without changing pitch. Offsets the speed-up
    # from `pitch`; chosen by ear by the user (1.0 was too fast).
    slowness: float = 1.6


# Female base voices, so all languages sound like the same Furby. The ~291 Hz target was
# tuned by ear on the earlier amy voice (1.4 x its 208 Hz median F0; replaced because it is
# fine-tuned from lessac, whose dataset license is research-only).
# pitch = 291 Hz / this voice's median F0; slowness scales with pitch to keep the tempo.
VOICES: dict[str, Voice] = {
    # Lower (~160 Hz) and natively faster than amy, so both values were matched to amy's
    # output (F0 and duration) on the same texts. TODO: not yet checked by ear
    "en": Voice("en_US-kristin-medium", pitch=1.8, slowness=2.35),
    # speaker 2 = "tetiana" (193 Hz); "lada" (0) is ~304 Hz and barely needs a shift
    "uk": Voice("uk_UA-ukrainian_tts-medium", speaker=2, pitch=1.51, slowness=1.73),
    "cs": Voice("cs_CZ-kasandra-medium", pitch=1.25, slowness=1.43),  # 232 Hz
}
DEFAULT_LANGUAGE = "en"

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
# Letters found in Czech but not in English; plain ASCII Czech falls back to English.
_CZECH = re.compile(r"[ěščřžýáíéůúťďňĚŠČŘŽÝÁÍÉŮÚŤĎŇ]")


# The Ukrainian model knows only Cyrillic letters and punctuation: Latin words and digits
# are silently skipped. Latin words are spelled in Cyrillic (roughly) and numbers become
# plain nominative cardinals (see uk_number).
_UK_WORDS = {"furby": "фербі", "furbies": "фербі", "furbys": "фербі"}
_LATIN_DIGRAPHS = {"sh": "ш", "ch": "ч", "zh": "ж", "th": "т", "ph": "ф", "oo": "у", "ee": "і"}
_LATIN_LETTERS = dict(
    zip(
        "abcdefghijklmnopqrstuvwxyz",
        [
            "а",
            "б",
            "к",
            "д",
            "е",
            "ф",
            "г",
            "х",
            "і",
            "дж",
            "к",
            "л",
            "м",
            "н",
            "о",
            "п",
            "к",
            "р",
            "с",
            "т",
            "у",
            "в",
            "в",
            "кс",
            "і",
            "з",
        ],
        strict=True,
    )
)
_LATIN_WORD = re.compile(r"[A-Za-z]+")


def spell_latin_in_cyrillic(text: str) -> str:
    def word(match: re.Match[str]) -> str:
        w = match.group().lower()
        if w in _UK_WORDS:
            return _UK_WORDS[w]
        out, i = [], 0
        while i < len(w):
            if w[i : i + 2] in _LATIN_DIGRAPHS:
                out.append(_LATIN_DIGRAPHS[w[i : i + 2]])
                i += 2
            else:
                out.append(_LATIN_LETTERS[w[i]])
                i += 1
        return "".join(out)

    return _LATIN_WORD.sub(word, text)


_UK_UNITS = ["нуль", "один", "два", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять"]
_UK_TEENS = [
    "десять",
    "одинадцять",
    "дванадцять",
    "тринадцять",
    "чотирнадцять",
    "п'ятнадцять",
    "шістнадцять",
    "сімнадцять",
    "вісімнадцять",
    "дев'ятнадцять",
]
_UK_TENS = [
    "_",
    "_",
    "двадцять",
    "тридцять",
    "сорок",
    "п'ятдесят",
    "шістдесят",
    "сімдесят",
    "вісімдесят",
    "дев'яносто",
]
_UK_HUNDREDS = [
    "_",
    "сто",
    "двісті",
    "триста",
    "чотириста",
    "п'ятсот",
    "шістсот",
    "сімсот",
    "вісімсот",
    "дев'ятсот",
]
# (one, few, many) forms and whether the group counts in feminine (одна/дві тисячі)
_UK_SCALES = [
    (("", "", ""), False),
    (("тисяча", "тисячі", "тисяч"), True),
    (("мільйон", "мільйони", "мільйонів"), False),
    (("мільярд", "мільярди", "мільярдів"), False),
]
_UK_MAX_DIGITS = 12  # _UK_SCALES goes up to billions ("мільярд")
# A number plus an optional case ending like "1990-х" or "5-ти"; the ending is dropped.
_NUMBER = re.compile(r"\d+(?:-[а-яіїєґ']+)?")


def _uk_below_thousand(n: int, feminine: bool) -> list[str]:
    words = []
    if n >= 100:
        words.append(_UK_HUNDREDS[n // 100])
        n %= 100
    if 10 <= n < 20:
        words.append(_UK_TEENS[n - 10])
        return words
    if n >= 20:
        words.append(_UK_TENS[n // 10])
        n %= 10
    if n:
        words.append({1: "одна", 2: "дві"}.get(n, _UK_UNITS[n]) if feminine else _UK_UNITS[n])
    return words


def uk_number(n: int) -> str:
    """Ukrainian cardinal in the nominative case: 1990 -> "тисяча дев'ятсот дев'яносто".

    Deliberately simple: no ordinals or other cases, so "1990-х" reads as the plain number.
    """
    if n == 0:
        return _UK_UNITS[0]
    words: list[str] = []
    for scale, ((one, few, many), feminine) in reversed(list(enumerate(_UK_SCALES))):
        group = n // 1000**scale % 1000
        if not group:
            continue
        group_words = _uk_below_thousand(group, feminine)
        if scale:
            last2, last = group % 100, group % 10
            if last == 1 and last2 != 11:
                form = one
                if group == 1:
                    group_words = []  # "тисяча", not "одна тисяча"
            elif 2 <= last <= 4 and not 12 <= last2 <= 14:
                form = few
            else:
                form = many
            group_words.append(form)
        words.extend(group_words)
    return " ".join(words)


def spell_numbers_uk(text: str) -> str:
    def number(match: re.Match[str]) -> str:
        digits = match.group().split("-")[0]
        if len(digits) > _UK_MAX_DIGITS:  # beyond billions: read digit by digit
            return " ".join(uk_number(int(d)) for d in digits)
        return uk_number(int(digits))

    return _NUMBER.sub(number, text)


# Reported by the user: the voices audibly "say" a "!" (kristin adds up to ~0.3 s of
# sound for it). A period keeps the sentence end; "?" keeps its question intonation.
_EXCLAMATION = re.compile(r"[!?！？]*[!！][!?！？]*")


def calm_exclamations(text: str) -> str:
    return _EXCLAMATION.sub(lambda m: "?" if re.search("[?？]", m[0]) else ".", text)


def detect_language(text: str) -> str:
    if _CYRILLIC.search(text):
        return "uk"
    if _CZECH.search(text):
        return "cs"
    return DEFAULT_LANGUAGE


def voice_for(text: str) -> Voice:
    return VOICES[detect_language(text)]


@functools.cache
def _load_voice(name: str) -> PiperVoice:
    path = MODELS_DIR / f"{name}.onnx"
    if not path.exists():
        raise FileNotFoundError(f"Voice model not found: {path} (see tts.py docstring)")
    return PiperVoice.load(path)


def furby_effect(samples: np.ndarray, factor: float) -> np.ndarray:
    """Band-limited resample to len/factor samples.

    Played at the original rate this is `factor` times higher and faster.
    FFT truncation drops content above the new Nyquist, so no aliasing.
    """
    n_in = len(samples)
    n_out = max(1, round(n_in / factor))
    spectrum = np.fft.rfft(samples)[: n_out // 2 + 1]
    return np.fft.irfft(spectrum, n=n_out) * (n_out / n_in)


def to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def synthesize(text: str, voice: Voice | None = None) -> bytes:
    """Text -> Furby-voiced WAV bytes at the voice's native rate. Blocking."""
    voice = voice or voice_for(text)
    samples, rate = _piper(text, voice)
    return to_wav(furby_effect(samples, voice.pitch), rate)


FURBY_RATE = 16000  # A1800 / DLC audio rate
# The Furby hissed at the end of a 16 s phrase that stopped abruptly and touched full
# scale. Hypothesis: headroom plus a short silent tail avoids that. Cheap either way.
FURBY_PEAK = 0.9
FURBY_LEAD_S = 0.05
FURBY_TAIL_S = 0.25


def synthesize_for_furby(text: str, voice: Voice | None = None) -> bytes:
    """Text -> Furby-voiced WAV at 16 kHz, ready for dlc.encode_track. Blocking."""
    return furby_wav(furby_samples(text, voice))


def furby_samples(text: str, voice: Voice | None = None) -> np.ndarray:
    """Text -> Furby-voiced float samples at 16 kHz, not yet normalized or padded."""
    voice = voice or voice_for(text)
    samples, rate = _piper(text, voice)
    # One resample does both the pitch effect and the rate change to 16 kHz.
    return furby_effect(samples, voice.pitch * rate / FURBY_RATE)


def furby_wav(samples: np.ndarray) -> bytes:
    """16 kHz samples -> normalized, padded WAV for the Furby speaker."""
    return to_wav(for_furby_speaker(samples), FURBY_RATE)


def for_furby_speaker(samples: np.ndarray) -> np.ndarray:
    """Normalize to FURBY_PEAK and pad with a little silence at both ends."""
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    if peak > 0:
        samples = samples * (FURBY_PEAK / peak)
    lead = np.zeros(round(FURBY_LEAD_S * FURBY_RATE))
    tail = np.zeros(round(FURBY_TAIL_S * FURBY_RATE))
    return np.concatenate([lead, samples, tail])


def _piper(text: str, voice: Voice) -> tuple[np.ndarray, int]:
    if not text.strip():
        raise ValueError("Nothing to say")
    piper_voice = _load_voice(voice.model)
    text = calm_exclamations(text)
    if str(piper_voice.config.phoneme_type).lower().endswith("text"):
        # Text-based models (the Ukrainian one) know only lowercase Cyrillic letters.
        text = spell_latin_in_cyrillic(spell_numbers_uk(text)).lower()
    config = SynthesisConfig(speaker_id=voice.speaker, length_scale=voice.slowness)
    chunks = [chunk.audio_float_array for chunk in piper_voice.synthesize(text, config)]
    if not chunks:  # e.g. punctuation only
        raise ValueError("Nothing to say")
    return np.concatenate(chunks), piper_voice.config.sample_rate


def load_audio(path: Path, start_s: float, end_s: float) -> np.ndarray:
    """Any file ffmpeg can read (mp3, wav, ...) -> mono float samples at 16 kHz."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("ffmpeg not found; install it with: winget install Gyan.FFmpeg")
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-ss", str(start_s), "-to", str(end_s), "-i", str(path)]
        + ["-ac", "1", "-ar", str(FURBY_RATE), "-f", "s16le", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode(errors='replace').strip()}")
    pcm = result.stdout
    return np.frombuffer(pcm, "<i2").astype(np.float64) / 32768


SONG_FADE_OUT = 0.5  # seconds, before pitching


def song_wav(samples: np.ndarray, pitch: float) -> bytes:
    """16 kHz song -> Furby version, kept simple: `pitch` times higher and faster (plain
    resampling, like the voice), normalized and padded like speech."""
    fade = min(len(samples), round(SONG_FADE_OUT * FURBY_RATE))
    samples = samples.copy()
    samples[len(samples) - fade :] *= np.linspace(1.0, 0.0, fade)  # a hard cut sounds broken
    return furby_wav(furby_effect(samples, pitch))


def pcm_of(wav: bytes) -> bytes:
    """Raw int16 frames of a WAV."""
    with wave.open(io.BytesIO(wav)) as w:
        return w.readframes(w.getnframes())


def envelope(wav: bytes, step_s: float) -> list[float]:
    """Loudness per step_s window, normalized to 0..1. Drives the antenna while speaking."""
    with wave.open(io.BytesIO(wav)) as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
    step = max(1, round(rate * step_s))
    n = len(pcm) // step
    if n == 0:
        return []
    rms = np.sqrt(np.mean(pcm[: n * step].reshape(n, step) ** 2, axis=1))
    peak = rms.max()
    return (rms / peak).tolist() if peak > 0 else [0.0] * n


def play(wav: bytes) -> None:
    """Play on the PC speakers. Blocks until done."""
    winsound.PlaySound(wav, winsound.SND_MEMORY)


# --- Dev tool for tuning the voice: `uv run python -m furbyhack.tts "Hello"` ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Speak text in the Furby voice")
    parser.add_argument("text")
    parser.add_argument("--lang", choices=sorted(VOICES), help="default: detect from text")
    parser.add_argument("--model", help="override the Piper model, e.g. en_US-hfc_female-medium")
    parser.add_argument("--speaker", type=int, help="speaker id for multi-speaker models")
    parser.add_argument("--pitch", type=float, help="override the voice's pitch factor")
    parser.add_argument("--slow", type=float, help="override length scale, >1 = slower")
    parser.add_argument("--save", type=Path, help="also write the WAV to this file")
    args = parser.parse_args()

    base = VOICES[args.lang] if args.lang else voice_for(args.text)
    overrides = {
        "model": args.model,
        "speaker": args.speaker,
        "pitch": args.pitch,
        "slowness": args.slow,
    }
    voice = dataclasses.replace(base, **{k: v for k, v in overrides.items() if v is not None})
    print(f"Voice: {voice}")
    wav = synthesize(args.text, voice)
    if args.save:
        args.save.write_bytes(wav)
    play(wav)
