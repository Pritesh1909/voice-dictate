"""Entry point for the packaged ParakeetDictate.exe (PyInstaller)."""
import os

# Same as parakeet_dictate/__main__.py: keep numpy's OpenBLAS from reserving ~500 MB. Must run before numpy.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import multiprocessing  # noqa: E402

if __name__ == "__main__":
    multiprocessing.freeze_support()  # the model process is started with "spawn": re-enters here and exits
    from parakeet_dictate.__main__ import main
    raise SystemExit(main())
