/* QuickFind UI. Text only (Kit.el), no innerHTML. */
(() => {
  "use strict";
  const { $, el, clear, toast, post, bytes, num, dt, ago, dur, initTheme } = window.Kit;
  initTheme($("#theme"));
  const S = { status: null, rows: [], sel: -1, limit: 300, more: false, marks: [], seq: 0, ctl: null, view: "search", sort: "name", dir: "asc",
              lastEntries: -1, lastSearchAt: 0, edit: null, dirty: false, total: 0 };
  try { const o = JSON.parse(localStorage.getItem("qf.sort") || "{}"); if (["name", "size", "date", "path"].includes(o.sort)) S.sort = o.sort; if (o.dir === "desc") S.dir = "desc"; } catch { /* ignore */ }
  const TITLES = { search: "Search", settings: "Index & settings" };
  const DIRTXT = { name: ["A→Z", "Z→A"], path: ["A→Z", "Z→A"], size: ["Small→Large", "Large→Small"], date: ["Old→New", "New→Old"] };

  function show(v) {
    S.view = v;
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
    $("#view-title").textContent = TITLES[v];
    $("#v-search").hidden = v !== "search";
    $("#v-settings").hidden = v !== "settings";
    if (v === "settings") renderSettings(true);
    if (v === "search") $("#q").focus();
  }
  document.querySelectorAll(".nav-btn").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

  // ------------------------------------------------------------ search
  const val = (id) => $(id).value;
  function dateBounds() {
    const k = val("#f-date"), now = new Date();
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
    if (k === "today") return [midnight, ""];
    if (k === "7" || k === "30" || k === "365") return [Date.now() / 1000 - Number(k) * 86400, ""];
    if (k === "custom") {
      const f = val("#f-dfrom"), t = val("#f-dto");
      const lo = f ? new Date(f + "T00:00:00").getTime() / 1000 : "";
      const hi = t ? new Date(t + "T23:59:59").getTime() / 1000 : "";
      return [lo, hi];
    }
    return ["", ""];
  }
  function sizeVal(id, unit) {
    const v = val(id);
    if (v === "" || isNaN(Number(v)) || Number(v) < 0) return "";
    return String(Math.round(Number(v) * Number(val(unit))));
  }
  function spec() {
    const [dmin, dmax] = dateBounds();
    return { q: val("#q"), type: val("#f-type"), ext: val("#f-ext"), smin: sizeVal("#f-smin", "#f-smin-u"), smax: sizeVal("#f-smax", "#f-smax-u"),
             dmin, dmax, sort: S.sort, dir: S.dir, limit: S.limit };
  }
  const filtersActive = () => val("#f-type") !== "any" || val("#f-ext").trim() || val("#f-smin") !== "" || val("#f-smax") !== "" || val("#f-date") !== "any";
  let timer = null;
  function schedule() { clearTimeout(timer); S.limit = 300; timer = setTimeout(runSearch, 45); }

  async function runSearch() {
    const sp = spec();
    const my = ++S.seq;
    if (S.ctl) S.ctl.abort();
    S.ctl = new AbortController();
    const qs = new URLSearchParams();
    Object.entries(sp).forEach(([k, v]) => { if (v !== "" && v != null) qs.set(k, String(v)); });
    let res;
    try {
      const r = await fetch("/api/search?" + qs.toString(), { cache: "no-store", signal: S.ctl.signal });
      res = await r.json();
      if (!r.ok) { if (my === S.seq) { showEmpty(res.error || "Search failed", true); } return; }
    } catch (e) { if (e.name !== "AbortError" && my === S.seq) showEmpty("Cannot reach QuickFind", true); return; }
    if (my !== S.seq || res.cancelled) return;
    S.lastSearchAt = Date.now();
    const keep = S.sel >= 0 && S.rows[S.sel] ? S.rows[S.sel].id : null;
    S.rows = res.results; S.more = res.more; S.marks = res.marks || [];
    S.sel = keep == null ? -1 : S.rows.findIndex((r) => r.id === keep);
    renderRows(res);
  }

  function highlight(text, marks) {
    const lc = text.toLowerCase();
    if (!marks.length || lc.length !== text.length) return [text];
    const hit = new Array(text.length).fill(false);
    for (const m of marks) {
      if (!m) continue;
      let i = lc.indexOf(m);
      while (i !== -1) { for (let k = i; k < i + m.length; k++) hit[k] = true; i = lc.indexOf(m, i + m.length); }
    }
    const out = []; let start = 0;
    for (let i = 1; i <= text.length; i++) {
      if (i === text.length || hit[i] !== hit[start]) {
        const piece = text.slice(start, i);
        out.push(hit[start] ? el("span", { className: "hit", text: piece }) : piece);
        start = i;
      }
    }
    return out;
  }
  function extBadge(r) {
    if (r.is_dir) return el("span", { className: "kind dir", text: "DIR" });
    const i = r.name.lastIndexOf(".");
    const e = i > 0 && i < r.name.length - 1 ? r.name.slice(i + 1).toUpperCase() : "";
    return el("span", { className: "kind", text: e.length > 4 ? e.slice(0, 4) : e || "FILE" });
  }
  function showEmpty(msg, isErr) {
    clear($("#rows"));
    const e = $("#empty");
    e.textContent = msg; e.className = "empty center" + (isErr ? " error" : ""); e.hidden = false;
    $("#count").textContent = ""; $("#btn-more").hidden = true; $("#actions").hidden = true;
  }
  function renderRows(res) {
    const body = clear($("#rows"));
    const idx = S.status ? S.status.index : null;
    if (!S.rows.length) {
      let msg;
      if (res.empty_query) msg = idx && idx.entries ? "Type a name to search " + num(idx.entries) + " indexed files and folders." : "Nothing indexed yet. QuickFind is building its index.";
      else msg = idx && idx.phase === "scanning" ? "No matches yet. The index is still being built, so results will keep appearing." : "No matches.";
      showEmpty(msg, false);
      $("#count").textContent = res.empty_query ? "" : "0 results · " + res.ms + " ms";
      return;
    }
    $("#empty").hidden = true;
    S.rows.forEach((r, i) => {
      const nameCell = el("td", { title: r.name }, extBadge(r), highlight(r.name, S.marks), (r.flags & 4) ? el("span", { className: "tag", text: "online-only" }) : null);
      const tr = el("tr", { className: "selectable" + (i === S.sel ? " selected" : ""), "data-i": i },
        nameCell,
        el("td", { text: r.folder, title: r.folder }),
        el("td", { className: "num", text: r.is_dir ? "" : bytes(r.size) }),
        el("td", { text: r.mtime ? dt(r.mtime) : "—" }));
      tr.addEventListener("click", () => select(i));
      tr.addEventListener("dblclick", () => openItem(S.rows[i]));
      body.appendChild(tr);
    });
    $("#count").textContent = (res.more ? num(S.rows.length) + "+ results" : num(S.rows.length) + (S.rows.length === 1 ? " result" : " results")) + " · " + res.ms + " ms";
    $("#btn-more").hidden = !(res.more && S.limit < (S.status ? S.status.max_limit : 5000));
    updateActions();
  }
  function select(i, scroll) {
    if (i < 0 || i >= S.rows.length) return;
    S.sel = i;
    document.querySelectorAll("#rows tr").forEach((tr, k) => tr.classList.toggle("selected", k === i));
    if (scroll) { const tr = $("#rows").children[i]; if (tr && tr.scrollIntoView) tr.scrollIntoView({ block: "nearest" }); }
    updateActions();
  }
  function updateActions() {
    const r = S.rows[S.sel];
    $("#actions").hidden = !r;
    if (r) $("#sel-path").textContent = r.folder + r.name;
  }
  const current = () => S.rows[S.sel] || null;

  async function copyText(t) {
    try { await navigator.clipboard.writeText(t); return true; } catch { /* fall back */ }
    const ta = el("textarea", { className: "offscreen", "aria-hidden": "true" });
    ta.value = t; document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    return ok;
  }
  async function copyPath(r) {
    r = r || current(); if (!r) return;
    toast((await copyText(r.folder + r.name)) ? "Path copied" : "Could not copy. Select the path text and press Ctrl+C.", false);
  }
  async function act(kind, r, extra) {
    r = r || current(); if (!r) return;
    try {
      const res = await post("/api/" + kind, Object.assign({ id: r.id }, extra || {}));
      if (res.confirm) return confirmRun(res.confirm, kind, r);
      if (!res.ok) { toast(res.error || "Could not do that", true); schedule(); setTimeout(runSearch, 4500); setTimeout(runSearch, 8000); }
    } catch (e) { toast(e.message, true); }
  }
  const openItem = (r) => act("open", r);
  const revealItem = (r) => act("reveal", r);
  function modal(children) {
    const root = $("#modal-root");
    const back = el("div", { className: "modal-back", role: "presentation" }, el("div", { className: "modal", role: "dialog", "aria-modal": "true" }, children));
    root.appendChild(back);
    const close = () => { if (back.parentNode) back.parentNode.removeChild(back); document.removeEventListener("keydown", onKey); const q = $("#q"); if (S.view === "search") q.focus(); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    return { close, back };
  }
  function confirmDialog(title, text, label, onOk, danger) {
    const ok = el("button", { type: "button", className: danger ? "danger" : "primary", text: label });
    const no = el("button", { type: "button", text: "Cancel" });
    const m = modal([el("h2", { text: title }), el("p", { text }), el("div", { className: "actions" }, no, ok)]);
    no.addEventListener("click", m.close);
    ok.addEventListener("click", () => { m.close(); onOk(); });
    no.focus();
  }
  function confirmRun(kind, what, r) {
    if (kind === "exec") confirmDialog("Run " + r.name + "?", "This is a program or script. Opening it runs it, the same as double-clicking it in File Explorer.", "Run it", () => act(what, r, { confirm_exec: true }), true);
    else confirmDialog("Download " + r.name + "?", "This file is stored online only (OneDrive). Opening it downloads it to this PC.", "Download and open", () => act(what, r, { allow_download: true }));
  }
  $("#a-open").addEventListener("click", () => openItem());
  $("#a-reveal").addEventListener("click", () => revealItem());
  $("#a-copy").addEventListener("click", () => copyPath());

  ["#q", "#f-ext", "#f-smin", "#f-smax"].forEach((s) => $(s).addEventListener("input", schedule));
  ["#f-type", "#f-smin-u", "#f-smax-u", "#f-dfrom", "#f-dto"].forEach((s) => $(s).addEventListener("change", schedule));
  $("#f-date").addEventListener("change", () => { $("#f-custom").hidden = val("#f-date") !== "custom"; schedule(); });
  $("#btn-more").addEventListener("click", () => { S.limit = Math.min(S.status ? S.status.max_limit : 5000, S.limit + 700); runSearch(); });
  $("#f-clear").addEventListener("click", () => {
    $("#f-type").value = "any"; $("#f-ext").value = ""; $("#f-smin").value = ""; $("#f-smax").value = ""; $("#f-date").value = "any";
    $("#f-dfrom").value = ""; $("#f-dto").value = ""; $("#f-custom").hidden = true; schedule();
  });
  function syncSortUi() {
    $("#f-sort").value = S.sort;
    const b = $("#f-dir"), t = DIRTXT[S.sort][S.dir === "desc" ? 1 : 0];
    b.textContent = t; b.setAttribute("aria-label", "Sort direction: " + (S.dir === "desc" ? "descending" : "ascending"));
    document.querySelectorAll("button.th").forEach((h) => {
      const on = h.dataset.sort === S.sort;
      h.classList.toggle("on", on);
      h.setAttribute("data-arrow", on ? (S.dir === "desc" ? " ▼" : " ▲") : "");
      h.parentElement.setAttribute("aria-sort", on ? (S.dir === "desc" ? "descending" : "ascending") : "none");
    });
    try { localStorage.setItem("qf.sort", JSON.stringify({ sort: S.sort, dir: S.dir })); } catch { /* ignore */ }
  }
  function setSort(s, d) { S.sort = s; S.dir = d; syncSortUi(); schedule(); }
  $("#f-sort").addEventListener("change", () => setSort(val("#f-sort"), S.dir));
  $("#f-dir").addEventListener("click", () => setSort(S.sort, S.dir === "asc" ? "desc" : "asc"));
  document.querySelectorAll("button.th").forEach((h) => h.addEventListener("click", () => {
    const s = h.dataset.sort;
    setSort(s, s === S.sort ? (S.dir === "asc" ? "desc" : "asc") : (s === "size" || s === "date" ? "desc" : "asc"));
  }));

  $("#q").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); select(Math.min(S.rows.length - 1, S.sel + 1), true); }
    else if (e.key === "ArrowUp") { e.preventDefault(); select(Math.max(0, S.sel - 1), true); }
    else if (e.key === "Enter") {
      e.preventDefault();
      if (S.sel < 0 && S.rows.length) select(0, true);
      if (e.ctrlKey) revealItem(); else openItem(current());
    } else if (e.key === "Escape") { if ($("#q").value) { $("#q").value = ""; schedule(); } }
  });
  document.addEventListener("keydown", (e) => {
    if (S.view !== "search" || $("#modal-root").firstChild) return;
    if (e.ctrlKey && e.shiftKey && (e.key === "C" || e.key === "c")) { e.preventDefault(); copyPath(); }
    else if ((e.key === "/" || (e.ctrlKey && (e.key === "l" || e.key === "f"))) && document.activeElement !== $("#q") && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); $("#q").focus(); $("#q").select(); }
  });

  // ------------------------------------------------------------ status polling
  const phaseText = (idx) => {
    if (idx.phase === "paused") return "Indexing paused · " + num(idx.entries) + " items";
    if (idx.phase === "scanning") {
      const kind = idx.kind === "startup" || idx.kind === "periodic" || idx.kind === "manual" ? "Checking for changes" : "Building the index";
      return kind + " · " + num(idx.seen) + " items" + (idx.root_total > 1 ? " · drive " + idx.root_index + " of " + idx.root_total : "");
    }
    if (idx.phase === "resetting") return "Clearing the index…";
    if (idx.phase === "updating") return "Applying changes · " + num(idx.entries) + " items";
    return num(idx.entries) + " items indexed";
  };
  let firstStatus = true;
  async function loadStatus() {
    let delay = 4000;
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      S.status = await r.json();
      const idx = S.status.index;
      const busy = idx.phase === "scanning" || idx.phase === "updating" || idx.phase === "resetting";
      delay = busy ? 900 : 4000;
      $("#status").textContent = phaseText(idx); $("#status").className = "status";
      $("#side-text").textContent = phaseText(idx);
      const bar = $("#side-bar"), fill = $("#side-bar-fill");
      bar.hidden = idx.phase !== "scanning";
      if (!bar.hidden) { if (idx.pct == null) { fill.className = "indet"; } else { fill.className = ""; fill.style.width = idx.pct + "%"; } }
      renderBanner(idx);
      if (S.view === "settings") renderSettings(false);
      const changed = idx.entries !== S.lastEntries;
      S.lastEntries = idx.entries;
      if (S.view === "search" && (firstStatus || (changed && Date.now() - S.lastSearchAt > 2500))) runSearch();
      firstStatus = false;
    } catch (e) { $("#status").textContent = "Cannot reach QuickFind"; $("#status").className = "status error"; }
    return delay;
  }
  let pollTimer = null;
  async function tick() {
    clearTimeout(pollTimer);
    const delay = await loadStatus();
    pollTimer = setTimeout(tick, delay);
  }
  function renderBanner(idx) {
    const b = $("#banner");
    const firstBuild = S.status.drives.some((d) => d.enabled && !d.built);
    if (idx.phase === "scanning" && (firstBuild || idx.kind === "rebuild" || idx.kind === "settings")) {
      clear(b);
      b.hidden = false; b.className = "notice";
      const fill = el("span", {});
      b.appendChild(el("div", { text: (idx.kind === "settings" ? "Updating the index — " : "Building the index for the first time — ") + "you can already search what is indexed so far. " + num(idx.seen) + " items" + (idx.pct != null ? " (about " + idx.pct + "%)" : "") + "." }));
      b.appendChild(el("div", { className: "small muted", text: idx.current || "" }));
      b.appendChild(el("div", { className: "bar" }, fill));
      if (idx.pct == null) fill.className = "indet"; else fill.style.width = idx.pct + "%";
    } else if (idx.phase === "paused") {
      clear(b); b.hidden = false; b.className = "notice warn"; b.textContent = "Indexing is paused. New and changed files will not show up until you resume it in Index & settings.";
    } else if (!S.status.drives.some((d) => d.enabled) && S.status.drives.length) {
      clear(b); b.hidden = false; b.className = "notice warn"; b.textContent = "No drives are included. Turn one on in Index & settings.";
    } else b.hidden = true;
  }

  // ------------------------------------------------------------ settings
  function renderSettings(reset) {
    const st = S.status; if (!st) return;
    if (reset || !S.edit) { S.edit = JSON.parse(JSON.stringify(st.settings)); S.dirty = false; }
    const idx = st.index;
    const cards = clear($("#idx-cards"));
    const card = (label, value, sub) => el("div", { className: "card" }, el("div", { className: "label", text: label }), el("div", { className: "value", text: value }), sub ? el("div", { className: "sub", text: sub }) : null);
    cards.append(card("Items", num(idx.entries), num(idx.dirs_total) + " folders"), card("Index size", bytes(idx.db_bytes), idx.fts ? "with fast substring index" : "plain scan (no FTS5)"),
      card("Status", phaseText(idx).split(" · ")[0], idx.phase === "scanning" && idx.current ? idx.current : (idx.last_done ? "last finished " + ago(idx.last_done, st.now) : "")),
      card("Next full check", idx.next_scan && idx.phase !== "paused" ? "in " + dur(Math.max(0, idx.next_scan - st.now)) : "—", "live changes are picked up as they happen"));
    const w = st.watchers.length ? st.watchers.map((x) => x.root + " " + x.state).join(" · ") : (st.windows ? "no live watchers" : "live watching works on Windows");
    $("#idx-detail").textContent = "Live watcher: " + w + " · " + (idx.errors ? num(idx.errors) + " folders could not be read (permissions) · " : "") + "data: " + idx.data_dir;
    $("#btn-pause").textContent = idx.paused ? "Resume indexing" : "Pause indexing";
    if (!reset) return;
    // drives
    const dl = clear($("#drive-list"));
    if (!st.drives.length) dl.appendChild(el("div", { className: "muted", text: "No fixed drives found." }));
    st.drives.forEach((d) => {
      const on = !S.edit.drives_off.some((x) => x.toLowerCase() === d.root.toLowerCase());
      const cb = el("input", { type: "checkbox", checked: on, "data-root": d.root });
      cb.addEventListener("change", () => {
        const off = S.edit.drives_off.filter((x) => x.toLowerCase() !== d.root.toLowerCase());
        if (!cb.checked) off.push(d.root);
        S.edit.drives_off = off; markDirty();
      });
      dl.appendChild(el("label", { className: "check" }, cb, d.root + (d.label ? "  " + d.label : ""), d.enabled && !d.built ? el("span", { className: "badge warn", text: "not indexed yet" }) : null));
    });
    $("#builtin").textContent = st.builtin_skip.join(", ");
    renderExcludes();
    $("#o-system").checked = S.edit.skip_system; $("#o-hidden").checked = S.edit.skip_hidden;
    $("#o-speed").value = S.edit.speed; $("#o-rescan").value = String(S.edit.rescan_min);
    $("#btn-apply").disabled = !S.dirty;
  }
  function renderExcludes() {
    const ul = clear($("#ex-list"));
    if (!S.edit.excludes.length) ul.appendChild(el("li", { className: "none", text: "No folders skipped" }));
    S.edit.excludes.forEach((p, i) => {
      const rm = el("button", { type: "button", className: "small", text: "Remove", "aria-label": "Remove " + p });
      rm.addEventListener("click", () => { S.edit.excludes.splice(i, 1); markDirty(); renderExcludes(); });
      ul.appendChild(el("li", {}, el("span", { text: p }), rm));
    });
  }
  function markDirty() { S.dirty = true; $("#btn-apply").disabled = false; $("#apply-note").textContent = ""; }
  function addExclude(p) {
    p = (p || "").trim();
    if (!p) return;
    if (!S.edit.excludes.some((x) => x.toLowerCase() === p.toLowerCase())) S.edit.excludes.push(p);
    $("#ex-add").value = ""; markDirty(); renderExcludes();
  }
  $("#ex-add-btn").addEventListener("click", () => addExclude(val("#ex-add")));
  $("#ex-add").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addExclude(val("#ex-add")); } });
  $("#ex-browse").addEventListener("click", async () => {
    try { const r = await post("/api/pick-folder", {}); if (r.ok && r.path) addExclude(r.path); else if (r.error) toast(r.error, true); } catch (e) { toast(e.message, true); }
  });
  $("#o-system").addEventListener("change", () => { S.edit.skip_system = $("#o-system").checked; markDirty(); });
  $("#o-hidden").addEventListener("change", () => { S.edit.skip_hidden = $("#o-hidden").checked; markDirty(); });
  $("#o-speed").addEventListener("change", () => { S.edit.speed = val("#o-speed"); markDirty(); });
  $("#o-rescan").addEventListener("change", () => { S.edit.rescan_min = Number(val("#o-rescan")); markDirty(); });
  $("#btn-apply").addEventListener("click", async () => {
    try {
      const r = await post("/api/settings", S.edit);
      S.edit = r.settings; S.dirty = false; $("#btn-apply").disabled = true; $("#apply-note").textContent = "Saved. The index is being updated.";
      toast("Settings saved");
      S.status.settings = r.settings; tick();
    } catch (e) { toast(e.message, true); }
  });
  $("#btn-rescan").addEventListener("click", async () => { try { await post("/api/rescan", {}); toast("Checking for changes"); tick(); } catch (e) { toast(e.message, true); } });
  $("#btn-pause").addEventListener("click", async () => {
    try { await post("/api/pause", { paused: !(S.status && S.status.index.paused) }); tick(); } catch (e) { toast(e.message, true); }
  });
  $("#btn-rebuild").addEventListener("click", () => confirmDialog("Delete the index and rebuild it?", "Only QuickFind's own index is deleted — none of your files. Searching finds fewer results until the rebuild is done, which can take several minutes.", "Delete and rebuild",
    async () => { try { await post("/api/rebuild", { confirm: true }); toast("Rebuilding the index"); tick(); } catch (e) { toast(e.message, true); } }, true));

  syncSortUi();
  $("#q").focus();
  runSearch();
  tick();
})();
