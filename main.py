import os
import uuid
import asyncio
import time
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import aiofiles
import yt_dlp
from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse, FileResponse,  HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# ── dirs ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
TEMP_DIR   = BASE_DIR / "temp"
OUT_DIR    = BASE_DIR / "outputs"
STATIC_DIR = BASE_DIR / "static"

for d in [TEMP_DIR, OUT_DIR, STATIC_DIR]:
    d.mkdir(exist_ok=True)

# ── large thread pool for parallel work ───────────────────────────────────────
executor = ThreadPoolExecutor(max_workers=8)

app = FastAPI(title="AI Video to MP3 Converter", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

app.mount("/static",  StaticFiles(directory=str(STATIC_DIR)),  name="static")
app.mount("/outputs", StaticFiles(directory=str(OUT_DIR)),      name="outputs")

# ── detect ffmpeg (system OR imageio-ffmpeg bundle) ───────────────────────────
def _find_ffmpeg():
    import shutil as sh
    p = sh.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return None

FFMPEG = _find_ffmpeg()

# ── cleanup helpers ───────────────────────────────────────────────────────────
def _cleanup(*paths):
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass

def _cleanup_old():
    now = time.time()
    for d in [TEMP_DIR, OUT_DIR]:
        for f in d.iterdir():
            try:
                if now - f.stat().st_mtime > 3600:
                    f.unlink(missing_ok=True)
            except Exception:
                pass

# ── models ────────────────────────────────────────────────────────────────────
class URLRequest(BaseModel):
    url: str

class ConvertRequest(BaseModel):
    file_id: str
    bitrate: str = "192"

class DownloadRequest(BaseModel):
    url: str
    quality: str = "best"
    audio_only: bool = False
    bitrate: str = "192"

# ── async thread runner ───────────────────────────────────────────────────────
async def run_in_thread(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, fn, *args)

# ── fast ffmpeg conversion ────────────────────────────────────────────────────
def _ffmpeg_convert(src: Path, out: Path, bitrate: str) -> None:
    if FFMPEG:
        cmd = [
            FFMPEG, "-y", "-i", str(src),
            "-vn",                        # strip video
            "-ar", "44100",               # sample rate
            "-ac", "2",                   # stereo
            "-b:a", f"{bitrate}k",
            "-threads", "0",              # all CPU cores
            str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode()[-500:])
    else:
        # fallback: moviepy bundles its own ffmpeg via imageio-ffmpeg
        from moviepy.editor import VideoFileClip
        clip = VideoFileClip(str(src))
        clip.audio.write_audiofile(str(out), bitrate=f"{bitrate}k", logger=None)
        clip.close()

# ── common yt-dlp options ─────────────────────────────────────────────────────
def _ydl_base():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "concurrent_fragment_downloads": 4,   # parallel HLS/DASH fragments
        "http_chunk_size": 10 * 1024 * 1024,  # 10 MB chunks
    }
    if FFMPEG:
        opts["ffmpeg_location"] = str(Path(FFMPEG).parent)
    return opts

# ═════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═════════════════════════════════════════════════════════════════════════════

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return FileResponse(str(BASE_DIR / "index.html"))

@app.get("/health")
async def health():
    return {"status": "ok", "ffmpeg": FFMPEG or "moviepy-fallback", "version": "2.0.0"}

# ── upload ────────────────────────────────────────────────────────────────────
@app.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    _cleanup_old()
    ALLOWED  = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv"}
    MAX_SIZE = 500 * 1024 * 1024

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Unsupported format: {ext}")

    file_id  = str(uuid.uuid4())
    tmp_path = TEMP_DIR / f"{file_id}{ext}"
    size     = 0

    async with aiofiles.open(tmp_path, "wb") as out:
        while True:
            chunk = await file.read(4 * 1024 * 1024)  # 4 MB read chunks
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_SIZE:
                _cleanup(tmp_path)
                raise HTTPException(413, "File too large (max 500 MB)")
            await out.write(chunk)

    return {"file_id": file_id, "filename": file.filename, "size": size, "extension": ext}

# ── convert ───────────────────────────────────────────────────────────────────
@app.post("/convert")
async def convert_to_mp3(req: ConvertRequest, background_tasks: BackgroundTasks):
    if req.bitrate not in {"64", "128", "192", "256", "320"}:
        raise HTTPException(400, "Invalid bitrate")

    src = None
    for ext in [".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv"]:
        c = TEMP_DIR / f"{req.file_id}{ext}"
        if c.exists():
            src = c
            break
    if not src:
        raise HTTPException(404, "Upload not found. Please re-upload.")

    out_path = OUT_DIR / f"{req.file_id}_{req.bitrate}k.mp3"

    try:
        await run_in_thread(_ffmpeg_convert, src, out_path, req.bitrate)
    except Exception as e:
        _cleanup(out_path)
        raise HTTPException(500, f"Conversion failed: {e}")

    background_tasks.add_task(_cleanup, str(src))

    return {
        "file_id":      req.file_id,
        "bitrate":      req.bitrate,
        "download_url": f"/outputs/{out_path.name}",
        "filename":     out_path.name,
        "size":         out_path.stat().st_size,
    }

# ── fetch URL metadata ────────────────────────────────────────────────────────
@app.post("/fetch-url")
async def fetch_url_info(req: URLRequest):
    opts = {**_ydl_base(), "skip_download": True}

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(req.url, download=False)

    try:
        info = await run_in_thread(_extract)
    except Exception as e:
        raise HTTPException(400, f"Could not fetch URL info: {e}")

    formats, seen = [], set()
    for f in (info.get("formats") or []):
        res = f.get("height")
        if res and res not in seen and f.get("ext") in ("mp4", "webm"):
            seen.add(res)
            formats.append({"resolution": res, "ext": f["ext"],
                             "format_id": f.get("format_id",""), "label": f"{res}p"})
    formats.sort(key=lambda x: x["resolution"])

    dur = int(info.get("duration") or 0)
    m, s = divmod(dur, 60)

    return {
        "title":      info.get("title", "Unknown"),
        "thumbnail":  info.get("thumbnail", ""),
        "duration":   f"{m}:{s:02d}",
        "uploader":   info.get("uploader", ""),
        "view_count": info.get("view_count", 0),
        "formats":    formats,
        "url":        req.url,
    }

# ── download video / audio ────────────────────────────────────────────────────
QUALITY_MAP = {
    "144":  "bestvideo[height<=144][ext=mp4]+bestaudio[ext=m4a]/best[height<=144]",
    "240":  "bestvideo[height<=240][ext=mp4]+bestaudio[ext=m4a]/best[height<=240]",
    "360":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
    "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
    "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
    "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
}

@app.post("/download-video")
async def download_video(req: DownloadRequest, background_tasks: BackgroundTasks):
    _cleanup_old()
    file_id = str(uuid.uuid4())

    if req.audio_only:
        fmt = "bestaudio[ext=m4a]/bestaudio/best"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": req.bitrate,
        }] if FFMPEG else []
    else:
        fmt = QUALITY_MAP.get(req.quality, QUALITY_MAP["best"])
        postprocessors = []

    opts = {
        **_ydl_base(),
        "format":              fmt,
        "outtmpl":             str(TEMP_DIR / f"{file_id}.%(ext)s"),
        "postprocessors":      postprocessors,
        "merge_output_format": "mp4" if not req.audio_only else None,
    }

    title = "download"
    def _dl():
        nonlocal title
        with yt_dlp.YoutubeDL(opts) as ydl:
            info  = ydl.extract_info(req.url, download=True)
            title = info.get("title", "video")

    try:
        await run_in_thread(_dl)
    except Exception as e:
        raise HTTPException(500, f"Download failed: {e}")

    # find the output file (pick largest = merged file)
    candidates = sorted(TEMP_DIR.glob(f"{file_id}.*"),
                        key=lambda p: p.stat().st_size, reverse=True)
    if req.audio_only:
        mp3s = [p for p in candidates if p.suffix.lower() == ".mp3"]
        candidates = mp3s or candidates
    else:
        mp4s = [p for p in candidates if p.suffix.lower() == ".mp4"]
        candidates = mp4s or candidates

    downloaded = next((p for p in candidates if p.is_file()), None)
    if not downloaded:
        raise HTTPException(500, "Downloaded file not found on server.")

    safe  = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:60]
    fname = f"{safe}{downloaded.suffix}"
    fsize = downloaded.stat().st_size

    # ── streaming response with 256 KB chunks for max throughput ──────────────
    async def stream():
        async with aiofiles.open(downloaded, "rb") as f:
            while True:
                chunk = await f.read(262144)   # 256 KB
                if not chunk:
                    break
                yield chunk
        background_tasks.add_task(_cleanup, str(downloaded))

    media_type = "audio/mpeg" if req.audio_only else "video/mp4"
    return StreamingResponse(
        stream(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Content-Length":      str(fsize),
            "Accept-Ranges":       "bytes",
            "Cache-Control":       "no-cache",
        },
    )

@app.post("/download-audio")
async def download_audio(req: DownloadRequest, background_tasks: BackgroundTasks):
    req.audio_only = True
    return await download_video(req, background_tasks)
