/* Media Sorter - folder tree + one or two folder panes, organise by dragging.
   Vanilla JS, one IIFE. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const body = document.body;
  const HAS_FFMPEG = body.dataset.hasFfmpeg === "true";
  const ROOTS = (body.dataset.roots || "").split(":").filter(Boolean);
  const SORTS = ["name", "date", "size"];
  const FILTERS = ["all", "image", "video"];

  const state = {
    panes: [],
    active: 0,
    sort: "name",
    filter: "all",
    tileSize: 200,
    csrf: null,
    undoDepth: 0,
    lightbox: null,        // the Pane being viewed, or null
  };

  const el = {
    panes: $("panes"), tree: $("tree"),
    btnNew: $("btn-newfolder"), btnUndo: $("btn-undo"), btnSplit: $("btn-split"),
    sort: $("sort"), filter: $("filter"), size: $("size"), btnHelp: $("btn-help"),
    statusL: $("status-left"), statusR: $("status-right"),
    lightbox: $("lightbox"), lbStage: $("lb-stage"), lbCaption: $("lb-caption"),
    lbClose: $("lb-close"), lbPrev: $("lb-prev"), lbNext: $("lb-next"),
    modal: $("modal"), modalForm: $("modal-form"), modalInput: $("modal-input"),
    modalWhere: $("modal-where"), modalCancel: $("modal-cancel"),
    help: $("help"), helpClose: $("help-close"),
    toast: $("toast"), ghost: $("drag-ghost"),
  };

  // --- Persistence (per-browser conveniences only) -------------------------
  function remember(key, value) {
    try { localStorage.setItem("media-sorter:" + key, JSON.stringify(value)); } catch (e) { /* ignore */ }
  }
  function recall(key, fallback) {
    try {
      const raw = localStorage.getItem("media-sorter:" + key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) { return fallback; }
  }

  // --- API -----------------------------------------------------------------
  async function api(url, payload, retry) {
    const opts = payload === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf || "" },
      body: JSON.stringify(payload),
    };
    const res = await fetch(url, opts);
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (res.status === 403 && payload !== undefined && !retry) {
      await fetchCsrf();
      return api(url, payload, true);
    }
    if (!res.ok) throw new Error(data.message || data.error || res.statusText);
    return data;
  }
  async function fetchCsrf() {
    const data = await api("/api/csrf-token");
    state.csrf = data.token;
  }

  // --- Helpers -------------------------------------------------------------
  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }
  function fmtDate(ts) {
    const d = new Date(ts * 1000);
    const p = (x) => String(x).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }
  function dirMeta(d) {
    const parts = [];
    if (d.images) parts.push(plural(d.images, "image"));
    if (d.videos) parts.push(plural(d.videos, "video"));
    if (d.subdirs) parts.push(plural(d.subdirs, "folder"));
    return parts.length ? parts.join(" · ") : "empty";
  }
  function mediaUrl(path) { return "/api/media?path=" + encodeURIComponent(path); }
  function thumbUrl(path) { return "/api/thumb?path=" + encodeURIComponent(path); }
  function baseName(path) { return path.slice(path.lastIndexOf("/") + 1) || path; }
  function parentOf(path) { const i = path.lastIndexOf("/"); return i > 0 ? path.slice(0, i) : "/"; }
  function joinPath(dir, name) { return (dir === "/" ? "" : dir) + "/" + name; }
  function rootOf(path) { return ROOTS.find((r) => path === r || path.startsWith(r === "/" ? "/" : r + "/")) || null; }
  function activePane() { return state.panes[state.active]; }
  function otherPane() { return state.panes[state.active === 0 ? 1 : 0] || null; }

  let toastTimer = null;
  function toast(msg, isError) {
    el.toast.textContent = msg;
    el.toast.classList.toggle("error", !!isError);
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.hidden = true; }, isError ? 4000 : 2200);
  }

  // --- Drag and drop ---------------------------------------------------------
  // dataTransfer contents are not readable during dragover, so the payload is
  // kept here for the duration of the drag.
  let drag = null;   // {paths: [...], from: dir}

  function startDrag(e, paths, fromDir) {
    drag = { paths, from: fromDir };
    e.dataTransfer.setData("text/plain", paths.join("\n"));
    e.dataTransfer.effectAllowed = "move";
    el.ghost.textContent = paths.length === 1 ? baseName(paths[0]) : paths.length + " items";
    el.ghost.hidden = false;
    e.dataTransfer.setDragImage(el.ghost, 16, 16);
    setTimeout(() => { el.ghost.hidden = true; }, 0);
  }
  function canDropInto(dest) {
    if (!drag || !dest || drag.from === dest) return false;
    return !drag.paths.some((p) => p === dest || dest.startsWith(p + "/"));
  }
  function dropTarget(node, getDest) {
    node.addEventListener("dragover", (e) => {
      const dest = getDest();
      if (!canDropInto(dest)) return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = "move";
      node.classList.add("drop");
    });
    node.addEventListener("dragleave", () => node.classList.remove("drop"));
    node.addEventListener("drop", (e) => {
      node.classList.remove("drop");
      const dest = getDest();
      if (!canDropInto(dest)) return;
      e.preventDefault();
      e.stopPropagation();
      const payload = drag;
      drag = null;
      moveItems(payload.paths, dest);
    });
  }
  document.addEventListener("dragend", () => {
    drag = null;
    document.querySelectorAll(".drop, .dragging").forEach((n) => n.classList.remove("drop", "dragging"));
  });

  // --- Pane ------------------------------------------------------------------
  class Pane {
    constructor(idx) {
      this.idx = idx;
      this.dir = null;
      this.parent = null;
      this.dirs = [];
      this.files = [];
      this.view = [];          // folders first, then filtered+sorted files
      this.cursor = 0;
      this.anchor = 0;
      this.selected = new Set();
      this.build();
    }

    build() {
      const root = document.createElement("section");
      root.className = "pane";
      root.addEventListener("mousedown", () => setActive(this.idx));

      const crumbs = document.createElement("nav");
      crumbs.className = "crumbs";
      const bodyEl = document.createElement("div");
      bodyEl.className = "pane-body";
      const grid = document.createElement("div");
      grid.className = "grid";
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.hidden = true;
      const foot = document.createElement("div");
      foot.className = "pane-foot";
      bodyEl.append(grid, empty);
      root.append(crumbs, bodyEl, foot);

      bodyEl.addEventListener("click", (e) => {
        if (e.target === bodyEl || e.target === grid) { this.selected.clear(); this.updateClasses(); this.renderFoot(); }
      });
      dropTarget(bodyEl, () => this.dir);

      this.el = root; this.crumbs = crumbs; this.body = bodyEl; this.grid = grid; this.empty = empty; this.foot = foot;
    }

    async load(path, opts) {
      opts = opts || {};
      let data;
      try {
        data = await api("/api/browse?path=" + encodeURIComponent(path));
      } catch (err) {
        if (opts.refresh) {
          // The folder we were showing is gone (moved away); climb.
          const up = this.parent || ROOTS[0];
          if (up && up !== path) return this.load(up, { refresh: false });
        }
        toast(err.message, true);
        return false;
      }
      this.dir = data.path;
      this.parent = data.parent;
      this.dirs = data.dirs;
      this.files = data.files;
      if (!opts.keepSelection) this.selected.clear();
      remember("dir" + this.idx, this.dir);
      this.rebuildView();
      if (opts.focusPath) {
        const i = this.view.findIndex((it) => it.path === opts.focusPath);
        if (i >= 0) this.cursor = i;
      } else if (!opts.keepCursor) {
        this.cursor = 0;
      }
      this.clampCursor();
      this.anchor = this.cursor;
      this.render();
      if (this.idx === state.active) tree.reveal(this.dir);
      return true;
    }

    rebuildView() {
      const cmp = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" }).compare;
      const dirs = this.dirs.slice().sort((a, b) => cmp(a.name, b.name))
        .map((d) => Object.assign({ type: "dir" }, d));
      let files = this.files;
      if (state.filter !== "all") files = files.filter((f) => f.kind === state.filter);
      files = files.slice();
      if (state.sort === "name") files.sort((a, b) => cmp(a.name, b.name));
      else if (state.sort === "date") files.sort((a, b) => b.mtime - a.mtime || cmp(a.name, b.name));
      else if (state.sort === "size") files.sort((a, b) => b.size - a.size || cmp(a.name, b.name));
      this.view = dirs.concat(files.map((f) => Object.assign({ type: "file" }, f)));
      for (const p of Array.from(this.selected)) {
        if (!this.view.some((it) => it.path === p)) this.selected.delete(p);
      }
    }

    clampCursor() {
      this.cursor = this.view.length ? Math.max(0, Math.min(this.cursor, this.view.length - 1)) : 0;
    }

    current() { return this.view[this.cursor] || null; }
    files_() { return this.view.filter((it) => it.type === "file"); }

    targets() {
      if (this.selected.size) return this.view.filter((it) => this.selected.has(it.path)).map((it) => it.path);
      const cur = this.current();
      return cur ? [cur.path] : [];
    }

    // -- rendering
    render() {
      this.renderCrumbs();
      this.grid.innerHTML = "";
      this.empty.hidden = this.view.length > 0;
      this.empty.textContent = this.files.length || this.dirs.length ? "Nothing matches the current filter." : "Empty folder — drop things here.";
      const frag = document.createDocumentFragment();
      this.view.forEach((it, i) => frag.appendChild(it.type === "dir" ? this.makeDirTile(it, i) : this.makeFileTile(it, i)));
      this.grid.appendChild(frag);
      this.updateClasses();
      this.renderFoot();
      renderStatus();
    }

    renderCrumbs() {
      const c = this.crumbs;
      c.innerHTML = "";
      const up = document.createElement("button");
      up.type = "button"; up.className = "crumb-up"; up.textContent = "↑"; up.title = "Up one folder (Backspace)";
      up.disabled = !this.parent;
      up.addEventListener("click", () => this.goUp());
      c.appendChild(up);

      const root = rootOf(this.dir);
      const segs = [];
      if (root) {
        segs.push({ label: baseName(root), path: root });
        const rel = this.dir.slice(root.length).split("/").filter(Boolean);
        let cur = root;
        for (const s of rel) { cur = joinPath(cur, s); segs.push({ label: s, path: cur }); }
      } else {
        segs.push({ label: this.dir, path: this.dir });
      }
      segs.forEach((seg, i) => {
        if (i > 0) { const sep = document.createElement("span"); sep.className = "crumb-sep"; sep.textContent = "›"; c.appendChild(sep); }
        const span = document.createElement("span");
        span.className = "crumb" + (i === segs.length - 1 ? " last" : "");
        span.textContent = seg.label;
        span.title = seg.path;
        if (i < segs.length - 1) span.addEventListener("click", () => this.load(seg.path));
        dropTarget(span, () => seg.path);
        c.appendChild(span);
      });

      const edit = document.createElement("button");
      edit.type = "button"; edit.className = "crumb-edit"; edit.textContent = "✎"; edit.title = "Type a path";
      const form = document.createElement("form");
      form.hidden = true;
      const input = document.createElement("input");
      input.type = "text"; input.spellcheck = false; input.value = this.dir;
      form.appendChild(input);
      const showForm = () => { form.hidden = false; edit.hidden = true; input.value = this.dir; input.focus(); input.select(); };
      const hideForm = () => { form.hidden = true; edit.hidden = false; };
      edit.addEventListener("click", showForm);
      form.addEventListener("submit", (e) => { e.preventDefault(); hideForm(); this.load(input.value.trim()); });
      input.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); hideForm(); } });
      input.addEventListener("blur", hideForm);
      c.append(edit, form);
      this.pathInput = input; this.showPathForm = showForm;
    }

    makeDirTile(d, i) {
      const tile = document.createElement("div");
      tile.className = "tile dir";
      tile.dataset.index = String(i);
      tile.dataset.path = d.path;
      tile.draggable = true;
      tile.title = d.path + "\n" + dirMeta(d);

      const collage = document.createElement("div");
      const n = Math.min(d.preview.length, 4);
      collage.className = "collage c" + n;
      if (n === 0) {
        const g = document.createElement("div"); g.className = "glyph"; g.textContent = "▰";
        collage.appendChild(g);
      } else {
        for (const p of d.preview.slice(0, 4)) {
          const img = document.createElement("img");
          img.loading = "lazy"; img.decoding = "async"; img.alt = "";
          img.src = thumbUrl(p);
          img.addEventListener("error", () => { img.style.visibility = "hidden"; });
          collage.appendChild(img);
        }
      }
      const check = document.createElement("div"); check.className = "check"; check.textContent = "✓";
      const name = document.createElement("div"); name.className = "name"; name.textContent = d.name;
      const meta = document.createElement("div"); meta.className = "meta"; meta.textContent = dirMeta(d);
      tile.append(collage, check, name, meta);

      this.wireTile(tile, d, i);
      dropTarget(tile, () => d.path);
      return tile;
    }

    makeFileTile(f, i) {
      const tile = document.createElement("div");
      tile.className = "tile";
      tile.dataset.index = String(i);
      tile.dataset.path = f.path;
      tile.draggable = true;
      tile.title = f.name + "\n" + fmtSize(f.size) + " · " + fmtDate(f.mtime);

      const thumb = document.createElement("div");
      thumb.className = "thumb";
      const img = document.createElement("img");
      img.loading = "lazy"; img.decoding = "async"; img.alt = "";
      img.src = thumbUrl(f.path);
      img.addEventListener("error", () => {
        // No server-side thumbnail (no ffmpeg / unreadable). Let the browser try.
        if (f.kind === "video") {
          const v = document.createElement("video");
          v.preload = "metadata"; v.muted = true; v.playsInline = true;
          v.src = mediaUrl(f.path) + "#t=0.5";
          thumb.replaceChild(v, img);
        } else {
          const n = document.createElement("div"); n.className = "noimg"; n.textContent = "no preview";
          thumb.replaceChild(n, img);
        }
      });
      thumb.appendChild(img);
      tile.appendChild(thumb);
      if (f.kind === "video") {
        const badge = document.createElement("div"); badge.className = "badge"; badge.textContent = "▶ video";
        tile.appendChild(badge);
      }
      const check = document.createElement("div"); check.className = "check"; check.textContent = "✓";
      const name = document.createElement("div"); name.className = "name"; name.textContent = f.name;
      tile.append(check, name);

      this.wireTile(tile, f, i);
      return tile;
    }

    wireTile(tile, item, i) {
      tile.addEventListener("click", (e) => this.onTileClick(i, e));
      tile.addEventListener("dblclick", (e) => { this.setCursor(i); this.open(e.ctrlKey || e.metaKey); });
      tile.addEventListener("dragstart", (e) => {
        setActive(this.idx);
        if (!this.selected.has(item.path)) { this.selected.clear(); this.setCursor(i); }
        const paths = this.targets();
        startDrag(e, paths, this.dir);
        for (const p of paths) {
          const t = this.grid.querySelector('.tile[data-path="' + CSS.escape(p) + '"]');
          if (t) t.classList.add("dragging");
        }
      });
    }

    tileAt(i) { return this.grid.children[i] || null; }

    updateClasses() {
      const tiles = this.grid.children;
      for (let i = 0; i < tiles.length; i++) {
        tiles[i].classList.toggle("cursor", i === this.cursor);
        tiles[i].classList.toggle("selected", this.selected.has(tiles[i].dataset.path));
      }
    }

    setCursor(i, opts) {
      opts = opts || {};
      if (this.view.length === 0) return;
      this.cursor = Math.max(0, Math.min(i, this.view.length - 1));
      if (opts.extend) {
        const lo = Math.min(this.anchor, this.cursor), hi = Math.max(this.anchor, this.cursor);
        for (let k = lo; k <= hi; k++) this.selected.add(this.view[k].path);
      } else if (!opts.keepSelection) {
        this.anchor = this.cursor;
      }
      this.updateClasses();
      const t = this.tileAt(this.cursor);
      if (t) t.scrollIntoView({ block: "nearest" });
      this.renderFoot();
      renderStatus();
      if (state.lightbox === this) renderLightbox();
    }

    onTileClick(i, e) {
      setActive(this.idx);
      const it = this.view[i];
      if (e.ctrlKey || e.metaKey) {
        if (this.selected.has(it.path)) this.selected.delete(it.path); else this.selected.add(it.path);
        this.setCursor(i, { keepSelection: true });
      } else if (e.shiftKey) {
        this.setCursor(i, { extend: true });
      } else {
        this.selected.clear();
        this.setCursor(i);
      }
    }

    toggleCurrent() {
      const it = this.current();
      if (!it) return;
      if (this.selected.has(it.path)) this.selected.delete(it.path); else this.selected.add(it.path);
      this.updateClasses(); this.renderFoot();
    }

    columns() {
      return Math.max(1, getComputedStyle(this.grid).gridTemplateColumns.split(" ").length);
    }

    // Enter / double-click: folders open, files view.
    open(inOtherPane) {
      const it = this.current();
      if (!it) return;
      if (it.type === "dir") {
        if (inOtherPane) { const other = ensureSplit(); other.load(it.path); }
        else this.load(it.path);
      } else {
        openLightbox(this);
      }
    }

    goUp() {
      if (this.parent) this.load(this.parent, { focusPath: this.dir });
      else toast("At the top of the allowed folders");
    }

    renderFoot() {
      const parts = [];
      if (this.dirs.length) parts.push(plural(this.dirs.length, "folder"));
      const imgs = this.files.filter((f) => f.kind === "image").length;
      const vids = this.files.length - imgs;
      parts.push(plural(imgs, "image"));
      if (vids) parts.push(plural(vids, "video"));
      if (state.filter !== "all") parts.push("showing " + this.files_().length);
      if (this.selected.size) parts.push(this.selected.size + " selected");
      this.foot.textContent = parts.join(" · ");
    }
  }

  // --- Panes / split ---------------------------------------------------------
  function setActive(idx) {
    if (!state.panes[idx]) return;
    state.active = idx;
    state.panes.forEach((p, i) => p.el.classList.toggle("active", i === idx));
    const p = activePane();
    if (p.dir) tree.reveal(p.dir);
    renderStatus();
  }

  function ensureSplit() {
    if (state.panes.length < 2) toggleSplit(true);
    return state.panes[1];
  }

  function toggleSplit(force) {
    const on = force === undefined ? state.panes.length < 2 : force;
    if (on && state.panes.length < 2) {
      const p = new Pane(1);
      state.panes.push(p);
      el.panes.appendChild(p.el);
      p.load(recall("dir1", null) || activePane().dir);
    } else if (!on && state.panes.length > 1) {
      if (state.lightbox === state.panes[1]) closeLightbox();
      state.panes[1].el.remove();
      state.panes.pop();
      setActive(0);
    }
    el.btnSplit.classList.toggle("on", state.panes.length > 1);
    remember("split", state.panes.length > 1);
  }

  async function refreshAll(opts) {
    await Promise.all(state.panes.map((p) => p.load(p.dir, Object.assign({ keepCursor: true, keepSelection: true, refresh: true }, opts || {}))));
  }

  // --- Moving ------------------------------------------------------------
  async function moveItems(paths, destDir) {
    if (!paths.length) return;
    let data;
    try {
      data = await api("/api/move", { paths, dest_dir: destDir });
    } catch (err) {
      toast(err.message, true);
      return;
    }
    state.undoDepth = data.undo_depth;
    if (data.moved.length) {
      const n = data.moved.length;
      toast((n === 1 ? data.moved[0].name : n + " items") + "  →  " + baseName(destDir) + "   (undo: z)");
    }
    if (data.errors && data.errors.length) {
      toast(data.errors[0].error + (data.errors.length > 1 ? " (+" + (data.errors.length - 1) + " more)" : ""), true);
    }
    const touched = new Set([destDir]);
    for (const m of data.moved) touched.add(parentOf(m.from));
    await refreshAll();
    tree.refreshAround(touched);
  }

  async function undo() {
    let data;
    try {
      data = await api("/api/undo", {});
    } catch (err) {
      toast(err.message, true);
      return;
    }
    state.undoDepth = data.undo_depth;
    toast("Put back " + data.name);
    const home = parentOf(data.to);
    await refreshAll();
    for (const p of state.panes) if (p.dir === home) p.load(home, { focusPath: data.to, keepSelection: true });
    tree.refreshAround(new Set([home, parentOf(data.from)]));
  }

  // --- Folder tree -----------------------------------------------------------
  const tree = {
    nodes: new Map(),

    init() {
      el.tree.innerHTML = "";
      for (const r of ROOTS) this.add({ name: r, path: r, subdirs: 1 }, el.tree, 0);
    },

    add(info, container, depth) {
      const li = document.createElement("li");
      const row = document.createElement("div");
      row.className = "node-row";
      row.title = info.path;
      const caret = document.createElement("span");
      caret.className = "caret" + (info.subdirs ? "" : " leaf");
      caret.textContent = "▶";
      const icon = document.createElement("span"); icon.className = "node-icon"; icon.textContent = "▰";
      const name = document.createElement("span"); name.className = "node-name"; name.textContent = info.name;
      const count = document.createElement("span"); count.className = "node-count";
      const media = (info.images || 0) + (info.videos || 0);
      count.textContent = media ? String(media) : "";
      row.append(caret, icon, name, count);
      const children = document.createElement("ul");
      children.hidden = true;
      li.append(row, children);
      container.appendChild(li);

      const node = { path: info.path, info, li, row, caret, children, depth, loaded: false, expanded: false };
      this.nodes.set(info.path, node);
      caret.addEventListener("click", (e) => { e.stopPropagation(); this.toggle(info.path); });
      row.addEventListener("click", () => { activePane().load(info.path); });
      dropTarget(row, () => info.path);
      return node;
    },

    async expand(path) {
      const n = this.nodes.get(path);
      if (!n) return;
      if (!n.loaded) {
        const loading = document.createElement("li");
        loading.className = "tree-loading"; loading.textContent = "…";
        n.children.innerHTML = ""; n.children.appendChild(loading); n.children.hidden = false;
        let data;
        try { data = await api("/api/dirs?path=" + encodeURIComponent(path)); }
        catch (err) { n.children.innerHTML = ""; n.children.hidden = true; toast(err.message, true); return; }
        n.children.innerHTML = "";
        for (const d of data.dirs) this.add(d, n.children, n.depth + 1);
        n.loaded = true;
        n.caret.classList.toggle("leaf", data.dirs.length === 0);
      }
      n.expanded = true;
      n.children.hidden = false;
      n.caret.textContent = "▼";
    },

    collapse(path) {
      const n = this.nodes.get(path);
      if (!n) return;
      n.expanded = false;
      n.children.hidden = true;
      n.caret.textContent = "▶";
    },

    toggle(path) {
      const n = this.nodes.get(path);
      if (n && n.expanded) this.collapse(path); else this.expand(path);
    },

    // Expand the chain of folders down to `dir` and mark it as current.
    async reveal(dir) {
      const root = rootOf(dir);
      if (!root) return;
      await this.expand(root);
      const rel = dir.slice(root.length).split("/").filter(Boolean);
      let cur = root;
      for (const s of rel) {
        cur = joinPath(cur, s);
        if (cur !== dir && this.nodes.has(cur)) await this.expand(cur);
      }
      this.nodes.forEach((n) => n.row.classList.remove("current"));
      const n = this.nodes.get(dir);
      if (n) { n.row.classList.add("current"); n.row.scrollIntoView({ block: "nearest" }); }
    },

    // Re-list the children of every loaded node in `dirs` and of their parents
    // (that is where counts and previews live), keeping what was expanded.
    async refreshAround(dirs) {
      const targets = new Set();
      for (const d of dirs) { targets.add(d); targets.add(parentOf(d)); }
      for (const path of targets) {
        const n = this.nodes.get(path);
        if (!n || !n.loaded) continue;
        const openKids = [];
        n.children.querySelectorAll(":scope > li").forEach((li) => {
          const kid = Array.from(this.nodes.values()).find((k) => k.li === li);
          if (kid && kid.expanded) openKids.push(kid.path);
        });
        this.dropSubtree(n);
        n.loaded = false;
        if (n.expanded) {
          await this.expand(path);
          for (const k of openKids) if (this.nodes.has(k)) await this.expand(k);
        }
      }
      const p = activePane();
      if (p && p.dir) this.reveal(p.dir);
    },

    dropSubtree(n) {
      for (const [path, k] of Array.from(this.nodes.entries())) {
        if (k !== n && (path === n.path || path.startsWith(n.path === "/" ? "/" : n.path + "/")) && k.depth > n.depth) this.nodes.delete(path);
      }
      n.children.innerHTML = "";
    },
  };

  // --- Global status -----------------------------------------------------------
  function renderStatus() {
    const p = activePane();
    const it = p ? p.current() : null;
    el.statusL.innerHTML = "";
    if (it) {
      const b = document.createElement("b");
      b.textContent = it.name;
      const detail = it.type === "dir" ? dirMeta(it) : fmtSize(it.size) + "  " + fmtDate(it.mtime);
      el.statusL.append((p.cursor + 1) + " / " + p.view.length + "  ", b, "  " + detail);
    }
    const parts = [];
    if (state.undoDepth) parts.push("undo ×" + state.undoDepth);
    if (!HAS_FFMPEG) parts.push("no ffmpeg: video previews by browser");
    el.statusR.textContent = parts.join("  ·  ");
  }

  // --- New folder ------------------------------------------------------------
  function openModal() {
    const p = activePane();
    if (!p || !p.dir) return;
    el.modalWhere.textContent = "Inside " + p.dir;
    el.modalInput.value = "";
    el.modal.hidden = false;
    el.modalInput.focus();
  }
  function closeModal() { el.modal.hidden = true; }

  async function createFolder(name) {
    name = name.trim();
    if (!name) return;
    const p = activePane();
    try {
      const data = await api("/api/mkdir", { parent: p.dir, name });
      closeModal();
      toast("Created " + data.name);
      await refreshAll();
      p.load(p.dir, { focusPath: data.path, keepSelection: true });
      tree.refreshAround(new Set([p.dir]));
    } catch (err) {
      toast(err.message, true);
    }
  }

  // --- Lightbox --------------------------------------------------------------
  function openLightbox(pane) {
    const it = pane.current();
    if (!it || it.type !== "file") return;
    state.lightbox = pane;
    el.lightbox.hidden = false;
    renderLightbox();
  }
  function closeLightbox() {
    state.lightbox = null;
    el.lightbox.hidden = true;
    el.lbStage.innerHTML = "";
  }
  function lightboxStep(dir) {
    const p = state.lightbox;
    if (!p) return;
    let i = p.cursor + dir;
    while (i >= 0 && i < p.view.length && p.view[i].type !== "file") i += dir;
    if (i >= 0 && i < p.view.length) p.setCursor(i);
  }
  function renderLightbox() {
    const p = state.lightbox;
    const f = p ? p.current() : null;
    el.lbStage.innerHTML = "";
    if (!f || f.type !== "file") { closeLightbox(); return; }
    let node;
    if (f.kind === "video") {
      node = document.createElement("video");
      node.controls = true; node.autoplay = true; node.playsInline = true;
      node.src = mediaUrl(f.path);
    } else {
      node = document.createElement("img");
      node.src = mediaUrl(f.path); node.alt = f.name;
    }
    el.lbStage.appendChild(node);
    el.lbCaption.innerHTML = "";
    const files = p.files_();
    const pos = files.findIndex((x) => x.path === f.path) + 1;
    const b = document.createElement("b");
    b.textContent = f.name;
    el.lbCaption.append(pos + " / " + files.length + "   ", b, "   " + fmtSize(f.size) + "   " + fmtDate(f.mtime));
  }

  // --- Help ------------------------------------------------------------------
  function toggleHelp(force) {
    el.help.hidden = force === undefined ? !el.help.hidden : !force;
  }

  // --- Keyboard --------------------------------------------------------------
  function onKey(e) {
    const tag = (e.target.tagName || "").toLowerCase();
    const inField = tag === "input" || tag === "textarea" || tag === "select";

    if (!el.modal.hidden) {
      if (e.key === "Escape") { e.preventDefault(); closeModal(); }
      return;
    }
    if (!el.help.hidden) {
      if (e.key === "Escape" || e.key === "?") { e.preventDefault(); toggleHelp(false); }
      return;
    }
    if (inField) {
      if (e.key === "Escape") e.target.blur();
      return;
    }
    if (e.altKey) return;
    const p = activePane();
    if (!p) return;

    if (e.ctrlKey || e.metaKey) {
      const k = e.key.toLowerCase();
      if (k === "a") { e.preventDefault(); p.view.forEach((it) => p.selected.add(it.path)); p.updateClasses(); p.renderFoot(); }
      else if (k === "z") { e.preventDefault(); undo(); }
      else if (k === "enter") { e.preventDefault(); p.open(true); }
      return;
    }

    if (state.lightbox) {
      switch (e.key) {
        case "ArrowRight": case "ArrowDown": case " ": e.preventDefault(); lightboxStep(1); return;
        case "ArrowLeft": case "ArrowUp": e.preventDefault(); lightboxStep(-1); return;
        case "Escape": case "Enter": e.preventDefault(); closeLightbox(); return;
        case "z": e.preventDefault(); undo(); return;
      }
      return;
    }

    const cols = p.columns();
    switch (e.key) {
      case "ArrowRight": e.preventDefault(); p.setCursor(p.cursor + 1, { extend: e.shiftKey }); return;
      case "ArrowLeft":  e.preventDefault(); p.setCursor(p.cursor - 1, { extend: e.shiftKey }); return;
      case "ArrowDown":  e.preventDefault(); p.setCursor(p.cursor + cols, { extend: e.shiftKey }); return;
      case "ArrowUp":    e.preventDefault(); p.setCursor(p.cursor - cols, { extend: e.shiftKey }); return;
      case "Home":       e.preventDefault(); p.setCursor(0, { extend: e.shiftKey }); return;
      case "End":        e.preventDefault(); p.setCursor(p.view.length - 1, { extend: e.shiftKey }); return;
      case "PageDown":   e.preventDefault(); p.setCursor(p.cursor + cols * 3, { extend: e.shiftKey }); return;
      case "PageUp":     e.preventDefault(); p.setCursor(p.cursor - cols * 3, { extend: e.shiftKey }); return;
      case "Enter":      e.preventDefault(); p.open(false); return;
      case " ":          e.preventDefault(); p.toggleCurrent(); return;
      case "Escape":     e.preventDefault(); p.selected.clear(); p.updateClasses(); p.renderFoot(); return;
      case "Backspace":  e.preventDefault(); p.goUp(); return;
      case "Tab":
        if (state.panes.length > 1) { e.preventDefault(); setActive(state.active === 0 ? 1 : 0); }
        return;
      case "?": e.preventDefault(); toggleHelp(true); return;
      case "/": e.preventDefault(); p.showPathForm(); return;
      case "[": e.preventDefault(); setTileSize(state.tileSize - 30); return;
      case "]": e.preventDefault(); setTileSize(state.tileSize + 30); return;
      case "n": e.preventDefault(); openModal(); return;
      case "z": e.preventDefault(); undo(); return;
      case "r": e.preventDefault(); refreshAll(); return;
    }
  }

  function setTileSize(px) {
    state.tileSize = Math.max(110, Math.min(420, px));
    document.documentElement.style.setProperty("--tile-size", state.tileSize + "px");
    el.size.value = state.tileSize;
    remember("tileSize", state.tileSize);
  }

  function reapplyView() {
    for (const p of state.panes) {
      const cur = p.current();
      p.rebuildView();
      if (cur) p.cursor = Math.max(0, p.view.findIndex((it) => it.path === cur.path));
      p.clampCursor();
      p.render();
    }
  }

  // --- Wire up ----------------------------------------------------------------
  function bind() {
    document.addEventListener("keydown", onKey);
    el.btnNew.addEventListener("click", openModal);
    el.btnUndo.addEventListener("click", undo);
    el.btnSplit.addEventListener("click", () => toggleSplit());
    el.sort.addEventListener("change", () => { state.sort = el.sort.value; remember("sort", state.sort); reapplyView(); });
    el.filter.addEventListener("change", () => { state.filter = el.filter.value; remember("filter", state.filter); reapplyView(); });
    el.size.addEventListener("input", () => setTileSize(parseInt(el.size.value, 10)));
    el.btnHelp.addEventListener("click", () => toggleHelp(true));
    el.helpClose.addEventListener("click", () => toggleHelp(false));
    el.help.addEventListener("click", (e) => { if (e.target === el.help) toggleHelp(false); });
    el.modalCancel.addEventListener("click", closeModal);
    el.modal.addEventListener("click", (e) => { if (e.target === el.modal) closeModal(); });
    el.modalForm.addEventListener("submit", (e) => { e.preventDefault(); createFolder(el.modalInput.value); });
    el.lbClose.addEventListener("click", closeLightbox);
    el.lbPrev.addEventListener("click", () => lightboxStep(-1));
    el.lbNext.addEventListener("click", () => lightboxStep(1));
    el.lightbox.addEventListener("click", (e) => { if (e.target === el.lightbox || e.target === el.lbStage) closeLightbox(); });
  }

  async function init() {
    bind();
    state.sort = SORTS.includes(recall("sort", "name")) ? recall("sort", "name") : "name";
    state.filter = FILTERS.includes(recall("filter", "all")) ? recall("filter", "all") : "all";
    el.sort.value = state.sort;
    el.filter.value = state.filter;
    setTileSize(recall("tileSize", 200));
    try { await fetchCsrf(); } catch (e) { toast("Could not get a session token", true); }
    tree.init();

    const p = new Pane(0);
    state.panes.push(p);
    el.panes.appendChild(p.el);
    setActive(0);
    const q = new URLSearchParams(location.search).get("dir");
    const start = q || recall("dir0", null) || body.dataset.startDir;
    const ok = await p.load(start);
    if (!ok && start !== body.dataset.startDir) await p.load(body.dataset.startDir);
    if (recall("split", false)) toggleSplit(true);
    try {
      const h = await api("/api/history");
      state.undoDepth = h.undo_depth;
      renderStatus();
    } catch (e) { /* cosmetic */ }
  }

  init();
})();
