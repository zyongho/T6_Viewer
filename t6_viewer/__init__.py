"""T6 Viewer — Tesla dashcam smart viewer."""

import os

# NumPy's bundled OpenBLAS reserves per-thread buffers at import time: about
# 480 MiB private memory per process on a 16-thread CPU. This app only uses
# NumPy for light array work, so one BLAS thread keeps the UI process and each
# analysis process small. Set before any submodule imports NumPy; spawned
# analysis processes inherit it.
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

__version__ = "0.1.1"

# Shown in the 정보 (About) window. Fill in before publishing; empty values
# are simply not shown.
PROJECT_URL = "https://github.com/zyongho/T6_Viewer"  # 소스·문의·업데이트
AUTHOR = "zyongho"
DONATE_URL = "https://buymeacoffee.com/zyongho"  # 후원
APP_LICENSE = "AGPL-3.0-or-later"
