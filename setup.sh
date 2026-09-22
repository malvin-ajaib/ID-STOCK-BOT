#!/usr/bin/env bash
# Bootstrap a local virtual environment for this project.
#
# A venv is NOT portable — its `python` is a symlink to whatever interpreter
# created it, and pyvenv.cfg / activate bake in absolute paths. So we never
# commit the venv; each machine builds its own here from requirements.txt.
#
# Usage:
#   ./setup.sh                # uses the first python3 on PATH
#   PYTHON=/path/to/python3 ./setup.sh   # pin a specific interpreter
set -euo pipefail

cd "$(dirname "$0")"

# Pick an interpreter: $PYTHON override -> python3 -> python.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PY="python3"
  elif command -v python >/dev/null 2>&1; then
    PY="python"
  else
    echo "No python3/python found on PATH. Install Python 3 or set PYTHON=/path/to/python3." >&2
    exit 1
  fi
fi

echo "Using interpreter: $("$PY" --version 2>&1) ($(command -v "$PY"))"

# (Re)create the venv. Remove a stale/broken one first so a cloned or moved
# venv with dead symlinks can't linger.
if [ -d venv ] && ! venv/bin/python --version >/dev/null 2>&1; then
  echo "Existing venv is broken (dead interpreter symlink) — recreating."
  rm -rf venv
fi
if [ ! -d venv ]; then
  echo "Creating venv/ ..."
  "$PY" -m venv venv
fi

# Install dependencies into the venv.
echo "Installing dependencies ..."
./venv/bin/python -m pip install --upgrade pip >/dev/null
./venv/bin/python -m pip install -r requirements.txt

echo
echo "Done. Activate it with:"
echo "  source venv/bin/activate"
