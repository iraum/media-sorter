#!/usr/bin/env python3
"""
Smoke test for Media Sorter. Builds fixtures in a temp dir, starts app.py on
a free port with MEDIA_ROOTS pinned to that dir, and exercises the API.
Exits non-zero on the first failure. No third-party test deps.
"""

import io
import os
import sys
import json
import time
import socket
import shutil
import tempfile
import subprocess
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app.py"
PYTHON = sys.executable

_checks = 0


def check(cond, label):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL: %s" % label)
        sys.exit(1)
    print("  ok  %s" % label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def find_ffmpeg():
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


# --- Fixtures ---------------------------------------------------------------

tmp = Path(tempfile.mkdtemp(prefix="media-sorter-test-"))
root = tmp / "root"
photos = root / "photos"
outside = tmp / "outside"          # NOT under MEDIA_ROOTS
cache = tmp / "cache"
for d in (photos / "keep", photos / "later", outside):
    d.mkdir(parents=True)

Image.new("RGB", (900, 600), (200, 30, 30)).save(photos / "a.jpg", quality=85)
Image.new("RGBA", (300, 700), (30, 200, 30, 120)).save(photos / "b.png")
Image.new("RGB", (100, 100), (30, 30, 200)).save(photos / "c.gif")
(photos / "notes.txt").write_text("not media\n")
(photos / "link.jpg").symlink_to(photos / "a.jpg")
(outside / "o.jpg").write_bytes((photos / "a.jpg").read_bytes())

FFMPEG = find_ffmpeg()
real_video = False
if FFMPEG:
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
                        "-pix_fmt", "yuv420p", str(photos / "clip.mp4")],
                       capture_output=True, timeout=60)
    real_video = r.returncode == 0 and (photos / "clip.mp4").stat().st_size > 0
if not real_video:
    (photos / "clip.mp4").write_bytes(b"\x00" * 2048)      # listed, but not thumbnailable
print("fixtures in %s (ffmpeg: %s, real video: %s)" % (tmp, FFMPEG or "none", real_video))

# --- Server -----------------------------------------------------------------

port = free_port()
env = dict(os.environ, MEDIA_ROOTS=str(root), MEDIA_CACHE=str(cache),
           FLASK_PORT=str(port), FLASK_HOST="127.0.0.1", SECRET_KEY="test",
           MEDIA_START_DIR=str(photos))
server = subprocess.Popen([PYTHON, str(APP)], env=env, cwd=str(tmp),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
base = "http://127.0.0.1:%d" % port

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def req(path, payload=None, headers=None, method=None):
    """Returns (status, headers, body-bytes). Never raises on HTTP errors."""
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=data, headers=hdrs, method=method)
    try:
        with opener.open(r, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def jreq(path, payload=None, headers=None):
    status, hdrs, body = req(path, payload, headers)
    try:
        return status, json.loads(body.decode() or "{}")
    except ValueError:
        return status, {}


def q(path):
    return urllib.request.quote(str(path), safe="")


try:
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.25)
    else:
        print("FAIL: server did not start")
        sys.exit(1)

    # --- Session + CSRF ----------------------------------------------------
    status, _, body = req("/")
    check(status == 200 and b"Media Sorter" in body, "index renders")
    status, data = jreq("/api/csrf-token")
    check(status == 200 and data.get("token"), "csrf token issued")
    csrf = {"X-CSRF-Token": data["token"]}

    status, data = jreq("/api/status")
    check(status == 200 and data.get("roots") == [str(root)], "status reports roots")

    # --- Browse ------------------------------------------------------------
    status, data = jreq("/api/browse?path=" + q(photos))
    check(status == 200, "browse photos")
    names = [f["name"] for f in data["files"]]
    kinds = {f["name"]: f["kind"] for f in data["files"]}
    check(names == ["a.jpg", "b.png", "c.gif", "clip.mp4"], "browse lists media only, sorted: %s" % names)
    check(kinds["a.jpg"] == "image" and kinds["clip.mp4"] == "video", "browse reports kinds")
    check("link.jpg" not in names and "notes.txt" not in names, "browse skips symlinks and non-media")
    check([d["name"] for d in data["dirs"]] == ["keep", "later"], "browse lists subfolders")
    check(data["dirs"][0]["images"] == 0 and data["dirs"][0]["preview"] == [], "empty subfolder summary")
    check(data["parent"] == str(root), "browse gives parent inside roots")

    status, data = jreq("/api/browse?path=" + q(root))
    check(status == 200 and data["parent"] is None, "browse at root has no parent")
    d = data["dirs"][0]
    check(d["name"] == "photos" and d["images"] == 3 and d["videos"] == 1 and d["subdirs"] == 2,
          "folder summary counts media and subfolders")
    check([Path(p).name for p in d["preview"]] == ["a.jpg", "b.png", "c.gif", "clip.mp4"], "folder summary previews first files by name")

    status, data = jreq("/api/dirs?path=" + q(root))
    check(status == 200 and [x["name"] for x in data["dirs"]] == ["photos"], "dirs endpoint lists subfolders")
    status, _ = jreq("/api/dirs?path=" + q(outside))
    check(status == 400, "dirs endpoint outside roots rejected")

    # --- Path guards -------------------------------------------------------
    status, _ = jreq("/api/browse?path=" + q(outside))
    check(status == 400, "browse outside roots rejected")
    status, _ = jreq("/api/thumb?path=" + q(outside / "o.jpg"))
    check(status == 400, "thumb outside roots rejected")
    status, _ = jreq("/api/thumb?path=" + q(photos / ".." / ".." / "outside" / "o.jpg"))
    check(status == 400, "thumb with .. escape rejected")
    status, _ = jreq("/api/thumb?path=photos/a.jpg")
    check(status == 400, "thumb with relative path rejected")
    status, _ = jreq("/api/thumb?path=" + q(photos / "link.jpg"))
    check(status == 400, "thumb via symlink rejected")
    status, _ = jreq("/api/thumb?path=" + q(photos / "notes.txt"))
    check(status == 400, "thumb of non-media rejected")
    status, _ = jreq("/api/media?path=" + q(photos / "notes.txt"))
    check(status == 400, "media of non-media rejected")

    # --- Thumbnails --------------------------------------------------------
    status, hdrs, body = req("/api/thumb?path=" + q(photos / "a.jpg"))
    check(status == 200 and hdrs.get("Content-Type", "").startswith("image/jpeg"), "image thumb is jpeg")
    im = Image.open(io.BytesIO(body))
    check(max(im.size) <= 320 and im.size[0] > im.size[1], "image thumb fits 320 and keeps aspect")
    etag = hdrs.get("ETag")
    check(bool(etag) and any(cache.glob("*.jpg")), "thumb cached to disk with etag")
    status, _, _ = req("/api/thumb?path=" + q(photos / "a.jpg"), headers={"If-None-Match": etag})
    check(status == 304, "thumb etag revalidation returns 304")

    status, hdrs, body = req("/api/thumb?path=" + q(photos / "b.png"))
    check(status == 200 and Image.open(io.BytesIO(body)).mode == "RGB", "alpha png thumb flattened to RGB jpeg")

    status, hdrs, body = req("/api/thumb?path=" + q(photos / "clip.mp4"))
    if real_video:
        check(status == 200 and Image.open(io.BytesIO(body)).size[0] <= 320, "video thumb via ffmpeg")
    else:
        check(status == 404, "video thumb 404s cleanly without ffmpeg")

    # --- Media + Range -----------------------------------------------------
    status, hdrs, body = req("/api/media?path=" + q(photos / "a.jpg"))
    check(status == 200 and len(body) == (photos / "a.jpg").stat().st_size, "media serves full file")
    status, hdrs, body = req("/api/media?path=" + q(photos / "a.jpg"), headers={"Range": "bytes=0-9"})
    check(status == 206 and len(body) == 10, "media honours range requests")

    # --- CSRF --------------------------------------------------------------
    status, _ = jreq("/api/move", {"paths": [str(photos / "a.jpg")], "dest_dir": str(photos / "keep")})
    check(status == 403, "move without csrf rejected")

    # --- mkdir -------------------------------------------------------------
    status, data = jreq("/api/mkdir", {"parent": str(photos), "name": "sorted"}, csrf)
    check(status == 200 and (photos / "sorted").is_dir(), "mkdir creates folder")
    status, _ = jreq("/api/mkdir", {"parent": str(photos), "name": "sorted"}, csrf)
    check(status == 409, "mkdir duplicate is 409")
    for bad in ("../x", "a/b", ".hidden", "", "..", "x\x00y"):
        status, _ = jreq("/api/mkdir", {"parent": str(photos), "name": bad}, csrf)
        check(status == 400, "mkdir rejects %r" % bad)
    status, _ = jreq("/api/mkdir", {"parent": str(outside), "name": "x"}, csrf)
    check(status == 400, "mkdir outside roots rejected")

    # --- Move --------------------------------------------------------------
    status, data = jreq("/api/move", {"paths": [str(photos / "a.jpg")], "dest_dir": str(photos / "keep")}, csrf)
    check(status == 200 and len(data["moved"]) == 1 and not data["errors"], "move a.jpg -> keep")
    check((photos / "keep" / "a.jpg").is_file() and not (photos / "a.jpg").exists(), "a.jpg actually moved")
    check(data["undo_depth"] == 1, "undo depth tracks")

    # collision: keep/b.png already exists -> b_1.png
    (photos / "keep" / "b.png").write_bytes(b"occupied")
    status, data = jreq("/api/move", {"path": str(photos / "b.png"), "dest_dir": str(photos / "keep")}, csrf)
    check(status == 200 and data["moved"][0]["name"] == "b_1.png", "collision renamed to b_1.png")
    check((photos / "keep" / "b.png").read_bytes() == b"occupied", "existing file untouched")

    status, data = jreq("/api/move", {"paths": [str(photos / "c.gif")], "dest_dir": str(photos)}, csrf)
    check(status == 200 and not data["moved"] and data["errors"], "move into same folder reported as error")
    status, data = jreq("/api/move", {"paths": [str(photos / "c.gif")], "dest_dir": str(outside)}, csrf)
    check(status == 400, "move to outside roots rejected")
    status, data = jreq("/api/move", {"paths": [str(outside / "o.jpg")], "dest_dir": str(photos / "keep")}, csrf)
    check(status == 200 and data["errors"] and not (photos / "keep" / "o.jpg").exists(), "move from outside roots rejected per-file")
    status, data = jreq("/api/move", {"paths": [str(photos / "notes.txt")], "dest_dir": str(photos / "keep")}, csrf)
    check(status == 200 and data["errors"], "move of non-media rejected")
    status, data = jreq("/api/move", {"paths": [str(photos / "link.jpg")], "dest_dir": str(photos / "keep")}, csrf)
    check(status == 200 and data["errors"] and (photos / "link.jpg").is_symlink(), "move of symlink rejected")

    # batch
    status, data = jreq("/api/move", {"paths": [str(photos / "c.gif"), str(photos / "clip.mp4")],
                                      "dest_dir": str(photos / "later")}, csrf)
    check(status == 200 and len(data["moved"]) == 2, "batch move of two files")
    check(sorted(p.name for p in (photos / "later").iterdir()) == ["c.gif", "clip.mp4"], "batch landed")

    status, data = jreq("/api/browse?path=" + q(photos))
    check(data["files"] == [], "photos folder now empty of media")

    # --- Moving folders ------------------------------------------------------
    status, data = jreq("/api/move", {"paths": [str(photos / "keep")], "dest_dir": str(photos / "later")}, csrf)
    check(status == 200 and data["moved"][0]["kind"] == "dir" and (photos / "later" / "keep" / "a.jpg").is_file(),
          "move a folder (with its contents)")
    status, data = jreq("/api/move", {"paths": [str(photos / "later")], "dest_dir": str(photos / "later" / "keep")}, csrf)
    check(status == 200 and data["errors"] and (photos / "later").is_dir(), "moving a folder into itself refused")
    status, data = jreq("/api/move", {"paths": [str(root)], "dest_dir": str(photos)}, csrf)
    check(status == 200 and data["errors"], "moving a root folder refused")

    status, data = jreq("/api/history")
    check(status == 200 and data["undo_depth"] == 5 and len(data["moves"]) == 5, "history lists moves")
    log = [json.loads(l) for l in (APP.parent / "moves.log").read_text().splitlines()[-5:]]
    check(all(e["action"] == "move" for e in log), "moves.log records moves")

    # --- Undo --------------------------------------------------------------
    status, data = jreq("/api/undo", {}, csrf)
    check(status == 200 and data["name"] == "keep" and (photos / "keep" / "a.jpg").is_file(), "undo restores the moved folder")
    status, data = jreq("/api/undo", {}, csrf)
    check(status == 200 and data["name"] == "clip.mp4" and (photos / "clip.mp4").is_file(), "undo restores last move (clip.mp4)")
    status, data = jreq("/api/undo", {}, csrf)
    check(status == 200 and (photos / "c.gif").is_file(), "undo restores c.gif")
    status, data = jreq("/api/undo", {}, csrf)
    check(status == 200 and (photos / "b.png").is_file() and not (photos / "keep" / "b_1.png").exists(),
          "undo restores b_1.png back to b.png")
    status, data = jreq("/api/undo", {}, csrf)
    check(status == 200 and (photos / "a.jpg").is_file() and data["undo_depth"] == 0, "undo restores a.jpg")
    status, data = jreq("/api/undo", {}, csrf)
    check(status == 400, "undo with empty stack is 400")
    status, _ = jreq("/api/undo", {})
    check(status == 403, "undo without csrf rejected")

    print("\nPASS  %d checks" % _checks)
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
    shutil.rmtree(tmp, ignore_errors=True)
