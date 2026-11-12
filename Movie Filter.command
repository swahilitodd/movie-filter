#!/bin/bash
# Movie Filter launcher for macOS. Double-click to run (right-click > Open the first time).
cd "$(dirname "$0")" || exit 1
fail() { echo; echo "$1"; echo; echo "Press Return to close."; read -r; exit 1; }

# ffmpeg cuts out the audio clips.
if ! command -v ffmpeg >/dev/null; then
  command -v brew >/dev/null || fail "Please install Homebrew from https://brew.sh, then run this again."
  echo "Installing ffmpeg..."
  brew install ffmpeg || fail "Could not install ffmpeg."
fi

# Supported Python versions: the speech recognizer has no builds for 3.15 or later yet.
supported() { "$1" -c 'import sys; sys.exit(not (3, 12) <= sys.version_info[:2] <= (3, 14))' 2>/dev/null; }

PY=""
for cand in python3.13 python3.12 python3.14 python3; do
  if command -v "$cand" >/dev/null && supported "$cand"; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  fail "Movie Filter needs Python 3.12, 3.13 or 3.14 (Python 3.15 and later are not supported yet).
Install Python 3.13 with:   brew install python@3.13 python-tk@3.13
or the macOS installer from https://www.python.org/downloads/ . It can sit alongside other versions."
fi
if ! "$PY" -c "import tkinter" 2>/dev/null; then
  VER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  fail "$PY has no window support. Run:   brew install python-tk@$VER"
fi

# Rebuild the setup if it was made with an unsupported Python.
if [ -x .venv/bin/python ] && ! supported .venv/bin/python; then
  echo "Rebuilding setup with $PY..."
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  echo "First-time setup with $PY (this takes a few minutes)..."
  "$PY" -m venv .venv || fail "Could not create the setup folder."
fi
# Install or update the libraries whenever requirements.txt changes.
if ! cmp -s requirements.txt .venv/requirements.installed; then
  .venv/bin/python -m pip install --upgrade pip &&
  .venv/bin/python -m pip install --upgrade --only-binary=av,ctranslate2,onnxruntime,tokenizers -r requirements.txt ||
    fail "Setup failed. See 'Troubleshooting' in README.txt."
  cp requirements.txt .venv/requirements.installed
fi

.venv/bin/python movie_filter.py "$@"
