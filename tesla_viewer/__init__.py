"""TeslaCam decrypt-and-view desktop application."""

import os

# NumPy's bundled OpenBLAS reserves per-thread buffers at import time: about
# 480 MiB private memory per process on a 16-thread CPU. This app only uses
# NumPy for light array work, so one BLAS thread keeps the UI process and each
# analysis process small. Set before any submodule imports NumPy; spawned
# analysis processes inherit it.
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

__version__ = "0.1"

# Shown in the 정보 (About) window. Fill in before publishing; empty values
# are simply not shown.
PROJECT_URL = ""   # e.g. "https://github.com/<계정>/MyTeslaViewer" (소스·문의·업데이트)
AUTHOR = ""        # 이름 또는 닉네임
APP_LICENSE = ""   # 이 프로그램 코드의 라이선스, e.g. "MIT"
