from __future__ import annotations

import os

# numpy's OpenBLAS reserves ~32 MB of commit per CPU thread at import (≈500 MB on this laptop).
# We never do matrix maths with numpy, so one thread is enough; set before numpy is imported anywhere.
# The model child process inherits this; transcribe.cpp has its own thread pool ([model] threads).
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import logging  # noqa: E402
import logging.handlers  # noqa: E402
import multiprocessing  # noqa: E402
import sys  # noqa: E402


def main() -> int:
    multiprocessing.freeze_support()
    if len(sys.argv) >= 2 and sys.argv[1] == "--list-devices":
        return list_devices()  # before anything else: no settings, log or folders are touched
    from . import APP_NAME, winutil
    from .config import ConfigError, Paths

    paths = Paths.default()
    paths.ensure()
    if len(sys.argv) >= 2 and sys.argv[1] in ("--correct", "--settings", "--history"):
        # The "Correct last dictation…", Settings and History windows, each its own process while the tray
        # app keeps running (see correct.py for why). They append to the log without rotating it (the tray
        # app owns it).
        logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler(paths.log_file, encoding="utf-8")],
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        if sys.argv[1] == "--settings":
            from . import settings_ui
            return settings_ui.run(paths)
        if len(sys.argv) >= 3:
            from pathlib import Path
            if sys.argv[1] == "--history":
                from . import history_ui
                return history_ui.run(paths, Path(sys.argv[2]))

            from .correct import run
            return run(paths, Path(sys.argv[2]))
        return 2
    handler = logging.handlers.RotatingFileHandler(paths.log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("parakeet_dictate")

    if len(sys.argv) >= 4 and sys.argv[1] == "--selftest":
        return selftest(paths, sys.argv[2], sys.argv[3])

    if not winutil.single_instance():
        log.info("already running")
        return 0
    try:
        from .app import App
        App(paths).run()
    except ConfigError as e:
        log.error("%s", e)
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"{e}\n\nSettings file:\n{paths.config_file}", APP_NAME, 0x10)
        return 1
    except Exception:
        log.exception("fatal")
        raise
    return 0


def list_devices() -> int:
    """ParakeetDictate.exe --list-devices : print the speech engine's devices as JSON, [[type, name], ...]
    (type is "gpu" for a discrete card, "igpu", or "cpu"). The tray app never loads the engine itself, so it
    asks a short-lived process like this one (teacher.has_discrete_gpu)."""
    import json

    import transcribe_cpp as tc
    tc.set_log_callback(None)  # native logging must not end up in the output
    print(json.dumps([[d.device_type, d.name] for d in tc.backends()]))
    return 0


def selftest(paths, wav_path: str, out_path: str) -> int:
    """ParakeetDictate.exe --selftest in.wav out.json : find the model, transcribe, clean, report.
    Checks the whole packaged pipeline (model finder, model process, device, clean-up) without a microphone."""
    import json
    import time
    import wave

    import numpy as np

    from . import config, models
    from .audio import SR, preprocess, resample
    from .engine import Engine, EngineSpec
    from .textclean import Cleaner

    report: dict = {}
    try:
        c = config.load(paths)
        found = models.find(include_wsl=True)
        report["models_found"] = [m.describe() for m in found]
        if not found:
            raise RuntimeError("no speech model found")
        m = found[0]
        spec = EngineSpec.for_model(m, c.threads, device=c.device, vocabulary=c.hotwords)
        with wave.open(wav_path) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
            x = resample(x, w.getframerate(), SR)
        e = Engine(spec, 0)
        t = time.perf_counter()
        raw, secs = e.transcribe(preprocess(x))
        report.update(model=m.describe(), device=e.note, load_and_decode_s=round(time.perf_counter() - t, 2),
                      decode_s=round(secs, 2), raw=raw,
                      clean=Cleaner.from_config(c).clean(raw))
        e.unload()
        report["ok"] = True
    except Exception as ex:
        report.update(ok=False, error=f"{type(ex).__name__}: {ex}")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
