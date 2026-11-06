#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
py="${SEO_QA_BOOTSTRAP_PYTHON:-python3.12}"
"$py" -c 'import sys; assert sys.version_info[:2]==(3,12), "Use Python 3.12"'
[ -x .venv/bin/python ] || "$py" -m venv .venv
.venv/bin/python -c 'import sys; from pathlib import Path; assert "include-system-site-packages = false" in (Path(sys.prefix)/"pyvenv.cfg").read_text()'
.venv/bin/python -m pip install -r requirements-runtime.lock
.venv/bin/python -m pip install -r requirements-demo.lock
.venv/bin/python -m pip check
