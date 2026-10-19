#!/bin/zsh
# Lucid Warrant launcher for macOS. Double-click to start (first run sets up a private Python environment).
cd "${0:A:h}"
if [ ! -x .venv/bin/python ]; then
  echo "Setting up Lucid Warrant for the first time..."
  if command -v uv >/dev/null; then uv venv --quiet --python 3.13 .venv && uv pip install --quiet --python .venv/bin/python -e .
  else python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e .; fi || { echo "Setup failed."; read -k1; exit 1; }
fi
exec .venv/bin/python -m lucid_warrant.gui
