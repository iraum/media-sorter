#!/usr/bin/env python3
"""
Media Sorter - A Flask application for browsing a folder of images and
videos as a thumbnail grid and filing them into subfolders with single
keystrokes. Browse, look, press a key, the file moves. Undo if you fumble.

Thumbnails are generated on demand (Pillow for images, ffmpeg for a video
poster frame) and cached on disk, so the grid stays fast on any machine
regardless of what the desktop's own thumbnailer is doing.
"""

import io
import os
import time
import json
import shutil
import hashlib
import logging
import secrets
import mimetypes
import subprocess
import threading
from pathlib import Path
from functools import wraps
from collections import defaultdict

from flask import Flask, render_template, jsonify, request, session, Response, send_file
from PIL import Image, ImageOps

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOME_DIR = Path.home()
APP_DIR = Path(__file__).parent
LOG_FILE = APP_DIR / "security.log"
MOVE_LOG = APP_DIR / "moves.log"          # append-only JSONL audit of every move

# Directories the app may read from and move files within. Override with
# MEDIA_ROOTS (colon-separated). Everything outside these trees is rejected.
_roots_env = os.environ.get("MEDIA_ROOTS", "")
if _roots_env.strip():
    ALLOWED_ROOTS = [Path(p).resolve() for p in _roots_env.split(":") if p.strip()]
else:
    ALLOWED_ROOTS = [Path("/mnt/spielraum").resolve(), HOME_DIR.resolve()]

DEFAULT_START_DIR = Path(os.environ.get("MEDIA_START_DIR", ALLOWED_ROOTS[0]))

# Thumbnail cache. Keyed by path+mtime+size, so an edited file re-thumbnails
# and a moved file keeps its thumb only until it is next seen (cheap to redo).
CACHE_DIR = Path(os.environ.get("MEDIA_CACHE",
                                Path(os.environ.get("XDG_CACHE_HOME", HOME_DIR / ".cache"))
                                / "media-sorter" / "thumbs"))

THUMB_EDGE = 320                         # px, grid tile thumbnails
THUMB_BG = (34, 34, 38)                  # alpha is flattened onto this (matches the dark UI)
MAX_IMAGE_BYTES = 200 * 1024 * 1024      # 200MB source file cap for the image thumbnailer
MAX_PIXELS = 120_000_000                 # decompression-bomb guard (~120MP)
FFMPEG_TIMEOUT = 30                      # seconds per video poster frame
UNDO_DEPTH = 500
RATE_LIMIT_REQUESTS = 1200               # thumbnails come in bursts
RATE_LIMIT_WINDOW = 60                   # seconds

Image.MAX_IMAGE_PIXELS = MAX_PIXELS

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".gif", ".bmp",
    ".tif", ".tiff", ".ppm", ".pgm", ".ico", ".avif", ".heic", ".heif",
}
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpg", ".mpeg",
    ".wmv", ".flv", ".ogv", ".3gp", ".ts", ".mts", ".m2ts",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

if "SECRET_KEY" not in os.environ:
    logger.warning("SECRET_KEY not set - using a random key (sessions reset on restart)")
    app.config["SECRET_KEY"] = secrets.token_hex(32)
else:
    app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]

app.config["MAX_CONTENT_LENGTH"] = 64 * 1024   # JSON payloads only; no uploads
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 3600


def find_ffmpeg():
    """A usable ffmpeg: the system one, else the static binary that the
    optional imageio-ffmpeg package ships inside the venv, else None."""
    env = os.environ.get("FFMPEG")
    if env and Path(env).is_file():
        return env
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()

# ---------------------------------------------------------------------------
# Security middleware
# ---------------------------------------------------------------------------

_rate_state = defaultdict(list)


def rate_limit(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ip = request.remote_addr or "unknown"
        now = time.time()
        hits = [t for t in _rate_state[ip] if now - t < RATE_LIMIT_WINDOW]
        if len(hits) >= RATE_LIMIT_REQUESTS:
            _rate_state[ip] = hits
            logger.warning("Rate limit exceeded for %s on %s", ip, request.path)
            return jsonify({"error": "Rate limit exceeded"}), 429
        hits.append(now)
        _rate_state[ip] = hits
        return fn(*args, **kwargs)
    return wrapper


def generate_csrf_token():
    token = secrets.token_urlsafe(32)
    session["csrf_token"] = token
    session["csrf_issued"] = time.time()
    return token


def validate_csrf_token():
    token = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token")
    issued = session.get("csrf_issued", 0)
    if not expected or not token:
        return False
    if time.time() - issued > 3600:
        return False
    return secrets.compare_digest(token, expected)


def require_csrf(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not validate_csrf_token():
            logger.warning("CSRF validation failed for %s on %s",
                           request.remote_addr, request.path)
            return jsonify({"error": "Invalid or expired CSRF token"}), 403
        return fn(*args, **kwargs)
    return wrapper


@app.after_request
def add_security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self'; "
        "script-src 'self'; style-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class PathError(Exception):
    """Raised for any path that fails validation. Message is client-safe."""


def _inside_roots(path):
    return any(path == r or r in path.parents for r in ALLOWED_ROOTS)


def safe_path(raw, must_exist=True, must_be_dir=False):
    """Resolve `raw`, reject symlinks and anything outside ALLOWED_ROOTS."""
    if not raw or not isinstance(raw, str):
        raise PathError("Path is required")
    if "\x00" in raw:
        raise PathError("Invalid path")

    candidate = Path(os.path.expanduser(raw))
    if not candidate.is_absolute():
        raise PathError("Path must be absolute")

    # Reject symlinks anywhere in the chain before resolving them away.
    probe = candidate
    seen = set()
    while probe != probe.parent and probe not in seen:
        seen.add(probe)
        if probe.is_symlink():
            logger.warning("Symlink rejected: %s (%s)", probe, request.remote_addr)
            raise PathError("Symlinked paths are not allowed")
        probe = probe.parent

    resolved = candidate.resolve()
    if not _inside_roots(resolved):
        logger.warning("Path outside allowed roots: %s (%s)", resolved, request.remote_addr)
        raise PathError("Path is outside the allowed directories")

    if must_exist and not resolved.exists():
        raise PathError("Path does not exist")
    if must_be_dir and resolved.exists() and not resolved.is_dir():
        raise PathError("Path is not a directory")
    return resolved


def safe_name(raw):
    """A bare filename for a new folder: no path parts, no dotfiles."""
    name = str(raw or "").strip()
    if not name or name in (".", "..") or name != Path(name).name:
        raise PathError("Name must be a plain folder name")
    if name.startswith(".") or "\x00" in name or "/" in name or "\\" in name:
        raise PathError("Name must be a plain folder name")
    if len(name) > 200:
        raise PathError("Name is too long")
    return name


def media_kind(path):
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


PREVIEW_COUNT = 4


def dir_summary(path):
    """What a folder tile / tree node shows: media counts and a few preview
    files (first by name). One scandir, no recursion."""
    info = {"name": path.name or str(path), "path": str(path),
            "images": 0, "videos": 0, "subdirs": 0, "preview": []}
    media = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir():
                        info["subdirs"] += 1
                    elif e.is_file():
                        kind = media_kind(Path(e.name))
                        if kind == "image":
                            info["images"] += 1
                        elif kind == "video":
                            info["videos"] += 1
                        if kind:
                            media.append(e.name)
                except OSError:
                    continue
    except OSError:
        pass
    media.sort(key=str.lower)
    info["preview"] = [str(path / n) for n in media[:PREVIEW_COUNT]]
    return info


def list_dirs(target):
    """Immediate subfolders of `target` as dir_summary dicts, name order."""
    dirs = []
    with os.scandir(target) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            try:
                if e.is_symlink() or not e.is_dir():
                    continue
            except OSError:
                continue
            dirs.append(dir_summary(Path(e.path)))
    dirs.sort(key=lambda d: d["name"].lower())
    return dirs


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------

_thumb_failed = set()      # cache keys that failed; do not hammer ffmpeg on them
_thumb_lock = threading.Lock()


def thumb_key(path, stat):
    raw = "%s|%d|%d" % (path, int(stat.st_mtime), stat.st_size)
    return hashlib.sha1(raw.encode("utf-8", "surrogateescape")).hexdigest()


def open_image(path, draft=None):
    """Open an image with size guards and EXIF orientation applied.

    `draft` asks the JPEG decoder for the smallest DCT scale that still covers
    that edge length - a big win when thumbnailing a folder of large photos.
    """
    try:
        size = path.stat().st_size
    except OSError:
        raise PathError("Cannot read file")
    if size > MAX_IMAGE_BYTES:
        raise PathError("File is larger than %dMB" % (MAX_IMAGE_BYTES // (1024 * 1024)))
    try:
        img = Image.open(path)
        if draft:
            img.draft("RGB", (draft, draft))
        img.load()
    except Image.DecompressionBombError:
        raise PathError("Image exceeds the maximum pixel count")
    except Exception:
        raise PathError("Not a readable image file")
    return ImageOps.exif_transpose(img) or img


def _encode_thumb(img):
    """Fit into THUMB_EDGE, flatten alpha onto the UI background, JPEG bytes."""
    img = img.copy()
    img.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
    if img.mode in ("RGBA", "LA", "PA", "P"):
        img = img.convert("RGBA")
        canvas = Image.new("RGB", img.size, THUMB_BG)
        canvas.paste(img, mask=img.split()[-1])
        img = canvas
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()


def _image_thumb(path):
    return _encode_thumb(open_image(path, draft=THUMB_EDGE))


def _video_thumb(path):
    """Grab one frame with ffmpeg. Tries ~1s in, then the first frame for
    clips shorter than that. Returns JPEG bytes or None."""
    if not FFMPEG:
        return None
    scale = "scale='min(%d,iw)':-2" % THUMB_EDGE
    for seek in ("1", "0"):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-ss", seek, "-i", str(path), "-frames:v", "1", "-vf", scale,
               "-f", "image2", "-vcodec", "mjpeg", "-q:v", "4", "pipe:1"]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.info("ffmpeg failed on %s: %s", path, exc)
            return None
        if proc.returncode == 0 and len(proc.stdout) > 100:
            return proc.stdout
    return None


def get_thumb(path):
    """Cached thumbnail JPEG bytes for an image or video, or None."""
    stat = path.stat()
    key = thumb_key(path, stat)
    cached = CACHE_DIR / (key + ".jpg")
    if cached.is_file():
        return cached.read_bytes(), key
    if key in _thumb_failed:
        return None, key

    kind = media_kind(path)
    data = None
    try:
        if kind == "image":
            data = _image_thumb(path)
        elif kind == "video":
            data = _video_thumb(path)
    except PathError as exc:
        logger.info("Thumbnail failed for %s: %s", path, exc)

    if not data:
        with _thumb_lock:
            _thumb_failed.add(key)
        return None, key

    # Atomic write so a concurrent request never reads a half-written file.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_name(".%s.%d.tmp" % (cached.name, os.getpid()))
    try:
        tmp.write_bytes(data)
        os.replace(tmp, cached)
    except OSError as exc:
        logger.warning("Could not write thumbnail cache: %s", exc)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return data, key


# ---------------------------------------------------------------------------
# Moves + undo
# ---------------------------------------------------------------------------

_undo_stack = []           # [{"from": str, "to": str, "ts": float}, ...]
_move_lock = threading.Lock()


def unique_dest(dest_dir, name):
    """`name` inside `dest_dir`, suffixed _1, _2, ... until it does not exist."""
    dest = dest_dir / name
    if not dest.exists() and not dest.is_symlink():
        return dest
    stem, ext = Path(name).stem, Path(name).suffix
    for i in range(1, 10000):
        cand = dest_dir / ("%s_%d%s" % (stem, i, ext))
        if not cand.exists() and not cand.is_symlink():
            return cand
    raise PathError("Could not find a free filename")


def move_entry(src, dest_dir, name=None):
    """Move a file or folder into a directory, never clobbering. Returns dest.

    `name` overrides the name at the destination; undo uses it to give a
    collision-renamed entry its original name back.
    """
    if src.is_file():
        if not media_kind(src):
            raise PathError("Not a media file")
    elif src.is_dir():
        if src in ALLOWED_ROOTS:
            raise PathError("Cannot move a root folder")
        if dest_dir == src or src in dest_dir.parents:
            raise PathError("Cannot move a folder into itself")
    else:
        raise PathError("Source does not exist")
    if not dest_dir.is_dir():
        raise PathError("Destination folder does not exist")
    if src.parent == dest_dir:
        raise PathError("Already in that folder")
    dest = unique_dest(dest_dir, name or src.name)
    try:
        os.rename(src, dest)
    except OSError as exc:
        if getattr(exc, "errno", None) == 18:      # EXDEV: different filesystem
            shutil.move(str(src), str(dest))
        else:
            raise
    return dest


def record_move(src, dest, action):
    entry = {"ts": time.time(), "action": action, "from": str(src), "to": str(dest)}
    try:
        with MOVE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.warning("Could not write move log: %s", exc)
    return entry


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    generate_csrf_token()
    return render_template(
        "index.html",
        start_dir=str(DEFAULT_START_DIR),
        roots=[str(r) for r in ALLOWED_ROOTS],
        has_ffmpeg=bool(FFMPEG),
    )


@app.route("/api/csrf-token")
def api_csrf_token():
    return jsonify({"token": generate_csrf_token()})


@app.route("/api/browse")
@rate_limit
def api_browse():
    raw = request.args.get("path") or str(DEFAULT_START_DIR)
    try:
        target = safe_path(raw, must_be_dir=True)
    except PathError as exc:
        return jsonify({"error": str(exc)}), 400

    dirs, files = [], []
    try:
        for entry in os.scandir(target):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    dirs.append(dir_summary(Path(entry.path)))
                elif entry.is_file():
                    kind = media_kind(Path(entry.name))
                    if kind:
                        stat = entry.stat()
                        files.append({
                            "name": entry.name,
                            "path": entry.path,
                            "kind": kind,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                        })
            except OSError:
                continue
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403

    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    parent = str(target.parent) if target != target.parent else None
    if parent and not _inside_roots(Path(parent)):
        parent = None
    return jsonify({"path": str(target), "parent": parent,
                    "dirs": dirs, "files": files})


@app.route("/api/dirs")
@rate_limit
def api_dirs():
    """Subfolders only, for the folder tree."""
    try:
        target = safe_path(request.args.get("path"), must_be_dir=True)
        dirs = list_dirs(target)
    except PathError as exc:
        return jsonify({"error": str(exc)}), 400
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403
    return jsonify({"path": str(target), "dirs": dirs})


@app.route("/api/thumb")
@rate_limit
def api_thumb():
    try:
        path = safe_path(request.args.get("path"))
        if not path.is_file() or not media_kind(path):
            raise PathError("Not a media file")
        data, key = get_thumb(path)
    except PathError as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError:
        return jsonify({"error": "Cannot read file"}), 400

    if data is None:
        # The client falls back to letting the browser render the file itself.
        return jsonify({"error": "No thumbnail available"}), 404
    if request.headers.get("If-None-Match") == key:
        return Response(status=304)
    resp = Response(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    resp.headers["ETag"] = key
    return resp


@app.route("/api/media")
@rate_limit
def api_media():
    """The original file, with Range support so video seeking works."""
    try:
        path = safe_path(request.args.get("path"))
        if not path.is_file() or not media_kind(path):
            raise PathError("Not a media file")
    except PathError as exc:
        return jsonify({"error": str(exc)}), 400
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    resp = send_file(path, mimetype=mime, conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.route("/api/mkdir", methods=["POST"])
@rate_limit
@require_csrf
def api_mkdir():
    payload = request.get_json(silent=True) or {}
    try:
        parent = safe_path(payload.get("parent"), must_be_dir=True)
        name = safe_name(payload.get("name"))
        target = parent / name
        if target.exists():
            return jsonify({"error": "exists", "path": str(target),
                            "message": "A folder with that name already exists"}), 409
        target.mkdir()
    except PathError as exc:
        return jsonify({"error": str(exc)}), 400
    except PermissionError:
        return jsonify({"error": "Permission denied creating that folder"}), 403
    except OSError as exc:
        logger.error("mkdir failed: %s", exc)
        return jsonify({"error": "Could not create the folder"}), 500
    logger.info("Created folder %s", target)
    return jsonify({"name": target.name, "path": str(target)})


@app.route("/api/move", methods=["POST"])
@rate_limit
@require_csrf
def api_move():
    payload = request.get_json(silent=True) or {}
    paths = payload.get("paths")
    if not isinstance(paths, list):
        paths = [payload.get("path")]
    if not paths or len(paths) > 500:
        return jsonify({"error": "Nothing to move"}), 400

    try:
        dest_dir = safe_path(payload.get("dest_dir"), must_be_dir=True)
    except PathError as exc:
        return jsonify({"error": str(exc)}), 400

    moved, errors = [], []
    with _move_lock:
        for raw in paths:
            try:
                src = safe_path(raw)
                dest = move_entry(src, dest_dir)
            except PathError as exc:
                errors.append({"path": str(raw), "error": str(exc)})
                continue
            except PermissionError:
                errors.append({"path": str(raw), "error": "Permission denied"})
                continue
            except OSError as exc:
                logger.error("Move failed %s -> %s: %s", raw, dest_dir, exc)
                errors.append({"path": str(raw), "error": "Could not move the file"})
                continue
            entry = record_move(src, dest, "move")
            _undo_stack.append(entry)
            del _undo_stack[:-UNDO_DEPTH]
            logger.info("Moved %s -> %s", src, dest)
            moved.append({"from": str(src), "to": str(dest), "name": dest.name,
                          "kind": "dir" if dest.is_dir() else "file"})

    return jsonify({"moved": moved, "errors": errors,
                    "undo_depth": len(_undo_stack)})


@app.route("/api/undo", methods=["POST"])
@rate_limit
@require_csrf
def api_undo():
    with _move_lock:
        if not _undo_stack:
            return jsonify({"error": "Nothing to undo"}), 400
        entry = _undo_stack.pop()
        src, orig = Path(entry["to"]), Path(entry["from"])
        try:
            # Re-validate: the file must still be where we put it, inside roots.
            src = safe_path(str(src))
            orig_dir = safe_path(str(orig.parent), must_be_dir=True)
            dest = move_entry(src, orig_dir, orig.name)
        except PathError as exc:
            return jsonify({"error": "Cannot undo: %s" % exc}), 400
        except OSError as exc:
            logger.error("Undo failed for %s: %s", entry, exc)
            return jsonify({"error": "Could not move the file back"}), 500
        record_move(src, dest, "undo")
        logger.info("Undid move: %s -> %s", src, dest)

    return jsonify({"from": str(src), "to": str(dest), "name": dest.name,
                    "undo_depth": len(_undo_stack)})


@app.route("/api/history")
@rate_limit
def api_history():
    return jsonify({"moves": list(reversed(_undo_stack[-30:])),
                    "undo_depth": len(_undo_stack)})


@app.route("/api/status")
def api_status():
    return jsonify({"ffmpeg": FFMPEG, "cache_dir": str(CACHE_DIR),
                    "roots": [str(r) for r in ALLOWED_ROOTS]})


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    # Not 5060/5061 (SIP) or 6000 (X11): browsers refuse to open those ports.
    port = int(os.environ.get("FLASK_PORT", "5070"))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    if host == "0.0.0.0":
        logger.warning("Binding to 0.0.0.0 - this app MOVES files; do not expose it")
    if debug:
        logger.warning("Debug mode is on - never enable this on a shared host")
    logger.info("Allowed roots: %s", ", ".join(str(r) for r in ALLOWED_ROOTS))
    logger.info("Thumbnail cache: %s", CACHE_DIR)
    if FFMPEG:
        logger.info("Video thumbnails via ffmpeg: %s", FFMPEG)
    else:
        logger.warning("No ffmpeg found - video tiles will be rendered by the browser "
                       "(install system ffmpeg or `pip install imageio-ffmpeg` to fix)")
    app.run(host=host, port=port, debug=debug, threaded=True)
