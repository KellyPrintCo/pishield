"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => Number(n || 0).toLocaleString();

const TABS = [
  ["overview", "Overview"], ["sites", "Sites"], ["activity", "Activity"], ["filters", "Filters"],
  ["network", "Network"], ["devices", "Devices"], ["settings", "Settings"],
];
const MODES = {
  off: ["Off", "Ad blocking is paused. Everything passes through."],
  sites: ["Selected sites", "Ads and trackers are blocked only while a device is on one of the websites you picked in Sites."],
  everywhere: ["Everywhere", "Ads and trackers are blocked on every website for every device on the hotspot."],
};

let timer = null;
let current = "";

async function api(path, { method = "GET", body } = {}) {
  const r = await fetch("/api" + path, {
    method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-PiShield": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401 && path !== "/login") { showLogin(); throw new Error("Please sign in"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText || "Request failed");
  return data;
}

function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), err ? 6000 : 3000);
}

async function run(btn, fn, okMsg) {
  const label = btn ? btn.innerHTML : "";
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span>'; }
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, true); }
  finally { if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = label; } }
}

function ask(title, bodyHtml, okLabel = "OK") {
  const dlg = $("#dlg");
  $("#dlgTitle").textContent = title;
  $("#dlgBody").innerHTML = bodyHtml;
  $("#dlgOk").textContent = okLabel;
  dlg.returnValue = "";
  dlg.showModal();
  const first = $("input, select, textarea", dlg);
  if (first) first.focus();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      if (dlg.returnValue !== "ok") return resolve(null);
      const vals = {};
      $$("[name]", $("#dlgBody")).forEach((i) => (vals[i.name] = i.type === "checkbox" ? i.checked : i.value));
      resolve(vals);
    }, { once: true });
  });
}

const ago = (ts) => {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
};
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dur = (s) => {
  if (s == null) return "–";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
};

/* ---------------- shell ---------------- */
function showLogin() {
  stopTimer();
  $("#login").classList.remove("hidden");
  $("#tabs").classList.add("hidden");
  $("#logout").classList.add("hidden");
  $("#view").innerHTML = "";
  setTimeout(() => $("#pw").focus(), 50);
}

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#loginErr").textContent = "";
  try {
    await api("/login", { method: "POST", body: { password: $("#pw").value } });
    $("#pw").value = "";
    start();
  } catch (err) { $("#loginErr").textContent = err.message; }
});

$("#logout").addEventListener("click", async () => { await api("/logout", { method: "POST" }).catch(() => {}); showLogin(); });

function start() {
  $("#login").classList.add("hidden");
  $("#tabs").classList.remove("hidden");
  $("#logout").classList.remove("hidden");
  $("#tabs").innerHTML = TABS.map(([id, label]) => `<a href="#${id}" data-tab="${id}">${label}</a>`).join("");
  route();
}

function stopTimer() { if (timer) { clearInterval(timer); timer = null; } }
function every(ms, fn) { stopTimer(); timer = setInterval(() => { if (!document.hidden) fn(); }, ms); }

function setPill(mode) {
  const p = $("#pill");
  p.textContent = mode === "off" ? "Blocking off" : mode === "sites" ? "Selected sites" : "Blocking everywhere";
  p.className = "pill " + (mode === "off" ? "off" : "on");
}

async function route() {
  const tab = (location.hash || "#overview").slice(1);
  const id = TABS.some(([t]) => t === tab) ? tab : "overview";
  current = id;
  stopTimer();
  $$("#tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === id));
  const view = $("#view");
  view.innerHTML = '<div class="empty"><span class="spin"></span></div>';
  try { await VIEWS[id](view); }
  catch (e) { if (current === id) view.innerHTML = `<div class="card empty">${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);

/* ---------------- overview ---------------- */
function chart(series) {
  const buckets = [];
  for (let i = 0; i < series.length; i += 3) {
    const g = series.slice(i, i + 3);
    buckets.push([g[0][0], g.reduce((a, s) => a + s[1], 0), g.reduce((a, s) => a + s[2], 0)]);
  }
  const max = Math.max(1, ...buckets.map((b) => b[1]));
  const w = 100 / buckets.length;
  const bars = buckets.map((b, i) => {
    const h = (b[1] / max) * 100, hb = (b[2] / max) * 100;
    return `<rect x="${i * w + w * 0.12}" y="${100 - h}" width="${w * 0.76}" height="${h}" fill="var(--panel-2)"><title>${clock(b[0])}: ${b[1]} queries, ${b[2]} blocked</title></rect>` +
      `<rect x="${i * w + w * 0.12}" y="${100 - hb}" width="${w * 0.76}" height="${hb}" fill="var(--accent)"/>`;
  }).join("");
  return `<svg class="chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Queries over the last 24 hours">${bars}</svg>
    <div class="chart-legend"><span><i style="background:var(--panel-2);border:1px solid var(--line)"></i>Queries</span><span><i style="background:var(--accent)"></i>Blocked</span><span style="margin-left:auto">24h ago → now</span></div>`;
}

function bars(rows) {
  if (!rows.length) return '<div class="empty">Nothing blocked yet</div>';
  const max = rows[0][1] || 1;
  return rows.map(([d, n]) => `<div class="bar-row"><span class="truncate">${esc(d)}</span><span class="n">${fmt(n)}</span><span class="bar"><i style="width:${(n / max) * 100}%"></i></span></div>`).join("");
}

const VIEWS = {};
VIEWS.overview = async (el) => {
  const draw = async () => {
    const s = await api("/overview");
    if (current !== "overview") return;
    setPill(s.mode);
    const pct = s.queries_24h ? ((s.blocked_24h / s.queries_24h) * 100).toFixed(1) : "0.0";
    const h = s.health || {};
    el.innerHTML = `
      <section class="card stack">
        <h3 style="margin:0">Ad blocking</h3>
        <div class="seg" id="modeSeg">${Object.entries(MODES).map(([m, [l]]) => `<button data-m="${m}" class="${s.mode === m ? "on" : ""}">${l}</button>`).join("")}</div>
        <div class="muted small">${esc(MODES[s.mode][1])}${s.mode === "sites" ? ` <a href="#sites" style="color:var(--accent)">${s.sites_enabled} site${s.sites_enabled === 1 ? "" : "s"} selected →</a>` : ""}</div>
        ${s.mode === "sites" && !s.sites_enabled ? '<div class="banner warn">No websites selected yet, so nothing is being blocked. Add some in <a href="#sites">Sites</a>.</div>' : ""}
        ${s.blocklist_domains === 0 ? `<div class="banner warn">${s.lists_updating ? "Downloading ad lists…" : "No ad lists are loaded yet. Connect the Pi to the internet in Network; lists download automatically."}</div>` : ""}
      </section>
      <div class="stats">
        <div class="stat"><div class="v">${fmt(s.queries_24h)}</div><div class="l">Lookups (24h)</div></div>
        <div class="stat accent"><div class="v">${fmt(s.blocked_24h)}</div><div class="l">Ads blocked (24h)</div></div>
        <div class="stat"><div class="v">${pct}%</div><div class="l">Blocked</div></div>
        <div class="stat"><div class="v">${s.devices_online}</div><div class="l">Devices online</div></div>
      </div>
      ${s.active.length ? `<section class="card"><h3>On a selected site right now</h3><div class="list">${s.active.map((a) => `<div class="item"><span class="tag live">live</span><div class="main truncate">${esc(a.device)}</div><div class="muted small">${a.sites.map(esc).join(", ")}</div></div>`).join("")}</div></section>` : ""}
      <section class="card"><h3>Last 24 hours</h3>${chart(s.series)}</section>
      <div class="grid2">
        <section class="card"><h3>Most blocked</h3>${bars(s.top_blocked)}</section>
        <section class="card"><h3>Pi status</h3>
          <div class="list">
            <div class="item"><div class="main">Ad domains loaded</div><div>${fmt(s.blocklist_domains)}</div></div>
            <div class="item"><div class="main">Upstream DNS</div><div class="mono">${esc(s.upstreams.join(", "))}</div></div>
            <div class="item"><div class="main">Temperature</div><div>${h.temp_c == null ? "–" : h.temp_c + " °C"}</div></div>
            <div class="item"><div class="main">Memory used</div><div>${h.mem_used_pct == null ? "–" : h.mem_used_pct + "%"}</div></div>
            <div class="item"><div class="main">Pi uptime</div><div>${dur(h.uptime_s)}</div></div>
            <div class="item"><div class="main">PiShield uptime</div><div>${dur(s.uptime)}</div></div>
          </div>
        </section>
      </div>`;
    $$("#modeSeg button", el).forEach((b) => b.addEventListener("click", () =>
      run(null, async () => { await api("/mode", { method: "POST", body: { mode: b.dataset.m } }); await draw(); }, `Mode: ${MODES[b.dataset.m][0]}`)));
  };
  await draw();
  every(5000, () => draw().catch(() => {}));
};

/* ---------------- sites ---------------- */
VIEWS.sites = async (el) => {
  const s = await api("/sites");
  setPill(s.mode);
  const modeNote = s.mode === "sites" ? "" : `<div class="banner info">These sites are used in <b>Selected sites</b> mode. Right now the mode is <b>${MODES[s.mode][0]}</b>. <button class="small primary" id="toSites" style="margin-left:6px">Switch to Selected sites</button></div>`;
  el.innerHTML = `
    ${modeNote}
    <section class="card stack">
      <h3 style="margin:0">Block ads on these websites</h3>
      <form id="addSite" class="row nowrap">
        <input id="siteInput" class="grow" placeholder="e.g. cnn.com or a full link" autocomplete="off" autocapitalize="none" spellcheck="false">
        <button class="primary" type="submit">Add</button>
      </form>
      <div class="list" id="siteList">
        ${s.sites.length ? s.sites.map(siteRow).join("") : '<div class="empty">No websites yet. Add one above or tap a suggestion below.</div>'}
      </div>
    </section>
    <section class="card">
      <h3>Recently visited <span class="muted">· tap to add</span></h3>
      ${s.suggestions.length ? `<div class="chips">${s.suggestions.map((x) => `<button class="chip" data-add="${esc(x.domain)}">+ ${esc(x.domain)}<span class="n">${fmt(x.count)}</span></button>`).join("")}</div>`
        : '<div class="empty">Browse a few websites from a device on the hotspot and they\'ll show up here.</div>'}
    </section>
    <section class="card muted small">
      <b style="color:var(--text)">How this works.</b> When a device on the hotspot opens one of these websites, PiShield blocks ad and tracker
      domains for that device for ${s.window} seconds after its last request to that site (change this in Settings).
      Other devices, and other websites, are left alone. Each site can also have its own extra domains to block.
    </section>`;

  const add = (domain, btn) => run(btn, async () => { await api("/sites", { method: "POST", body: { domain } }); await VIEWS.sites(el); }, `Added ${domain}`);
  $("#addSite").addEventListener("submit", (e) => { e.preventDefault(); const v = $("#siteInput").value.trim(); if (v) add(v, $("#addSite button")); });
  $$("[data-add]", el).forEach((b) => b.addEventListener("click", () => add(b.dataset.add, b)));
  if ($("#toSites")) $("#toSites").addEventListener("click", (e) => run(e.target, async () => { await api("/mode", { method: "POST", body: { mode: "sites" } }); await VIEWS.sites(el); }, "Mode: Selected sites"));

  $$(".site", el).forEach((row) => {
    const d = row.dataset.domain;
    const path = "/sites/" + encodeURIComponent(d);
    $("[data-toggle]", row).addEventListener("change", (e) => run(null, () => api(path, { method: "PATCH", body: { enabled: e.target.checked } }), e.target.checked ? `Blocking ads on ${d}` : `Paused ${d}`));
    $("[data-edit]", row).addEventListener("click", () => $(".site-extra", row).classList.toggle("hidden"));
    $("[data-del]", row).addEventListener("click", async (e) => {
      if (!(await ask(`Remove ${d}?`, `<p class="muted" style="margin:0">Ads will no longer be blocked on ${esc(d)} in Selected sites mode.</p>`, "Remove"))) return;
      run(e.target, async () => { await api(path, { method: "DELETE" }); await VIEWS.sites(el); }, `Removed ${d}`);
    });
    $("[data-save]", row).addEventListener("click", (e) => run(e.target, async () => {
      const r = await api(path, { method: "PATCH", body: { extra: $("textarea", row).value } });
      $("textarea", row).value = r.extra.join("\n");
    }, "Saved"));
  });
};

function siteRow(s) {
  const bits = [`${fmt(s.blocked)} blocked`];
  if (s.extra.length) bits.push(`${s.extra.length} extra domain${s.extra.length === 1 ? "" : "s"}`);
  return `<div class="item site" data-domain="${esc(s.domain)}" style="flex-wrap:wrap">
    <label class="switch"><input type="checkbox" data-toggle ${s.enabled ? "checked" : ""}><span></span></label>
    <div class="main">
      <div class="title">${esc(s.domain)} ${s.active_on.length ? `<span class="tag live">live on ${s.active_on.map(esc).join(", ")}</span>` : ""}</div>
      <div class="sub">${bits.join(" · ")}</div>
    </div>
    <button class="icon ghost" data-edit title="Extra domains">⋯</button>
    <button class="icon ghost danger" data-del title="Remove">✕</button>
    <div class="site-extra hidden" style="flex-basis:100%">
      ${s.warning ? `<div class="banner warn small">${esc(s.warning)} shows most ads from its own servers, the same ones that deliver its videos and posts. A network blocker can't separate those, so expect trackers and outside ads to be blocked but not ${esc(s.warning)}'s own ads.</div>` : ""}
      <label class="field">Extra domains to block only while on ${esc(s.domain)} (one per line)
        <textarea placeholder="ads.example-network.com">${esc(s.extra.join("\n"))}</textarea></label>
      <div class="row end"><button class="small primary" data-save>Save</button></div>
    </div>
  </div>`;
}

/* ---------------- activity ---------------- */
VIEWS.activity = async (el) => {
  let status = "", q = "", live = true;
  el.innerHTML = `
    <section class="card stack">
      <div class="row">
        <div class="seg compact" id="fSeg"><button data-s="" class="on">All</button><button data-s="blocked">Blocked</button><button data-s="allowed">Allowed</button></div>
        <input id="q" class="grow" placeholder="Search domain or device" style="min-width:160px" autocapitalize="none">
        <label class="check small"><input type="checkbox" id="live" checked> Live</label>
      </div>
      <div class="muted small" id="totals"></div>
      <table class="log"><tbody id="rows"><tr><td class="empty"><span class="spin"></span></td></tr></tbody></table>
    </section>`;
  const draw = async () => {
    const p = new URLSearchParams({ status, q, limit: 250 });
    const r = await api("/activity?" + p);
    if (current !== "activity") return;
    $("#totals").textContent = `${fmt(r.total)} lookups since PiShield started · ${fmt(r.blocked)} blocked`;
    $("#rows").innerHTML = r.rows.length ? r.rows.map((x) => `
      <tr>
        <td class="t">${clock(x.ts)}</td>
        <td><div class="name">${esc(x.name)}</div><div class="muted small">${esc(x.type)} <span class="tag ${x.status}">${esc(x.status)}</span> ${esc(x.reason)}</div></td>
        <td class="d truncate muted small" title="${esc(x.client)}">${esc(x.device)}</td>
        <td class="a">${x.status === "blocked" ? `<button class="small" data-rule="allow" data-d="${esc(x.name)}">Allow</button>` : x.status === "allowed" ? `<button class="small ghost" data-rule="block" data-d="${esc(x.name)}">Block</button>` : ""}</td>
      </tr>`).join("") : '<tr><td class="empty">No lookups match.</td></tr>';
  };
  $$("#fSeg button").forEach((b) => b.addEventListener("click", () => {
    $$("#fSeg button").forEach((x) => x.classList.toggle("on", x === b)); status = b.dataset.s; draw();
  }));
  let deb;
  $("#q").addEventListener("input", (e) => { clearTimeout(deb); deb = setTimeout(() => { q = e.target.value; draw(); }, 250); });
  $("#live").addEventListener("change", (e) => { live = e.target.checked; });
  $("#rows").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-rule]");
    if (!b) return;
    const d = b.dataset.d, kind = b.dataset.rule;
    const ok = await ask(kind === "allow" ? "Always allow?" : "Always block?",
      `<p style="margin:0"><span class="mono">${esc(d)}</span> and its subdomains will be ${kind === "allow" ? "allowed on every site, even if an ad list includes it" : "blocked on every site and every device"}. You can undo this in Filters.</p>`,
      kind === "allow" ? "Allow" : "Block");
    if (ok) run(b, () => api("/rule/" + kind, { method: "POST", body: { domain: d } }), kind === "allow" ? `Allowed ${d}` : `Blocking ${d}`).then(draw);
  });
  await draw();
  every(4000, () => { if (live) draw().catch(() => {}); });
};

/* ---------------- filters ---------------- */
VIEWS.filters = async (el) => {
  const f = await api("/filters");
  el.innerHTML = `
    <section class="card">
      <div class="row" style="margin-bottom:8px"><h3 style="margin:0" class="grow">Ad lists <span class="muted">· ${fmt(f.total)} domains</span></h3>
        <button class="small" id="upd">${f.updating ? '<span class="spin"></span> Updating' : "Update now"}</button></div>
      <div class="muted small" style="margin-bottom:6px">Updated ${ago(f.lists_updated)} · refreshes every 24 hours</div>
      <div class="list">${f.lists.map((l) => `
        <div class="item" data-id="${esc(l.id)}">
          <label class="switch"><input type="checkbox" ${l.enabled ? "checked" : ""}><span></span></label>
          <div class="main"><div class="title">${esc(l.name)}</div>
            <div class="sub">${l.count ? fmt(l.count) + " domains" : "not downloaded"}${l.updated ? " · " + ago(l.updated) : ""}${l.error ? ` · <span style="color:var(--danger)">${esc(l.error)}</span>` : ""}</div>
            <div class="sub truncate mono">${esc(l.url)}</div></div>
          <button class="icon ghost danger" data-del title="Remove list">✕</button>
        </div>`).join("")}</div>
      <div class="row end" style="margin-top:10px"><button class="small" id="addList">+ Add a list</button></div>
    </section>
    <div class="grid2">
      <section class="card stack">
        <h3 style="margin:0">Always allow</h3>
        <div class="muted small">Never blocked, in any mode. Use this when a site breaks.</div>
        <textarea id="allow" placeholder="example.com">${esc(f.allowlist.join("\n"))}</textarea>
        <div class="row end"><button class="primary small" data-save="allowlist">Save</button></div>
      </section>
      <section class="card stack">
        <h3 style="margin:0">Always block</h3>
        <div class="muted small">Blocked on every site for every device, whatever the mode (except Off).</div>
        <textarea id="block" placeholder="annoying-tracker.com">${esc(f.blocklist.join("\n"))}</textarea>
        <div class="row end"><button class="primary small" data-save="blocklist">Save</button></div>
      </section>
    </div>`;
  $("#upd").addEventListener("click", (e) => run(e.target, async () => {
    await api("/lists/update", { method: "POST" });
    for (let i = 0; i < 90; i++) { await new Promise((r) => setTimeout(r, 2000)); const x = await api("/filters"); if (!x.updating) break; }
    if (current === "filters") await VIEWS.filters(el);
  }, "Lists updated"));
  $$(".item[data-id]", el).forEach((row) => {
    const id = row.dataset.id;
    $("input", row).addEventListener("change", (e) => run(null, async () => {
      await api("/lists/" + encodeURIComponent(id), { method: "PATCH", body: { enabled: e.target.checked } });
      setTimeout(() => current === "filters" && VIEWS.filters(el), 1500);
    }, e.target.checked ? "List enabled" : "List disabled"));
    $("[data-del]", row).addEventListener("click", async (e) => {
      if (!(await ask("Remove this list?", `<p class="muted" style="margin:0">${esc($(".title", row).textContent)}</p>`, "Remove"))) return;
      run(e.target, async () => { await api("/lists/" + encodeURIComponent(id), { method: "DELETE" }); await VIEWS.filters(el); }, "List removed");
    });
  });
  $("#addList").addEventListener("click", async (e) => {
    const v = await ask("Add an ad list", `
      <label class="field">Name<input name="name" placeholder="My list"></label>
      <label class="field">URL (hosts file, domain list or AdGuard/uBlock DNS-style list)<input name="url" placeholder="https://…" required></label>`, "Add");
    if (v) run(e.target, async () => { await api("/lists", { method: "POST", body: v }); setTimeout(() => current === "filters" && VIEWS.filters(el), 4000); }, "Downloading list…");
  });
  $$("[data-save]", el).forEach((b) => b.addEventListener("click", () => run(b, async () => {
    const kind = b.dataset.save, ta = kind === "allowlist" ? $("#allow") : $("#block");
    const r = await api("/rules/" + kind, { method: "PUT", body: { domains: ta.value } });
    ta.value = r[kind].join("\n");
  }, "Saved")));
};

/* ---------------- network ---------------- */
const signal = (n) => `<span class="signal" title="${n}%">${[20, 45, 70, 90].map((t, i) => `<i class="${n >= t ? "on" : ""}" style="height:${4 + i * 3}px"></i>`).join("")}</span>`;

VIEWS.network = async (el) => {
  const n = await api("/network");
  const st = n.status || {}, up = st.upstream, hs = n.hotspot || {};
  const eth = (st.ethernet || []).filter((e) => e.state === "connected");
  el.innerHTML = `
    ${st.error ? `<div class="banner warn">${esc(st.error)}</div>` : ""}
    <section class="card stack">
      <h3 style="margin:0">Internet connection</h3>
      ${up ? `<div class="item" style="padding:0">
          <div class="main"><div class="title">${up.state === "connected" ? esc(up.ssid || up.connection) : "Not connected"}</div>
          <div class="sub">USB Wi-Fi <span class="mono">${esc(up.iface)}</span>${up.ip ? " · " + esc(up.ip) : ""}${up.state !== "connected" ? " · " + esc(up.state) : ""}</div></div>
          ${up.signal != null ? signal(up.signal) : ""}</div>`
        : '<div class="banner warn">No USB Wi-Fi dongle detected. Plug it in, or use an Ethernet cable for internet.</div>'}
      ${eth.map((e) => `<div class="muted small">Ethernet <span class="mono">${esc(e.iface)}</span> connected · ${esc(e.ip)}</div>`).join("")}
      ${st.internet_via ? `<div class="muted small">Internet traffic goes out through <span class="mono">${esc(st.internet_via)}</span></div>` : '<div class="banner warn">No internet route. Connect to a network below.</div>'}
      <div class="row"><button class="primary" id="scan">Find networks</button><button id="hidden">Hidden network…</button></div>
      <div class="list" id="scanList"></div>
    </section>
    <section class="card">
      <h3>Saved networks <span class="muted">· joined automatically when in range</span></h3>
      <div class="list">${n.saved.length ? n.saved.map((s) => `
        <div class="item" data-uuid="${esc(s.uuid)}">
          <div class="main"><div class="title">${esc(s.name)}</div><div class="sub">${s.active ? '<span class="tag allowed">connected</span>' : "saved"}</div></div>
          ${s.active ? "" : '<button class="small" data-act>Connect</button>'}
          <button class="small ghost danger" data-forget>Forget</button>
        </div>`).join("") : '<div class="empty">None yet</div>'}</div>
    </section>
    <section class="card stack">
      <h3 style="margin:0">PiShield hotspot</h3>
      <div class="muted small">Radio <span class="mono">${esc(st.hotspot ? st.hotspot.iface : "?")}</span> · ${st.hotspot && st.hotspot.active ? '<span class="tag allowed">broadcasting</span>' : '<span class="tag blocked">off</span>'} · dashboard at <span class="mono">http://${esc(hs.address)}</span></div>
      <form id="hsForm" class="stack">
        <label class="field">Network name<input name="ssid" value="${esc(hs.ssid)}" maxlength="32" required></label>
        <label class="field">Password<input name="password" type="password" value="${esc(hs.password)}" minlength="8" maxlength="63" required></label>
        <label class="check small"><input type="checkbox" id="showPw"> Show password</label>
        <div class="row">
          <label class="field grow">Band<select name="band"><option value="bg" ${hs.band === "bg" ? "selected" : ""}>2.4 GHz (longer range)</option><option value="a" ${hs.band === "a" ? "selected" : ""}>5 GHz (faster)</option></select></label>
          <label class="field" style="width:110px">Channel<select name="channel"></select></label>
        </div>
        <div class="row end"><button class="primary" type="submit">Save hotspot</button></div>
      </form>
    </section>`;

  const chans = { bg: [1, 6, 11, 2, 3, 4, 5, 7, 8, 9, 10], a: [36, 40, 44, 48, 149, 153, 157, 161, 165] };
  const form = $("#hsForm");
  const fillCh = (keep) => {
    const b = form.band.value;
    form.channel.innerHTML = chans[b].map((c) => `<option ${String(c) === String(keep) ? "selected" : ""}>${c}</option>`).join("");
  };
  fillCh(hs.channel);
  form.band.addEventListener("change", () => fillCh(form.band.value === "bg" ? 6 : 36));
  $("#showPw").addEventListener("change", (e) => (form.password.type = e.target.checked ? "text" : "password"));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const v = { ssid: form.ssid.value, password: form.password.value, band: form.band.value, channel: Number(form.channel.value) };
    if (!(await ask("Restart the hotspot?", `<p style="margin:0">Every device (including this one) will disconnect for a few seconds. Reconnect to <b>${esc(v.ssid)}</b> with the new password.</p>`, "Save & restart"))) return;
    run($("button[type=submit]", form), () => api("/hotspot", { method: "PUT", body: v }), "Hotspot restarting…");
  });

  const entFields = `
        <label class="field">Username<input name="identity" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
        <label class="field">Password<input name="ent_password" type="password" autocomplete="off"></label>
        <label class="field">Certificate domain (optional)<input name="domain" placeholder="e.g. radius.example.edu" autocapitalize="off" spellcheck="false"></label>
        <div class="muted small">Ask IT for this. With it, the Pi checks it's really your organisation's network before sending your password. Without it, a fake network with the same name could capture your password.</div>
        <details><summary class="small">Advanced</summary>
          <label class="field">Sign-in method<select name="method">
            <option value="peap/mschapv2">PEAP + MSCHAPv2 (most common)</option>
            <option value="ttls/pap">TTLS + PAP</option>
            <option value="ttls/mschapv2">TTLS + MSCHAPv2</option>
            <option value="peap/gtc">PEAP + GTC</option></select></label>
          <label class="field">Anonymous identity (optional)<input name="anonymous" placeholder="e.g. anonymous@example.edu" autocapitalize="off" spellcheck="false"></label>
        </details>`;
  const connect = async (ssid, open, hidden = false, enterprise = false) => {
    let body = { ssid, password: "", hidden };
    if (!open || hidden) {
      const p = ask(`Join ${hidden ? "a hidden network" : ssid}`, `
        ${hidden ? `<label class="field">Network name<input name="ssid" required></label>
          <label class="field">Sign-in<select name="kind"><option value="psk">Password</option><option value="eap">Username + password (work / school)</option><option value="open">None (open)</option></select></label>` : ""}
        <div data-kind="psk"><label class="field">Password<input name="password" type="password" minlength="8" autocomplete="off"></label></div>
        <div data-kind="eap">${entFields}</div>
        <div class="muted small">The hotspot stays up while the Pi connects.${enterprise || hidden ? " Some workplaces don't allow personal hotspots, so check with IT first." : ""}</div>`, "Connect");
      const dlg = $("#dlgBody");
      const show = (kind) => $$("[data-kind]", dlg).forEach((d) => {
        const on = d.dataset.kind === kind;
        d.hidden = !on;
        $$("input, select", d).forEach((i) => { i.disabled = !on; });
        $$('[name="password"], [name="identity"], [name="ent_password"]', d).forEach((i) => { i.required = on; });
      });
      const kindSel = $('[name="kind"]', dlg);
      show(kindSel ? kindSel.value : enterprise ? "eap" : "psk");
      if (kindSel) kindSel.addEventListener("change", () => show(kindSel.value));
      const v = await p;
      if (!v) return;
      if (hidden) body.ssid = ssid = v.ssid.trim();
      const kind = hidden ? v.kind : enterprise ? "eap" : "psk";
      if (kind === "psk") body.password = v.password;
      if (kind === "eap") {
        const [eap, phase2] = v.method.split("/");
        body = { ...body, enterprise: true, identity: v.identity, password: v.ent_password, domain: v.domain, anonymous: v.anonymous, eap, phase2 };
      }
    }
    await run(null, async () => {
      toast(`Connecting to ${ssid}…`);
      const r = await api("/network/connect", { method: "POST", body });
      toast(r.message);
      if (current === "network") await VIEWS.network(el);
    });
  };
  $("#hidden").addEventListener("click", () => connect("", false, true));
  $("#scan").addEventListener("click", (e) => run(e.target, async () => {
    const r = await api("/network/scan", { method: "POST" });
    $("#scanList").innerHTML = r.networks.length ? r.networks.map((w) => `
      <div class="item">
        ${signal(w.signal)}
        <div class="main"><div class="title">${esc(w.ssid)}</div><div class="sub">${w.open ? "Open" : w.enterprise ? "Work / school login · " + esc(w.security) : esc(w.security)} · ch ${esc(w.channel)}</div></div>
        ${w.in_use ? '<span class="tag allowed">connected</span>' : `<button class="small" data-ssid="${esc(w.ssid)}" data-open="${w.open ? 1 : ""}" data-ent="${w.enterprise ? 1 : ""}">Join</button>`}
      </div>`).join("") : '<div class="empty">No networks found</div>';
    $$("[data-ssid]", $("#scanList")).forEach((b) => b.addEventListener("click", () => connect(b.dataset.ssid, !!b.dataset.open, false, !!b.dataset.ent)));
  }));
  $$(".item[data-uuid]", el).forEach((row) => {
    const uuid = row.dataset.uuid;
    const act = $("[data-act]", row);
    if (act) act.addEventListener("click", (e) => run(e.target, async () => { await api("/network/activate", { method: "POST", body: { uuid } }); await VIEWS.network(el); }, "Connected"));
    $("[data-forget]", row).addEventListener("click", async (e) => {
      if (!(await ask("Forget this network?", `<p style="margin:0">${esc($(".title", row).textContent)}</p>`, "Forget"))) return;
      run(e.target, async () => { await api("/network/forget", { method: "POST", body: { uuid } }); await VIEWS.network(el); }, "Forgotten");
    });
  });
};

/* ---------------- devices ---------------- */
VIEWS.devices = async (el) => {
  const draw = async () => {
    const r = await api("/devices");
    if (current !== "devices") return;
    el.innerHTML = `<section class="card"><h3>Devices on the hotspot</h3><div class="list">${r.devices.length ? r.devices.map((d) => `
      <div class="item">
        <span class="tag ${d.online ? "allowed" : ""}">${d.online ? "online" : "away"}</span>
        <div class="main"><div class="title">${esc(d.hostname || d.ip)}</div>
          <div class="sub mono">${esc(d.ip)}${d.mac ? " · " + esc(d.mac) : ""}</div>
          ${d.on_sites.length ? `<div class="sub"><span class="tag live">on ${d.on_sites.map(esc).join(", ")}</span></div>` : ""}</div>
        <div style="text-align:right" class="small"><div>${fmt(d.blocked)} blocked</div><div class="muted">${fmt(d.queries)} lookups · ${ago(d.last_seen)}</div></div>
      </div>`).join("") : '<div class="empty">No devices yet. Join the PiShield Wi-Fi from a phone or laptop.</div>'}</div></section>`;
  };
  await draw();
  every(6000, () => draw().catch(() => {}));
};

/* ---------------- settings ---------------- */
VIEWS.settings = async (el) => {
  const s = await api("/settings");
  el.innerHTML = `
    <section class="card stack">
      <h3 style="margin:0">Blocking</h3>
      <label class="field">Selected-site window (seconds)
        <input type="number" id="win" min="15" max="3600" value="${s.site_window}"></label>
      <div class="muted small">How long a device keeps ad blocking after its last request to a selected website. Longer catches slow-loading ads; shorter avoids blocking on the next site you open.</div>
      <label class="check"><input type="checkbox" id="force" ${s.force_dns ? "checked" : ""}>
        <span>Make every device use PiShield<br><span class="muted small">Redirects hard-coded DNS (like 8.8.8.8), blocks DNS-over-TLS, and tells Firefox and iCloud Private Relay to stand down on this network. Strongly recommended.</span></span></label>
      <label class="field">Upstream DNS servers
        <input id="ups" value="${esc(s.upstream_dns)}" placeholder="auto"></label>
      <div class="muted small"><b>auto</b> uses whatever the joined network provides (needed for hotel/café sign-in pages). Or enter e.g. <span class="mono">1.1.1.1, 9.9.9.9</span>.</div>
      <label class="check"><input type="checkbox" id="fromUp" ${s.dashboard_from_upstream ? "checked" : ""}>
        <span>Allow the dashboard from the upstream network<br><span class="muted small">Off = only devices on the PiShield hotspot can open this page.</span></span></label>
      <div class="row end"><button class="primary" id="saveSet">Save</button></div>
    </section>
    <section class="card stack">
      <h3 style="margin:0">Dashboard password</h3>
      <form id="pwForm" class="stack">
        <input type="password" name="current" placeholder="Current password" autocomplete="current-password" required>
        <input type="password" name="new" placeholder="New password (6+ characters)" minlength="6" autocomplete="new-password" required>
        <div class="row end"><button class="primary" type="submit">Change password</button></div>
      </form>
    </section>
    <section class="card stack">
      <h3 style="margin:0">Raspberry Pi</h3>
      <div class="row">
        <button data-sys="restart">Restart PiShield</button>
        <button data-sys="reboot">Reboot Pi</button>
        <button data-sys="shutdown" class="danger">Shut down</button>
      </div>
      <div class="muted small">Shut down before unplugging the power to protect the SD card.</div>
    </section>`;
  $("#saveSet").addEventListener("click", (e) => run(e.target, () => api("/settings", { method: "PUT", body: {
    site_window: Number($("#win").value), force_dns: $("#force").checked,
    upstream_dns: $("#ups").value, dashboard_from_upstream: $("#fromUp").checked,
  } }), "Settings saved"));
  $("#pwForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target;
    run($("button", f), async () => { await api("/password", { method: "POST", body: { current: f.current.value, new: f.new.value } }); f.reset(); }, "Password changed");
  });
  $$("[data-sys]", el).forEach((b) => b.addEventListener("click", async () => {
    const a = b.dataset.sys;
    const text = { restart: "PiShield will restart (a few seconds without DNS).", reboot: "The Pi will reboot. The hotspot is back in about a minute.", shutdown: "The Pi will power off. You'll need to unplug and replug it to start again." }[a];
    if (await ask(b.textContent + "?", `<p style="margin:0">${text}</p>`, b.textContent)) run(b, () => api("/system/" + a, { method: "POST" }), "Done");
  }));
};

/* ---------------- boot ---------------- */
(async () => {
  try {
    const s = await api("/session");
    s.signed_in ? start() : showLogin();
  } catch { showLogin(); }
})();
