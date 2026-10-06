"use strict";
/* PiVault web UI. Plain JS, no build step.
   Security note: user-controlled text (file names!) is only ever inserted with
   textContent / text nodes, never innerHTML. */

const $ = (sel, root = document) => root.querySelector(sel);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const state = {
  user: null, path: "", entries: [], used: 0, quota: 0, config: {},
  view: store.get("pv.view") === "grid" ? "grid" : "list",
};

/* ---------------------------------------------------------------- icons */
const ICONS = {
  upload: '<path d="M12 16V4m0 0L8 8m4-4 4 4M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
  "folder-up": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 16v-5m0 0-2 2m2-2 2 2"/>',
  "folder-plus": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 10v6m-3-3h6"/>',
  list: '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
  grid: '<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/>',
  download: '<path d="M12 4v12m0 0-4-4m4 4 4-4M4 20h16"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-12M9 7V4h6v3"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  "chev-left": '<path d="m15 5-7 7 7 7"/>',
  "chev-right": '<path d="m9 5 7 7-7 7"/>',
  logout: '<path d="M10 4H6a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4M15 8l4 4-4 4M19 12H9"/>',
};
function icon(name) {
  const t = document.createElement("template");
  // constant, developer-written markup only
  t.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  return t.content.firstElementChild;
}

/* -------------------------------------------------------------- helpers */
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return el;
}
const enc = encodeURIComponent;
const join = (a, b) => (a ? `${a}/${b}` : b);
const fileURL = (p, dl) => `/api/file?path=${enc(p)}${dl ? "&download=1" : ""}`;
const extOf = (n) => (n.includes(".") ? n.split(".").pop().toLowerCase() : "");
const MEDIA = {
  image: ["jpg", "jpeg", "png", "gif", "webp", "avif"],
  video: ["mp4", "m4v", "webm", "mov"],
  audio: ["mp3", "m4a", "ogg", "wav"],
};
function mediaKind(name) {
  const e = extOf(name);
  return Object.keys(MEDIA).find((k) => MEDIA[k].includes(e)) || null;
}
function human(n) {
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n >= 100 ? n.toFixed(0) : n.toFixed(1)} ${u[i]}`;
}
const fmtDate = (t, long) => new Date(t * 1000).toLocaleString(undefined,
  long ? { dateStyle: "medium", timeStyle: "short" } : { dateStyle: "medium" });
function hueOf(s) { let n = 7; for (const c of s) n = (n * 31 + c.charCodeAt(0)) >>> 0; return n % 360; }
const folderName = () => state.path.split("/").filter(Boolean).pop() || "Your files";

function toast(msg, isErr = false) {
  const t = h("div", { class: "toast" + (isErr ? " err" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), isErr ? 6000 : 3000);
}

function ask({ title, message = "", value = "", input = false, okText = "OK", danger = false }) {
  return new Promise((resolve) => {
    const dlg = $("#dlg"), inp = $("#dlg-input");
    $("#dlg-title").textContent = title;
    $("#dlg-msg").textContent = message;
    $("#dlg-msg").hidden = !message;
    inp.hidden = !input;
    inp.value = value;
    const ok = $("#dlg-ok");
    ok.textContent = okText;
    ok.className = "btn " + (danger ? "danger" : "primary");
    dlg.addEventListener("close", () => {
      resolve(dlg.returnValue === "ok" ? (input ? inp.value.trim() : true) : null);
    }, { once: true });
    dlg.returnValue = "";
    dlg.showModal();
    if (input) { inp.focus(); inp.setSelectionRange(0, value.includes(".") ? value.lastIndexOf(".") : value.length); }
  });
}

/* ------------------------------------------------------------------ API */
async function api(method, url, body) {
  const opt = { method, headers: { "X-PiVault": "1" }, credentials: "same-origin" };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  let data = null;
  try { data = await r.json(); } catch { /* no body */ }
  if (!r.ok) {
    if (r.status === 401 && state.user) sessionExpired();
    throw new Error((data && data.error) || `Request failed (${r.status})`);
  }
  return data;
}

function sessionExpired() {
  state.user = null;
  showAuth();
  toast("Your session expired. Please sign in again.", true);
}

/* ----------------------------------------------------------------- auth */
let mode = "login";
function setMode(m) {
  mode = m;
  $("#tab-login").setAttribute("aria-selected", m === "login");
  $("#tab-register").setAttribute("aria-selected", m === "register");
  $("#row-pass2").hidden = m !== "register";
  $("#row-invite").hidden = m !== "register" || !state.config.invite_required;
  $("#auth-submit").textContent = m === "login" ? "Sign in" : "Create account";
  $("#f-pass").autocomplete = m === "login" ? "current-password" : "new-password";
  $("#auth-error").textContent = "";
}
function showAuth() {
  $("#app").hidden = true;
  $("#viewer").hidden = true;
  $("#auth").hidden = false;
  $("#f-pass").value = $("#f-pass2").value = "";
  setMode("login");
  $("#f-user").focus();
}

$("#tab-login").onclick = () => setMode("login");
$("#tab-register").onclick = () => setMode("register");
$("#auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#auth-error");
  err.textContent = "";
  const username = $("#f-user").value.trim();
  const password = $("#f-pass").value;
  if (mode === "register" && password !== $("#f-pass2").value) {
    err.textContent = "The two passwords don't match.";
    return;
  }
  const btn = $("#auth-submit");
  btn.disabled = true;
  try {
    const payload = { username, password };
    if (mode === "register") payload.invite_code = $("#f-invite").value;
    await api("POST", mode === "login" ? "/api/login" : "/api/register", payload);
    await enterApp();
  } catch (ex) {
    err.textContent = ex.message;
  } finally {
    btn.disabled = false;
  }
});

$("#btn-logout").onclick = async () => {
  try { await api("POST", "/api/logout", {}); } catch { /* already out */ }
  state.user = null;
  showAuth();
};

async function enterApp() {
  const me = await api("GET", "/api/me");
  state.user = me.username;
  state.used = me.used;
  state.quota = me.quota;
  $("#whoami").textContent = me.username;
  $("#auth").hidden = true;
  $("#app").hidden = false;
  $("#f-user").value = "";
  renderUsage();
  const start = decodeURIComponent(location.hash.slice(1));
  await navigate(start, false);
}

/* ----------------------------------------------------------- navigation */
async function navigate(path, push = true) {
  state.path = path;
  if (push) history.pushState({}, "", path ? `#${enc(path)}` : location.pathname);
  try {
    await load();
  } catch (ex) {
    if (path) { toast("That folder no longer exists.", true); await navigate("", true); }
    else toast(ex.message, true);
  }
}
window.addEventListener("popstate", () => {
  if (state.user) navigate(decodeURIComponent(location.hash.slice(1)), false);
});

async function load() {
  const d = await api("GET", `/api/list?path=${enc(state.path)}`);
  state.entries = d.entries;
  render();
}
async function refreshUsage() {
  try {
    const me = await api("GET", "/api/me");
    state.used = me.used;
    state.quota = me.quota;
    renderUsage();
  } catch { /* not critical */ }
}
function renderUsage() {
  const pct = state.quota ? Math.min(100, (state.used / state.quota) * 100) : 0;
  $("#usage-fill").style.width = `${pct}%`;
  $("#usage-text").textContent = `${human(state.used)} of ${human(state.quota)}`;
}

/* --------------------------------------------------------------- render */
function render() {
  // breadcrumbs
  const crumbs = $("#crumbs");
  crumbs.replaceChildren(h("button", { onclick: () => navigate("") }, "All files"));
  let acc = "";
  for (const part of state.path.split("/").filter(Boolean)) {
    acc = join(acc, part);
    const target = acc;
    crumbs.append(h("span", {}, "/"), h("button", { onclick: () => navigate(target) }, part));
  }
  $("#title").textContent = folderName();
  document.title = `${folderName()} - PiVault`;

  $("#v-list").setAttribute("aria-pressed", state.view === "list");
  $("#v-grid").setAttribute("aria-pressed", state.view === "grid");

  const box = $("#files");
  box.className = `files ${state.view}`;
  box.replaceChildren(...state.entries.map(makeItem));
  box.hidden = state.entries.length === 0;
  $("#empty").hidden = state.entries.length !== 0;
}

function iconBtn(name, title, fn, cls = "") {
  return h("button", { class: `icon-btn ${cls}`, title, "aria-label": title, onclick: fn }, icon(name));
}

function makeItem(e) {
  const full = join(state.path, e.name);
  const isDir = e.type === "dir";
  const kind = isDir ? null : mediaKind(e.name);

  let thumbInner;
  if (isDir) {
    thumbInner = h("div", { class: "fshape" });
    thumbInner.style.setProperty("--h", hueOf(e.name));
  } else if (kind === "image" && state.view === "grid") {
    thumbInner = h("img", { src: fileURL(full), loading: "lazy", alt: "" });
  } else {
    thumbInner = h("div", { class: "ftile" }, extOf(e.name).slice(0, 4));
  }

  const sub = isDir ? "Folder" : `${human(e.size)}, ${fmtDate(e.mtime)}`;
  const acts = h("div", { class: "acts", onclick: (ev) => ev.stopPropagation() },
    iconBtn("download", isDir ? "Download as zip" : "Download", () => download(e)),
    iconBtn("edit", "Rename", () => rename(e)),
    iconBtn("trash", "Delete", () => remove(e), "danger"));

  return h("div", {
    class: "item", role: "button", tabindex: "0",
    onclick: () => openEntry(e),
    onkeydown: (ev) => { if (ev.key === "Enter") openEntry(e); },
  },
    h("div", { class: "thumb" }, thumbInner),
    h("div", { class: "meta" }, h("div", { class: "name", title: e.name }, e.name), h("div", { class: "sub" }, sub)),
    h("div", { class: "size" }, isDir ? "" : human(e.size)),
    h("div", { class: "mtime" }, fmtDate(e.mtime, true)),
    acts);
}

/* -------------------------------------------------------------- actions */
function openEntry(e) {
  if (e.type === "dir") return navigate(join(state.path, e.name));
  if (mediaKind(e.name)) return openViewer(e);
  download(e);
}

function download(e) {
  const full = join(state.path, e.name);
  const href = e.type === "dir" ? `/api/zip?path=${enc(full)}` : fileURL(full, true);
  const a = h("a", { href, download: e.type === "dir" ? `${e.name}.zip` : e.name });
  document.body.append(a);
  a.click();
  a.remove();
}

async function rename(e) {
  const name = await ask({ title: "Rename", input: true, value: e.name, okText: "Rename" });
  if (!name || name === e.name) return;
  if (/[\\/]/.test(name)) return toast("Names can't contain / or \\", true);
  try {
    await api("POST", "/api/rename", { path: join(state.path, e.name), to: join(state.path, name) });
    await load();
  } catch (ex) { toast(ex.message, true); }
}

async function remove(e) {
  const ok = await ask({
    title: `Delete "${e.name}"?`, danger: true, okText: "Delete",
    message: e.type === "dir" ? "This folder and everything inside it will be permanently deleted." : "This file will be permanently deleted.",
  });
  if (!ok) return;
  try {
    await api("POST", "/api/delete", { path: join(state.path, e.name) });
    await load();
    refreshUsage();
  } catch (ex) { toast(ex.message, true); }
}

$("#btn-mkdir").onclick = async () => {
  const name = await ask({ title: "New folder", input: true, okText: "Create" });
  if (!name) return;
  if (/[\\/]/.test(name)) return toast("Names can't contain / or \\", true);
  try {
    await api("POST", "/api/mkdir", { path: join(state.path, name) });
    await load();
  } catch (ex) { toast(ex.message, true); }
};
$("#v-list").onclick = () => { state.view = "list"; store.set("pv.view", "list"); render(); };
$("#v-grid").onclick = () => { state.view = "grid"; store.set("pv.view", "grid"); render(); };

/* -------------------------------------------------------------- uploads */
const queue = [];
const jobs = [];
let running = 0;
const MAX_PARALLEL = 3;
let hideTimer = null;

function enqueue(file, dest) {
  clearTimeout(hideTimer);
  const bar = h("i");
  const status = h("span", {}, "waiting");
  const el = h("div", { class: "up" },
    h("div", { class: "row" }, h("span", { title: dest }, dest), status),
    h("div", { class: "bar" }, bar));
  $("#up-list").append(el);
  $("#uploads").hidden = false;
  const job = { file, dest, el, bar, status, state: "queued" };
  jobs.push(job);
  if (state.used + file.size > state.quota) return fail(job, "not enough space");
  queue.push(job);
  updateUploadTitle();
  pump();
}
function pump() {
  while (running < MAX_PARALLEL && queue.length) startJob(queue.shift());
}
function startJob(job) {
  running++;
  job.state = "running";
  const xhr = new XMLHttpRequest();
  xhr.open("PUT", `/api/file?path=${enc(job.dest)}`);
  xhr.setRequestHeader("X-PiVault", "1");
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const f = e.loaded / e.total;
    job.bar.style.width = `${f * 100}%`;
    job.status.textContent = `${Math.round(f * 100)}%`;
  };
  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      job.state = "done";
      job.bar.style.width = "100%";
      job.el.classList.add("done");
      job.status.textContent = "done";
      state.used += job.file.size;
      settle();
    } else {
      let msg = `failed (${xhr.status})`;
      try { msg = JSON.parse(xhr.responseText).error; } catch { /* keep default */ }
      if (xhr.status === 401) sessionExpired();
      fail(job, msg, true);
    }
  };
  xhr.onerror = () => fail(job, "network error", true);
  xhr.send(job.file);
}
function fail(job, msg, wasRunning = false) {
  job.state = "failed";
  job.el.classList.add("bad");
  job.bar.style.width = "100%";
  job.status.textContent = msg;
  if (wasRunning) settle(); else updateUploadTitle();
}
function settle() {
  running--;
  updateUploadTitle();
  pump();
  if (running === 0 && queue.length === 0) allDone();
}
function updateUploadTitle() {
  const done = jobs.filter((j) => j.state === "done").length;
  const bad = jobs.filter((j) => j.state === "failed").length;
  const total = jobs.length;
  $("#up-title").textContent = done + bad < total
    ? `Uploading ${done + bad + 1} of ${total}`
    : bad ? `${done} uploaded, ${bad} failed` : `Uploaded ${done} ${done === 1 ? "file" : "files"}`;
}
async function allDone() {
  if (state.user) { await load(); refreshUsage(); }
  if (!jobs.some((j) => j.state === "failed")) hideTimer = setTimeout(clearUploads, 5000);
}
function clearUploads() {
  jobs.length = 0;
  $("#up-list").replaceChildren();
  $("#uploads").hidden = true;
}
$("#up-close").onclick = clearUploads;

$("#btn-upload").onclick = () => $("#pick-files").click();
$("#btn-upfolder").onclick = () => $("#pick-folder").click();
$("#pick-files").onchange = (e) => {
  for (const f of e.target.files) enqueue(f, join(state.path, f.name));
  e.target.value = "";
};
$("#pick-folder").onchange = (e) => {
  for (const f of e.target.files) enqueue(f, join(state.path, f.webkitRelativePath || f.name));
  e.target.value = "";
};

/* drag & drop (files and whole folders) */
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
const veil = $("#dropveil");
addEventListener("dragenter", (e) => {
  if (!state.user || !hasFiles(e)) return;
  e.preventDefault();
  dragDepth++;
  $("#veil-text").textContent = `Drop to upload to "${folderName()}"`;
  veil.hidden = false;
});
addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
addEventListener("dragleave", (e) => {
  if (!hasFiles(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) veil.hidden = true;
});
addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  veil.hidden = true;
  if (!state.user) return;
  // webkitGetAsEntry must be called synchronously, before any await
  const entries = [...e.dataTransfer.items].filter((i) => i.kind === "file")
    .map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) {
    for (const f of e.dataTransfer.files) enqueue(f, join(state.path, f.name));
    return;
  }
  (async () => { for (const en of entries) await walkEntry(en, state.path); })()
    .catch((ex) => toast(`Could not read dropped items: ${ex.message || ex}`, true));
});
async function walkEntry(entry, base) {
  if (entry.isFile) {
    const file = await new Promise((res, rej) => entry.file(res, rej));
    enqueue(file, join(base, entry.name));
  } else if (entry.isDirectory) {
    const dir = join(base, entry.name);
    const reader = entry.createReader();
    let batch;
    do {
      batch = await new Promise((res, rej) => reader.readEntries(res, rej));
      for (const child of batch) await walkEntry(child, dir);
    } while (batch.length);
  }
}

/* --------------------------------------------------------------- viewer */
let viewList = [], viewIdx = -1;
function openViewer(entry) {
  viewList = state.entries.filter((x) => x.type === "file" && mediaKind(x.name));
  viewIdx = viewList.findIndex((x) => x.name === entry.name);
  $("#viewer").hidden = false;
  showViewer();
}
function clearStage() {
  const old = $("#v-stage > img, #v-stage > video, #v-stage > audio");
  if (!old) return;
  if (old.pause) { old.pause(); old.removeAttribute("src"); old.load(); }
  old.remove();
}
function showViewer() {
  clearStage();
  const e = viewList[viewIdx];
  const src = fileURL(join(state.path, e.name));
  const kind = mediaKind(e.name);
  const media = kind === "image" ? h("img", { src, alt: e.name })
    : kind === "video" ? h("video", { src, controls: true, autoplay: true, playsinline: true })
    : h("audio", { src, controls: true, autoplay: true });
  $("#v-stage").prepend(media);
  $("#v-name").textContent = e.name;
  $("#v-prev").hidden = $("#v-next").hidden = viewList.length < 2;
}
function stepViewer(d) {
  viewIdx = (viewIdx + d + viewList.length) % viewList.length;
  showViewer();
}
function closeViewer() { clearStage(); $("#viewer").hidden = true; }
$("#v-close").onclick = closeViewer;
$("#v-prev").onclick = () => stepViewer(-1);
$("#v-next").onclick = () => stepViewer(1);
$("#v-dl").onclick = () => download(viewList[viewIdx]);
addEventListener("keydown", (e) => {
  if ($("#viewer").hidden) return;
  if (e.key === "Escape") closeViewer();
  else if (e.key === "ArrowLeft" && viewList.length > 1) stepViewer(-1);
  else if (e.key === "ArrowRight" && viewList.length > 1) stepViewer(1);
});

/* ----------------------------------------------------------------- boot */
(async function boot() {
  document.querySelectorAll("[data-icon]").forEach((el) => el.prepend(icon(el.dataset.icon)));
  try { state.config = await api("GET", "/api/config"); } catch { /* ignore */ }
  if (!state.config.user) return showAuth();
  try { await enterApp(); } catch { showAuth(); }
})();
