# Enlistra Reels: dashboard + scheduler + pipeline in one container.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Piper voice (MIT licensed, from the piper-voices repo on Hugging Face).
RUN mkdir -p assets/voices && cd assets/voices \
    && curl -fsSL -o en_US-ryan-high.onnx "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high.onnx" \
    && curl -fsSL -o en_US-ryan-high.onnx.json "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high.onnx.json"

COPY . .
# Music and SFX are committed pre-processed (assets/music, assets/sfx); nothing to fetch.
RUN mkdir -p work/out work/src work/analysis

ENV PYTHONUNBUFFERED=1 RENDER=1 PORT=10000
EXPOSE 10000
CMD ["python", "dashboard.py"]
