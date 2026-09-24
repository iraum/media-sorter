# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A self-hosted Flask app for seeing what is in folders of images and videos
and reorganising them by drag and drop: a folder tree, one or two folder
panes of thumbnails, folder tiles that preview their contents. No build step,
no database, no CDNs, no frontend framework. Scope is deliberately narrow:
browse, preview, move (files and folders), undo. It does not edit, rename,
delete, or tag files, and should not grow those without being asked. The
emphasis is *seeing where things are* and *dragging*, not keyboard culling —
an earlier version had per-folder hotkeys and they were removed on purpose.

## Commands

```bash
# One-time setup
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# Run (start.sh prints a banner, honors env overrides, then exec's app.py)
./start.sh                       # picks up ./venv/bin/python automatically
./start.sh --background          # detached, logs to media-sorter.log, opens browser
./start.sh --stop

# Tests
./venv/bin/python tests/smoke.py
```

Open http://localhost:5070. Env overrides: `FLASK_HOST` (default `127.0.0.1`),
`FLASK_PORT` (5070), `MEDIA_ROOTS` (colon-separated, default
`/mnt/spielraum:$HOME`), `MEDIA_START_DIR`, `MEDIA_CACHE`, `FFMPEG`,
`SECRET_KEY`, `FLASK_DEBUG`, `PYTHON`.

`tests/smoke.py` is the verification path: it creates fixtures in a temp dir
(images via Pillow, a real 2s video via ffmpeg when one is found), starts the
app on a free port with `MEDIA_ROOTS` pinned to that dir, then exercises the
whole API including every path guard, CSRF, collision renaming and undo. Run
it after any change to `app.py`. It is one script with no test selection; it
exits non-zero on the first failure. There is no browser-driven test — check
`static/js/app.js` changes by hand in a browser.

Python must stay **3.9-compatible** (the reference host runs 3.9; the machines
this is meant for may be newer). No `match`, no `X | Y` unions at runtime.

## Architecture

**Backend — `app.py` (single file).** Flask serving one HTML shell plus a JSON
API. Every filesystem-touching endpoint goes through `safe_path()`, copied
from the sibling `image-editor` project and kept identical on purpose:

- `safe_path(raw, must_exist, must_be_dir)` — rejects relative paths and NULs,
  walks the parent chain rejecting **any symlink**, resolves, then requires the
  result to sit inside `ALLOWED_ROOTS`. Raises `PathError`, whose message is
  always safe to return to the client. Never bypass it, never widen the roots
  at request time. `safe_name()` is the equivalent for a new folder name.
- `media_kind(path)` — `"image"` / `"video"` / `None` by extension
  (`IMAGE_EXTS`, `VIDEO_EXTS`). It gates what `/api/browse` lists and what
  `/api/thumb`, `/api/media` and `/api/move` will touch. Widening it widens all
  four.
- `dir_summary(path)` — one scandir of a folder: image/video/subfolder counts
  plus up to `PREVIEW_COUNT` preview paths (first media files by name). This
  is what folder tiles and tree nodes display. `/api/browse` calls it for
  every subfolder it lists, so a browse costs one scandir per subfolder;
  `/api/dirs` returns only these summaries, for the tree.
- `@rate_limit`, `require_csrf`, `add_security_headers` — as in image-editor.
  CSP has `script-src 'self'` (so **no inline JS in the template**) and
  `media-src 'self'` so `<video>` can play from `/api/media`.

**Thumbnails.** `get_thumb(path)` returns cached JPEG bytes or generates them:
`_image_thumb` (Pillow: size/pixel caps, JPEG `draft` fast-path, EXIF
transpose, alpha flattened onto `THUMB_BG`) or `_video_thumb` (one frame via
ffmpeg at 1s, retrying at 0s for short clips). The cache lives in `CACHE_DIR`
keyed by `sha1(path|mtime|size)`, written atomically; keys that fail land in
the in-memory `_thumb_failed` set so a bad file is not re-attempted every
render. `FFMPEG` is resolved once at import by `find_ffmpeg()`: `$FFMPEG`, then
the system binary, then the one `imageio-ffmpeg` ships in the venv, else
`None` — in which case `/api/thumb` returns **404** for videos and the
frontend swaps in a `<video preload="metadata">` tile so the browser renders
the frame itself. Keep that fallback working; it's what makes the app run on
machines with no ffmpeg at all.

**Moves.** `POST /api/move {paths, dest_dir}` moves each entry with
`move_entry()` → `unique_dest()` (never overwrites: `_1`, `_2` suffixes),
`os.rename` with a `shutil.move` fallback on `EXDEV`. Files must pass
`media_kind`; directories are allowed but never a root, and never into
themselves or a descendant. Every success is appended to `moves.log` (JSONL,
`record_move`) and pushed onto the in-memory `_undo_stack` (capped at
`UNDO_DEPTH`). `POST /api/undo` pops one entry and moves it back through the
same validation, under its **original name** — the entry must still be where
it was put and inside the roots. Per-item failures come back in `errors` with
a 200, so a batch is never all-or-nothing. All of this runs under `_move_lock`.

API: `/api/browse`, `/api/dirs`, `/api/thumb`, `/api/media` (Range-capable
via `send_file(conditional=True)`), `/api/history`, `/api/status`,
`/api/csrf-token`, and the POSTs `/api/mkdir`, `/api/move`, `/api/undo`.

**Frontend — `static/js/app.js` (single IIFE).** Three pieces:

- `class Pane` — one folder view. `pane.dirs`/`pane.files` are what the
  server returned; `pane.view` is folders (name order) followed by files
  after filter + sort, and is the only thing the grid, cursor, selection and
  keyboard ever index. Items carry `type: "dir" | "file"`. `state.panes` holds
  one or two of them; `state.active` is the one the keyboard and the tree act
  on. `Pane.load(path, {keepCursor, keepSelection, focusPath, refresh})` is
  the only way a pane changes folder; with `refresh` it climbs to the parent
  if its folder no longer exists (it was moved).
- `tree` — lazy folder tree over `ROOTS`, nodes keyed by path in
  `tree.nodes`. `reveal(dir)` expands the chain down to `dir` and marks it
  current; `refreshAround(dirs)` re-lists the loaded nodes for those dirs and
  their parents (counts and previews live on the parent's listing) and
  re-expands what was open. After any move or mkdir, call it with every
  touched folder.
- Drag and drop — HTML5 DnD with the payload kept in the module-level
  `drag` (`dataTransfer` is unreadable during `dragover`). `dropTarget(node,
  getDest)` is the one place drop handling lives; every droppable thing
  (folder tile, tree row, breadcrumb segment, pane body) registers through
  it, and `canDropInto()` refuses no-op and into-itself drops before the
  server ever sees them. All drops end in `moveItems(paths, dest)`, which
  posts, toasts, then `refreshAll()`s the panes and refreshes the tree.

After a move the source pane reloads with the cursor kept at the **same
index**, so the next item lands under it. Undo reloads and focuses the
restored item. `img.onerror` on a file tile is the ffmpeg-less fallback
described above; on a collage image it just hides that image.

**CSS gotcha:** component rules set `display:`, which beats the UA stylesheet's
`[hidden]`. `[hidden] { display: none !important; }` at the top of
`style.css` is what makes `hidden` work — don't remove it.

## Conventions

- Vanilla JS only, no dependencies added to the frontend, no inline scripts or
  styles (the CSP blocks them).
- Log security events via `logger` (goes to `security.log`, gitignored); return
  sanitized messages to the client.
- Pin new Python dependencies exactly in `requirements.txt`.
- `README.md` is the user-facing reference; keep it in sync when user-visible
  behaviour changes (keys, formats, safety).
