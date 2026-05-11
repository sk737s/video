# AI Video to MP3 Converter

## Requirements
- Python 3.11+
- No Docker, no FFmpeg binary needed (moviepy handles conversion internally)

## Install & Run

```bash
# 1. Create virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open browser: https://video-db6q.onrender.com

## Features
- 📁 Local video upload (MP4, AVI, MOV, MKV, WEBM) → MP3 conversion (64–320 kbps)
- 🔗 YouTube & Instagram URL → MP3 audio download or video (144p–1080p)
- 🎵 In-browser audio preview after conversion
- 📜 Download history (localStorage)
- 💎 Royal dark gold UI with particles, glassmorphism, animations

## Deploy on Render
This repo includes a Render Blueprint at `render.yaml`.

Option A (recommended):
- Create a new Render Web Service and connect this repo
- Render will read `render.yaml` and set build/start commands automatically

Option B (manual):
- Build command: `pip install -r requirements.txt`
- Start command:  `uvicorn main:app --host 0.0.0.0 --port $PORT`

## Notes
- `moviepy` ships with its own `ffmpeg` binary — no system install needed
- `yt-dlp` audio extraction (MP3) uses its built-in postprocessor
- Temp files auto-delete after 1 hour
- Max upload size: 500 MB
