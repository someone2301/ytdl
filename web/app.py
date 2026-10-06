import hmac
import ipaddress
import os
import secrets
import shutil
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from functools import wraps

import yt_dlp
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for

from video_to_mp3 import build_options, ensure_ffmpeg

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = Path(
    os.environ.get("VIDEO_TO_MP3_OUTPUT_DIR", ROOT / "mp3_downloads" / "webjobs")
).resolve()

ACCESS_CODE = os.environ.get("VIDEO_TO_MP3_ACCESS_CODE", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
MAX_SECONDS = 20 * 60
DELETE_AFTER = 30 * 60
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
QUALITIES = {"96", "128", "160", "192", "256", "320"}

app = Flask(__name__)
app.secret_key = SECRET_KEY or "not-configured"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") != "0",
)

EXECUTOR = ThreadPoolExecutor(max_workers=2)
JOBS = {}
LIMITS = {}
LOCK = threading.Lock()


def client_ip():
    # The app should be reachable publicly only through the Cloudflare Tunnel.
    return request.headers.get("CF-Connecting-IP", request.remote_addr or "unknown")


def take_limit(name, key, maximum, seconds):
    now = time.time()
    with LOCK:
        recent = [t for t in LIMITS.get((name, key), []) if now - t < seconds]
        if len(recent) >= maximum:
            LIMITS[(name, key)] = recent
            return False
        recent.append(now)
        LIMITS[(name, key)] = recent
        return True


def cleanup_old_jobs():
    now = time.time()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK:
        for job_id, job in list(JOBS.items()):
            if job["state"] in {"done", "error"} and now - job["created"] > DELETE_AFTER:
                shutil.rmtree(OUTPUT_ROOT / job_id, ignore_errors=True)
                JOBS.pop(job_id, None)


@app.before_request
def check_setup_and_cleanup():
    if not ACCESS_CODE or not SECRET_KEY:
        return "Server is not configured: set VIDEO_TO_MP3_ACCESS_CODE and SECRET_KEY.", 503
    cleanup_old_jobs()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.context_processor
def add_template_helpers():
    return {"csrf_token": csrf_token}


def valid_csrf():
    submitted = request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    return bool(submitted and expected and hmac.compare_digest(submitted, expected))


def require_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def validate_video_url(value):
    if not value or len(value) > 2048:
        raise ValueError("Enter a valid YouTube video link.")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise ValueError("Enter a valid YouTube video link.")

    if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError("Use an HTTPS link from youtube.com or youtu.be.")
    if parsed.username or parsed.password or port not in (None, 443):
        raise ValueError("That link is not allowed.")
    query = parse_qs(parsed.query)
    if "list" in query or parsed.path.startswith("/playlist"):
        raise ValueError("Playlist links are not allowed.")

    # Reject DNS results that point at localhost or a private network.
    try:
        addresses = {
            ipaddress.ip_address(item[4][0].split("%")[0])
            for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError):
        raise ValueError("Could not verify that link. Try again later.")
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("That link is not allowed.")

    return value


def set_job(job_id, **changes):
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(changes)


def convert_job(job_id, video_url, quality, audio_format, sample_rate, bit_depth):
    job_dir = OUTPUT_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    set_job(job_id, state="running", progress=0, message="Checking video…")

    try:
        if not ensure_ffmpeg():
            raise RuntimeError("ffmpeg is unavailable")

        options = build_options(job_dir, quality, playlist=False, embed_art=False)
        extract_audio = next(
            pp for pp in options["postprocessors"]
            if pp["key"] == "FFmpegExtractAudio"
        )
        extract_audio["preferredcodec"] = audio_format

        if audio_format == "wav":
            extract_audio.pop("preferredquality", None)

        ffmpeg_args = []
        if sample_rate:
            ffmpeg_args.extend(["-ar", str(sample_rate)])
        if audio_format == "wav":
            ffmpeg_args.extend(["-acodec", f"pcm_s{bit_depth}le"])
        if ffmpeg_args:
            options["postprocessor_args"] = {
                "extractaudio+ffmpeg_o": ffmpeg_args
            }

        options.update({
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "noplaylist": True,
            "max_filesize": 150 * 1024 * 1024,
        })

        def progress_hook(data):
            if data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                percent = int(data.get("downloaded_bytes", 0) * 100 / total) if total else 0
                set_job(job_id, progress=min(percent, 99), message="Downloading and converting…")
            elif data.get("status") == "finished":
                set_job(job_id, progress=99, message="Finishing audio…")

        options["progress_hooks"] = [progress_hook]

        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(video_url, download=False)
            if not isinstance(info, dict) or info.get("_type") == "playlist" or info.get("entries"):
                raise ValueError("Playlist links are not allowed.")
            duration = info.get("duration")
            if duration is None or duration > MAX_SECONDS:
                raise ValueError("Video duration is unknown or exceeds 20 minutes.")
            downloader.download([video_url])

        audio_files = list(job_dir.glob(f"*.{audio_format}"))
        if not audio_files:
            raise RuntimeError(f"No {audio_format.upper()} file was created")

        set_job(
            job_id,
            state="done",
            progress=100,
            message="Ready to download.",
            file=str(audio_files[0]),
        )
    except ValueError as exc:
        set_job(job_id, state="error", message=str(exc))
    except Exception:
        app.logger.exception("Conversion failed")
        set_job(job_id, state="error", message="Conversion failed. Check the link and try again.")

@app.get("/")
def index():
    return render_template("index.html", message=None)


@app.post("/login")
def login():
    if not valid_csrf():
        abort(400)
    if not take_limit("login", client_ip(), 10, 3600):
        return render_template("index.html", message="Too many attempts. Try again later."), 429
    submitted = request.form.get("access_code", "")
    if not hmac.compare_digest(submitted, ACCESS_CODE):
        return render_template("index.html", message="Incorrect access code."), 401
    session.clear()
    session["logged_in"] = True
    return redirect(url_for("index"))


@app.post("/logout")
@require_login
def logout():
    if not valid_csrf():
        abort(400)
    session.clear()
    return redirect(url_for("index"))


@app.post("/convert")
@require_login
def start_conversion():
    if not valid_csrf():
        abort(400)
    if not take_limit("convert", client_ip(), 20, 3600):
        return render_template("index.html", message="Conversion limit reached. Try again later."), 429

    try:
        video_url = validate_video_url(request.form.get("url", "").strip())
    except ValueError as exc:
        return render_template("index.html", message=str(exc)), 400

    audio_format = request.form.get("audio_format", "mp3")
    if audio_format not in {"mp3", "wav"}:
        return render_template("index.html", message="Choose MP3 or WAV."), 400

    quality = request.form.get("quality", "192")
    if audio_format == "mp3" and quality not in QUALITIES:
        return render_template("index.html", message="Choose a listed MP3 bitrate."), 400

    sample_rate = request.form.get("sample_rate", "")
    allowed_sample_rates = {"", "22050", "32000", "44100", "48000"}
    if sample_rate not in allowed_sample_rates:
        return render_template("index.html", message="Choose a listed sample rate."), 400

    bit_depth = request.form.get("bit_depth", "16")
    if audio_format == "wav" and bit_depth not in {"16", "24", "32"}:
        return render_template("index.html", message="Choose a listed WAV bit depth."), 400

    job_id = secrets.token_urlsafe(18)
    with LOCK:
        JOBS[job_id] = {
            "state": "queued",
            "progress": 0,
            "message": "Waiting to start…",
            "created": time.time(),
            "file": None,
        }
    EXECUTOR.submit(
        convert_job, job_id, video_url, quality, audio_format,
        sample_rate or None, bit_depth,
    )
    return render_template("index.html", message=None, job_id=job_id)

@app.get("/status/<job_id>")
@require_login
def job_status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        return jsonify({
            "state": job["state"],
            "progress": job["progress"],
            "message": job["message"],
        })


@app.get("/download/<job_id>")
@require_login
def download(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job or job["state"] != "done" or not job["file"]:
            abort(404)
        file_path = Path(job["file"])
    if not file_path.is_file() or file_path.parent.parent != OUTPUT_ROOT:
        abort(404)
    return send_file(file_path, as_attachment=True, download_name=file_path.name)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
