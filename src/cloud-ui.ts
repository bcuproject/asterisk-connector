/** The private "Shop servers" page, served at /cloud/ui. Its key arrives in the link's #k= part
 *  (never sent to the server in the address), is kept for this tab only, and goes with each request. */
export const CLOUD_UI = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Shop servers</title>
<style>
  :root { --brand: #c2185b; --ink: #2b1d16; --muted: #8a7a70; --line: #eadfd5; --bg: #faf6f1; --ok: #2e7d32; --warn: #b26a00; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); margin: 0; padding: 24px 14px; }
  main { max-width: 680px; margin: 0 auto; }
  h1 { font-size: 24px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 18px; }
  section { background: #fff; border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px; margin: 0 0 12px; }
  h2 { font-size: 17px; margin: 0 0 8px; display: flex; gap: 8px; align-items: center; }
  h3 { font-size: 16px; margin: 0; }
  .n { background: var(--ink); color: #fff; border-radius: 50%; width: 24px; height: 24px; display: inline-grid; place-items: center; font-size: 13px; }
  .done .n { background: var(--ok); }
  p, li, label { color: var(--muted); font-size: 14px; line-height: 1.45; }
  label { display: block; margin: 8px 0 0; }
  input[type=password], input[type=text] { width: 100%; padding: 10px; border: 1px solid var(--line); border-radius: 10px; font: inherit; margin: 4px 0 6px; color: var(--ink); }
  button { border: 0; border-radius: 10px; padding: 10px 16px; font: inherit; font-weight: 700; cursor: pointer; background: var(--ink); color: #fff; }
  button.big { background: var(--brand); width: 100%; padding: 14px; font-size: 16px; margin-top: 10px; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .ok { color: var(--ok); font-weight: 600; } .bad { color: var(--brand); font-weight: 600; } .wait { color: var(--warn); font-weight: 600; }
  .msg { min-height: 1.2em; font-size: 14px; margin-top: 6px; }
  code { background: #f6efe8; padding: 2px 6px; border-radius: 6px; font-size: 13px; word-break: break-all; }
  pre { background: #2b1d16; color: #f6efe8; padding: 10px; border-radius: 10px; font-size: 12px; white-space: pre-wrap; max-height: 240px; overflow: auto; }
  .bar { height: 7px; background: var(--line); border-radius: 8px; overflow: hidden; margin: 8px 0; }
  .bar i { display: block; height: 100%; background: var(--brand); transition: width 1s; }
  a.btn { display: block; text-align: center; background: var(--ok); color: #fff; text-decoration: none; padding: 13px; border-radius: 10px; font-weight: 700; margin: 8px 0; }
  .shop { border-left: 5px solid var(--line); } .shop.ready { border-left-color: var(--ok); } .shop.installing { border-left-color: var(--warn); } .shop.failed { border-left-color: var(--brand); }
  .row { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
  .chk { display: flex; gap: 8px; align-items: center; color: var(--ink); }
  details summary { cursor: pointer; font-weight: 700; }
  #locked { display: none; }
</style>
</head>
<body>
<main>
  <h1>Shop servers</h1>
  <p class="sub">Each shop gets its own ordering server: WhatsApp ordering, back panel, tablet printing (and phone ordering for Sweet Spot).</p>
  <section id="locked"><h2>This page needs its private link</h2><p>Open it with the link from the phone server: <code>sudo claude-cloud-helper ui-link</code></p></section>

  <div id="app">
  <section id="s1"><h2><span class="n">1</span> DigitalOcean token <span id="tokState"></span></h2>
    <div id="tokBox"><p>DigitalOcean → API → Generate New Token (read + write). Saved where only the system can read it; never shown again.</p>
    <input id="tok" type="password" autocomplete="off" spellcheck="false" placeholder="dop_v1_…">
    <button id="tokSave">Save token</button><div class="msg" id="tokMsg"></div></div>
  </section>

  <section id="s2"><h2><span class="n">2</span> Shop kit <span id="kitState"></span></h2>
    <p>Choose <b>sweetspot-system.zip</b> (the file Claude sent you). Upload it again whenever Claude sends a newer one.</p>
    <input id="kit" type="file" accept=".zip"> <button id="kitSave">Upload kit</button><div class="msg" id="kitMsg"></div>
  </section>

  <div id="shops"></div>

  <section id="add"><h2><span class="n">+</span> Add a shop</h2>
    <label>Shop name <input id="name" type="text" maxlength="40" placeholder="e.g. Sweet Spot, Burger Barn" autocomplete="off"></label>
    <label>Short name (used for the server's name) <input id="slug" type="text" maxlength="24" placeholder="burger-barn" autocomplete="off"></label>
    <label>Own web address (optional; leave empty to use the server's built-in one) <input id="dom" type="text" placeholder="orders.burgerbarn.co.uk" autocomplete="off"></label>
    <label>Menu file (optional, .json in the shop-menu format; without it Sweet Spot gets its own menu and other shops start empty)
      <input id="menu" type="file" accept=".json,application/json"></label>
    <label class="chk"><input id="voice" type="checkbox"> Phone ordering (tuned to Sweet Spot's menu)</label>
    <button class="big" id="create" disabled>Create server for this shop</button>
    <div class="msg" id="createMsg"></div>
    <p>Each server is about <b>$12 a month</b> on your DigitalOcean bill. This page can create servers but can't delete or change them.</p>
  </section>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let key = new URLSearchParams(location.hash.slice(1)).get("k") || sessionStorage.getItem("ssk") || "";
if (location.hash) history.replaceState(null, "", location.pathname);
if (key) sessionStorage.setItem("ssk", key);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const call = async (what, body = {}) => {
  const r = await fetch("/cloud/api/" + what, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key, ...body }) });
  const d = await r.json().catch(() => ({}));
  if (r.status === 403) { lock(); throw new Error("locked"); }
  if (!r.ok) throw new Error(d.error || "Something went wrong");
  return d;
};
function lock() { $("app").style.display = "none"; $("locked").style.display = "block"; sessionStorage.removeItem("ssk"); }
const slugOf = (n) => n.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24);
let ready = false, timer = null, slugEdited = false;

$("name").oninput = () => {
  if (!slugEdited) $("slug").value = slugOf($("name").value);
  $("voice").checked = $("name").value.trim().toLowerCase() === "sweet spot";
  $("create").disabled = !ready || !$("name").value.trim();
};
$("slug").oninput = () => { slugEdited = true; };

function shopCard(slug, sh, server) {
  const st = sh.status || (server ? "ready" : "unknown");
  let body = "";
  if (st === "installing" || st === "creating") {
    const mins = sh.started_at ? (Date.now() - Date.parse(sh.started_at.replace(" UTC", "Z").replace(" ", "T"))) / 60000 : 0;
    body = '<p class="wait">Installing — about 10 minutes (started ' + esc(sh.started_at) + "). You can close this page.</p>" +
      '<div class="bar"><i style="width:' + Math.min(95, 5 + mins * 9) + '%"></i></div>' + (server && server.ip ? "<p>Server address: <code>" + esc(server.ip) + "</code></p>" : "");
  } else if (st === "ready") {
    body = '<p class="ok">✔ Ready at ' + esc(sh.domain || (server && server.ip) || "") + "</p>" +
      (sh.setup_url ? '<a class="btn" href="' + esc(sh.setup_url) + '" target="_blank" rel="noopener noreferrer">Open setup link — create the manager login</a>' +
        "<p>Works once. Then: <code>https://" + esc(sh.domain) + "/panel</code> → Settings → Keys &amp; connections.</p>" : "") +
      (sh.connector_url ? "<details><summary>Let Claude manage this server</summary><p>Claude settings → Connectors → Add custom connector, name <i>" + esc(sh.shop_name) +
        " Server</i>, address:</p><p><code>" + esc(sh.connector_url) + "</code></p><p>Keep it private: its long code is the password.</p></details>" : "");
  } else if (st === "failed") {
    body = '<p class="bad">Install failed</p><p>' + esc(sh.error || "") + "</p>" + (sh.log_tail ? "<pre>" + esc(sh.log_tail) + "</pre><p>Send this to Claude.</p>" : "");
  }
  return '<section class="shop ' + esc(st) + '"><div class="row"><h3>' + esc(sh.shop_name || slug) + '</h3><span class="muted"><code>shop-' + esc(slug) + "</code>" +
    (sh.phone_ordering ? " · 📞" : "") + "</span></div>" + body + "</section>";
}

async function refresh() {
  let s;
  try { s = await call("status"); } catch (e) { if (e.message !== "locked") $("createMsg").textContent = e.message; return; }
  $("s1").classList.toggle("done", s.token_set);
  $("tokState").innerHTML = s.token_set ? '<span class="ok">✔ saved</span>' : "";
  $("s2").classList.toggle("done", !!s.kit);
  $("kitState").innerHTML = s.kit ? '<span class="ok">✔ ' + Math.round(s.kit.bytes / 1024) + " KB, " + esc(s.kit.uploaded) + " UTC</span>" : "";
  ready = s.token_set && !!s.kit;
  $("create").disabled = !ready || !$("name").value.trim();
  const servers = {}; (s.servers || []).forEach((x) => { if (x.shop) servers[x.shop] = x; });
  const slugs = [...new Set([...Object.keys(s.shops || {}), ...Object.keys(servers)])];
  $("shops").innerHTML = slugs.map((k) => shopCard(k, (s.shops || {})[k] || { shop_name: k }, servers[k])).join("");
  const busy = slugs.some((k) => ["installing", "creating"].includes(((s.shops || {})[k] || {}).status));
  if (busy && !timer) timer = setInterval(refresh, 15000);
  if (!busy && timer) { clearInterval(timer); timer = null; }
}
$("tokSave").onclick = async () => {
  const t = $("tok").value.trim(); if (!t) return;
  $("tokMsg").textContent = "Checking…";
  try { const r = await call("token", { token: t }); $("tok").value = ""; $("tokMsg").innerHTML = '<span class="' + (r.works ? "ok" : "bad") + '">' + esc(r.message) + "</span>"; refresh(); }
  catch (e) { $("tokMsg").innerHTML = '<span class="bad">' + esc(e.message) + "</span>"; }
};
$("kitSave").onclick = async () => {
  const f = $("kit").files[0]; if (!f) return;
  $("kitMsg").textContent = "Uploading…";
  const r = await fetch("/cloud/api/kit", { method: "POST", headers: { "Content-Type": "application/zip", "x-ui-key": key }, body: f });
  const d = await r.json().catch(() => ({}));
  if (r.status === 403) return lock();
  $("kitMsg").innerHTML = r.ok ? '<span class="ok">✔ Uploaded</span>' : '<span class="bad">' + esc(d.error || "Upload failed") + "</span>";
  refresh();
};
$("create").onclick = async () => {
  const name = $("name").value.trim(), slug = ($("slug").value.trim() || slugOf(name)).toLowerCase();
  if (!confirm("Create a server for " + name + "? It costs about $12 a month on your DigitalOcean account.")) return;
  $("create").disabled = true; $("createMsg").textContent = "Working…";
  try {
    const f = $("menu").files[0];
    if (f) {
      let menu; try { menu = JSON.parse(await f.text()); } catch { throw new Error("That menu file isn't valid JSON."); }
      const m = await call("menu", { slug, menu }); $("createMsg").textContent = "Menu saved (" + m.items + " items). Creating…";
    }
    const r = await call("create", { confirm: true, shop_name: name, slug, custom_domain: $("dom").value.trim().toLowerCase(), phone_ordering: $("voice").checked });
    $("createMsg").innerHTML = '<span class="ok">✔ ' + esc(r.shop) + " server created — installing now (menu: " + esc(r.menu) + ").</span>";
    ["name", "slug", "dom"].forEach((i) => { $(i).value = ""; }); $("menu").value = ""; $("voice").checked = false; slugEdited = false;
  } catch (e) { $("createMsg").innerHTML = '<span class="bad">' + esc(e.message) + "</span>"; }
  refresh();
};
if (!key) lock(); else refresh();
</script>
</body>
</html>`;
