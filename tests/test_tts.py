import io
import wave

import numpy as np
import pytest

from furbyhack import tts

SR = 22050


def _sine(freq: float, seconds: float) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return 0.5 * np.sin(2 * np.pi * freq * t)


def _dominant_freq(samples: np.ndarray) -> float:
    spectrum = np.abs(np.fft.rfft(samples))
    return float(np.fft.rfftfreq(len(samples), 1 / SR)[np.argmax(spectrum)])


def test_effect_raises_pitch_and_shortens() -> None:
    src = _sine(440, 1.0)
    out = tts.furby_effect(src, 1.5)
    assert len(out) == round(len(src) / 1.5)
    assert _dominant_freq(out) == pytest.approx(660, abs=3)


def test_effect_keeps_amplitude() -> None:
    out = tts.furby_effect(_sine(440, 1.0), 1.4)
    assert np.max(np.abs(out)) == pytest.approx(0.5, abs=0.02)


def test_to_wav_header() -> None:
    wav = tts.to_wav(_sine(440, 0.1), SR)
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getframerate() == SR
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == int(SR * 0.1)


def test_envelope_follows_loudness() -> None:
    loud = _sine(440, 0.5)
    quiet = loud * 0.25
    silence = np.zeros(SR // 2)
    wav = tts.to_wav(np.concatenate([loud, quiet, silence]), SR)
    levels = tts.envelope(wav, 0.1)
    assert len(levels) == 15
    assert levels[0] == pytest.approx(1.0, abs=0.01)
    assert levels[7] == pytest.approx(0.25, abs=0.02)
    assert levels[12] == 0.0


def test_for_furby_speaker_headroom_and_padding() -> None:
    loud = _sine(440, 1.0) * 2.4  # peaks at 1.2, would clip
    out = tts.for_furby_speaker(loud)
    assert np.max(np.abs(out)) == pytest.approx(tts.FURBY_PEAK, abs=1e-6)
    tail = round(tts.FURBY_TAIL_S * tts.FURBY_RATE)
    lead = round(tts.FURBY_LEAD_S * tts.FURBY_RATE)
    assert len(out) == len(loud) + lead + tail
    assert not out[-tail:].any() and not out[:lead].any()


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Hello, I am Furby!", "en"),
        ("Привіт, я Фербі!", "uk"),
        ("Ahoj, já jsem Furby!", "cs"),
        ("Čau", "cs"),
        ("Ahoj jak se mas", "en"),  # Czech without diacritics can't be told from English
    ],
)
def test_detect_language(text: str, lang: str) -> None:
    assert tts.detect_language(text) == lang


def test_latin_words_spelled_in_cyrillic_for_ukrainian() -> None:
    text = "Furby — іграшка. Furbies відомі. Shop з Chip"
    assert tts.spell_latin_in_cyrillic(text) == "фербі — іграшка. фербі відомі. шоп з чіп"


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (0, "нуль"),
        (7, "сім"),
        (11, "одинадцять"),
        (21, "двадцять один"),
        (100, "сто"),
        (1000, "тисяча"),
        (1990, "тисяча дев'ятсот дев'яносто"),
        (2024, "дві тисячі двадцять чотири"),
        (5000, "п'ять тисяч"),
        (11000, "одинадцять тисяч"),
        (21000, "двадцять одна тисяча"),
        (3_000_000, "три мільйони"),
    ],
)
def test_uk_number(n: int, words: str) -> None:
    assert tts.uk_number(n) == words


def test_spell_numbers_uk_drops_case_endings() -> None:
    assert tts.spell_numbers_uk("у 1990-х роках, 5 котів") == (
        "у тисяча дев'ятсот дев'яносто роках, п'ять котів"
    )


def test_track_duration_uses_track_bitrate() -> None:
    from furbyhack import dlc

    assert dlc.track_duration_ms(dlc.encode_track(bytes(640 * 50), 16000)) == 1000
    assert dlc.track_duration_ms(dlc.encode_track(bytes(640 * 50), 12000)) == 1000


def test_every_language_has_a_voice() -> None:
    assert set(tts.VOICES) == {"en", "uk", "cs"}
    assert tts.DEFAULT_LANGUAGE in tts.VOICES


def test_empty_text_rejected() -> None:
    with pytest.raises(ValueError):
        tts.synthesize("   ")


@pytest.mark.parametrize(
    ("text", "calm"),
    [
        ("Hello I am Furby!", "Hello I am Furby."),
        ("Wow!!! Yes!", "Wow. Yes."),
        ("Really?!", "Really?"),
        ("What!?", "What?"),
        ("Do you want to play?", "Do you want to play?"),
        ("Привіт！", "Привіт."),
    ],
)
def test_exclamations_become_periods(text: str, calm: str) -> None:
    assert tts.calm_exclamations(text) == calm


def test_song_is_higher_faster_and_normalized() -> None:
    song = _sine(200, 3.0)[:48000] * 0.3  # 3 s at 16 kHz (sample rate doesn't matter here)
    wav = tts.song_wav(song, 1.5)
    with wave.open(io.BytesIO(wav)) as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), "<i2") / 32767
        assert w.getframerate() == tts.FURBY_RATE
    assert abs(np.max(np.abs(samples)) - tts.FURBY_PEAK) < 0.01
    lead_and_tail = len(samples) - 32000  # 3 s / 1.5 = 2 s of song + padding
    assert 0 <= lead_and_tail < tts.FURBY_RATE


def test_huge_numbers_are_read_digit_by_digit() -> None:
    # Beyond billions uk_number() would silently drop the high digits.
    assert tts.spell_numbers_uk("1000000000000") == " ".join(
        tts.uk_number(int(d)) for d in "1000000000000"
    )
    assert tts.spell_numbers_uk("1990") == tts.uk_number(1990)
