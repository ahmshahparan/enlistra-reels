#!/bin/bash
# Enlistra Reels one-time setup for macOS. Everything installed here is open source and free.
# Run:  bash setup_mac.sh
set -e
cd "$(dirname "$0")"

say() { printf "\n\033[1;36m==> %s\033[0m\n" "$1"; }

say "Homebrew"
if ! command -v brew >/dev/null; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/usr/local/bin/brew shellenv 2>/dev/null || /opt/homebrew/bin/brew shellenv)"
fi

say "ffmpeg + python"
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12

say "Python virtualenv + packages"
PY=$(brew --prefix python@3.12)/bin/python3.12
[ -d .venv ] || "$PY" -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

say "Ollama (local LLM, replaces paid API)"
if ! command -v ollama >/dev/null; then
  brew install ollama
fi
if ! pgrep -x ollama >/dev/null; then
  (ollama serve >/dev/null 2>&1 &)
  sleep 4
fi
ollama list 2>/dev/null | grep -q "llama3.1:8b" || ollama pull llama3.1:8b

say "Font check"
FONT="/System/Library/Fonts/Supplemental/Arial Bold.ttf"
[ -f "$FONT" ] || FONT="/System/Library/Fonts/Helvetica.ttc"
sed -i '' "s|^  font: .*|  font: $FONT|" config.yaml

say "Smoke test: one Chaplin short, render only (2 to 4 minutes)"
python cli.py run --id silent-the-count --no-post

say "Done. Reels are in work/out/. Next: open README.md, section 'Environment variables'."
