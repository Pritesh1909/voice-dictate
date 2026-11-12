import os
import wave
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from parakeet_dictate import recordings
from parakeet_dictate.audio import SR
from parakeet_dictate.recordings import Record

NOW = datetime(2026, 11, 12, 15, 30, 0)


def rec_at(when, **kw):
    fields = dict(id=recordings.new_id(when), time=when.isoformat(timespec="seconds"), exe="notepad.exe",
                  raw="hello wurld", phrases=["hello wurld"], typed="Hello world.", model="parakeet-v2",
                  language="en")
    fields.update(kw)
    return Record(**fields)


def tone(seconds=1.0):
    t = np.arange(int(SR * seconds)) / SR
    return (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def names(folder):
    return sorted(p.name for p in folder.iterdir())


def test_new_id_format():
    assert recordings.new_id(datetime(2026, 11, 12, 9, 5, 3, 123456)) == "20261112-090503-123456"
    assert recordings.new_id(datetime(2026, 1, 2, 3, 4, 5)) == "20260102-030405-000000"


def test_save_and_list_round_trip(tmp_path):
    older = rec_at(NOW - timedelta(hours=2), raw="naïve café — “quotes”", phrases=["naïve café", "two"])
    newer = rec_at(NOW - timedelta(hours=1))
    audio = tone()
    recordings.save(tmp_path, audio, newer)          # saved out of order on purpose
    recordings.save(tmp_path, tone(0.5), older)

    assert recordings.list_records(tmp_path) == [older, newer]   # oldest first, every field intact

    loaded = recordings.load_audio(tmp_path, newer.id)
    assert loaded.dtype == np.float32
    assert loaded.shape == audio.shape
    assert np.max(np.abs(loaded - audio)) < 1e-4
    assert recordings.load_audio(tmp_path, older.id).shape == (SR // 2,)

    with wave.open(str(tmp_path / f"{newer.id}.wav")) as w:       # 16 kHz mono 16-bit, as the checker expects
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
    assert not [n for n in names(tmp_path) if n.endswith(".tmp")]  # no half-written leftovers


def test_loud_audio_is_clipped_not_wrapped(tmp_path):
    rec = rec_at(NOW)
    recordings.save(tmp_path, np.array([2.0, -2.0, 1.0, -1.0, 0.0], np.float32), rec)
    got = recordings.load_audio(tmp_path, rec.id)
    assert got[0] > 0.99 and got[1] <= -0.99 and got[4] == 0.0


def test_json_is_written_last(tmp_path, monkeypatch):
    order = []
    real = os.replace
    monkeypatch.setattr(recordings.os, "replace", lambda a, b: (order.append(os.path.basename(b)), real(a, b))[1])
    rec = rec_at(NOW)
    recordings.save(tmp_path, tone(0.1), rec)
    assert order == [f"{rec.id}.wav", f"{rec.id}.json"]


def test_failed_save_leaves_no_partial_files(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(recordings.os, "replace", boom)
    with pytest.raises(OSError):
        recordings.save(tmp_path, tone(0.1), rec_at(NOW))
    assert names(tmp_path) == []          # no .tmp, no .wav, and so no .json that could point at nothing


def test_prune_by_age(tmp_path):
    old = rec_at(NOW - timedelta(days=15))
    edge = rec_at(NOW - timedelta(days=14))
    fresh = rec_at(NOW - timedelta(days=13))
    for r in (old, edge, fresh):
        recordings.save(tmp_path, tone(0.1), r)

    assert recordings.prune(tmp_path, keep_days=14, max_mb=1500, now=NOW) == 1

    assert [r.id for r in recordings.list_records(tmp_path)] == [edge.id, fresh.id]
    assert not (tmp_path / f"{old.id}.wav").exists() and not (tmp_path / f"{old.id}.json").exists()
    assert recordings.prune(tmp_path, keep_days=14, max_mb=1500, now=NOW) == 0


def test_prune_by_size_oldest_first(tmp_path):
    recs = [rec_at(NOW - timedelta(hours=h)) for h in (3, 2, 1)]       # oldest first
    for r in recs:
        recordings.save(tmp_path, np.zeros(SR * 40, np.float32), r)    # about 1.2 MB of WAV each

    assert recordings.prune(tmp_path, keep_days=14, max_mb=3, now=NOW) == 1      # 3.7 MB -> 2.4 MB
    assert [r.id for r in recordings.list_records(tmp_path)] == [recs[1].id, recs[2].id]

    assert recordings.prune(tmp_path, keep_days=14, max_mb=2, now=NOW) == 1      # 2.4 MB -> 1.2 MB
    assert [r.id for r in recordings.list_records(tmp_path)] == [recs[2].id]
    assert recordings.prune(tmp_path, keep_days=14, max_mb=2, now=NOW) == 0


def test_prune_also_clears_half_saved_files(tmp_path):
    old = recordings.new_id(NOW - timedelta(days=20))
    (tmp_path / f"{old}.wav").write_bytes(b"RIFF")              # saved wav, died before the json
    (tmp_path / f"{old}.json.tmp").write_text("{")
    (tmp_path / "notes.txt").write_text("not mine")
    assert recordings.prune(tmp_path, keep_days=14, max_mb=1500, now=NOW) == 1
    assert names(tmp_path) == ["notes.txt"]


def test_prune_accepts_an_aware_now(tmp_path):
    recordings.save(tmp_path, tone(0.1), rec_at(NOW - timedelta(days=15)))
    assert recordings.prune(tmp_path, keep_days=14, max_mb=1500, now=NOW.replace(tzinfo=timezone.utc)) == 1


def test_orphan_json_and_bad_json_skipped(tmp_path):
    good = rec_at(NOW - timedelta(hours=1))
    recordings.save(tmp_path, tone(0.1), good)

    orphan = rec_at(NOW - timedelta(hours=2))                          # .json without its .wav
    recordings.save(tmp_path, tone(0.1), orphan)
    (tmp_path / f"{orphan.id}.wav").unlink()

    def damaged(hours, text):                                          # a .wav with a damaged .json
        i = recordings.new_id(NOW - timedelta(hours=hours))
        (tmp_path / f"{i}.wav").write_bytes(b"RIFF")
        (tmp_path / f"{i}.json").write_text(text, "utf-8")

    damaged(3, "{not json")
    damaged(4, "")
    damaged(5, "[1, 2]")
    damaged(6, '{"id": "x"}')                                          # fields missing
    damaged(7, '{"id": 1, "time": "t", "exe": "e", "raw": "r", "phrases": "oops", "typed": "t", '
               '"model": "m", "language": "en"}')                      # fields of the wrong type
    (tmp_path / "20260101-000000-000000.wav").write_bytes(b"RIFF")     # a .wav with no .json at all
    (tmp_path / "bytes.json").write_bytes(b"\xff\xfe\x00")             # not even UTF-8

    assert recordings.list_records(tmp_path) == [good]


def test_missing_folder_is_empty_not_an_error(tmp_path):
    gone = tmp_path / "nope"
    assert recordings.list_records(gone) == []
    assert recordings.prune(gone, keep_days=14, max_mb=1500, now=NOW) == 0
    assert recordings.delete_all(gone) == 0
    assert recordings.load_audio(gone, "20260101-000000-000000") is None


@pytest.mark.parametrize("keep", [0, 20, 44, 45, 20000])    # inside the header / just past it / mid-audio
def test_truncated_wav_returns_none(tmp_path, keep):
    rec = rec_at(NOW)
    recordings.save(tmp_path, tone(), rec)
    wav = tmp_path / f"{rec.id}.wav"
    wav.write_bytes(wav.read_bytes()[:keep])
    assert recordings.load_audio(tmp_path, rec.id) is None
    assert recordings.list_records(tmp_path) == [rec]      # the record itself is still listed


def test_load_audio_rejects_missing_garbage_and_foreign_formats(tmp_path):
    assert recordings.load_audio(tmp_path, "missing") is None
    (tmp_path / "junk.wav").write_bytes(b"this is not a wav file at all, just text" * 5)
    assert recordings.load_audio(tmp_path, "junk") is None
    with wave.open(str(tmp_path / "stereo.wav"), "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(SR)
        w.writeframes(b"\0" * 400)
    with wave.open(str(tmp_path / "cd.wav"), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(44100)
        w.writeframes(b"\0" * 400)
    assert recordings.load_audio(tmp_path, "stereo") is None
    assert recordings.load_audio(tmp_path, "cd") is None


def test_delete_all_leaves_other_files(tmp_path):
    for h in (1, 2):
        recordings.save(tmp_path, tone(0.1), rec_at(NOW - timedelta(hours=h)))
    for other in ("notes.txt", "state.json", "checked.jsonl", "sample.wav"):
        (tmp_path / other).write_text("keep me")

    assert recordings.delete_all(tmp_path) == 2
    assert names(tmp_path) == ["checked.jsonl", "notes.txt", "sample.wav", "state.json"]
    assert recordings.list_records(tmp_path) == []
    assert recordings.delete_all(tmp_path) == 0
