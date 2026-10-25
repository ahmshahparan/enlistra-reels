#!/bin/bash
# First TikTok post, end to end. Run after setup_mac.sh has finished:
#   bash go.sh
set -e
cd "$(dirname "$0")"
eval "$(/usr/local/bin/brew shellenv 2>/dev/null || /opt/homebrew/bin/brew shellenv)"

if [ ! -x .venv/bin/python ]; then
  echo "==> Creating virtualenv (1 to 2 minutes)"
  PY=$(brew --prefix python@3.12)/bin/python3.12
  "$PY" -m venv .venv
  . .venv/bin/activate
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
else
  . .venv/bin/activate
  pip install -q -r requirements.txt
fi

FONT="/System/Library/Fonts/Supplemental/Arial Bold.ttf"
[ -f "$FONT" ] || FONT="/System/Library/Fonts/Helvetica.ttc"
grep -q "$FONT" config.yaml || sed -i '' "s|^  font: .*|  font: $FONT|" config.yaml

if ! pgrep -x ollama >/dev/null; then (ollama serve >/dev/null 2>&1 &); sleep 3; fi

# audio kit (public domain music, CC0 sfx) and narrator voice, fetched once
if [ ! -d assets/sfx ] || [ -z "$(ls assets/music 2>/dev/null)" ]; then
  echo "==> Fetching public domain music and CC0 sound effects (one time, ~60 MB)"
  python -m enlistra.assets
fi
if [ ! -f assets/voices/en_US-ryan-high.onnx ]; then
  echo "==> Fetching narrator voice (one time, ~120 MB)"
  mkdir -p assets/voices
  B="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high"
  curl -sS -L -o assets/voices/en_US-ryan-high.onnx "$B.onnx"
  curl -sS -L -o assets/voices/en_US-ryan-high.onnx.json "$B.onnx.json"
fi

if [ ! -f work/tiktok_token.json ]; then
  echo "==> TikTok login (approve in the browser; the code is picked up automatically or paste it here)"
  python tiktok_auth.py
fi

echo "==> Rendering v2 reels for The Count (3 to 6 minutes)"
rm -rf work/out
python cli.py run --id silent-the-count --no-post
for REEL in work/out/*.json; do
  echo "==> Posting $REEL to TikTok (sandbox, private)"
  python cli.py post "$REEL"
done
echo "==> Done. Open the TikTok app as @enlistra_reels and check your profile (private videos)."
