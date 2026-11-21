/** The private "Create Sweet Spot server" page, served at /cloud/ui. Its key arrives in the link's #k= part
 *  (never sent to the server in the address), is kept for this tab only, and goes with each request. */
export const CLOUD_UI = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Sweet Spot server</title>
<style>
  :root { --brand: #c2185b; --ink: #2b1d16; --muted: #8a7a70; --line: #eadfd5; --bg: #faf6f1; --ok: #2e7d32; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); margin: 0; padding: 24px 14px; }
  main { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 24px; margin: 0 0 4px; } h1 span { color: var(--brand); }
  .sub { color: var(--muted); margin: 0 0 20px; }
  section { background: #fff; border: 1px solid var(--line); border-radius: 14px; padding: 18px; margin: 0 0 14px; }
  h2 { font-size: 17px; margin: 0 0 10px; display: flex; gap: 8px; align-items: center; }
  .n { background: var(--ink); color: #fff; border-radius: 50%; width: 24px; height: 24px; display: inline-grid; place-items: center; font-size: 13px; }
  .done .n { background: var(--ok); }
  p, li { color: var(--muted); font-size: 14px; line-height: 1.45; }
  input[type=password], input[type=text] { width: 100%; padding: 11px; border: 1px solid var(--line); border-radius: 10px; font: inherit; margin: 6px 0 10px; }
  button { border: 0; border-radius: 10px; padding: 11px 16px; font: inherit; font-weight: 700; cursor: pointer; background: var(--ink); color: #fff; }
  button.big { background: var(--brand); width: 100%; padding: 15px; font-size: 17px; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .ok { color: var(--ok); font-weight: 600; } .bad { color: var(--brand); font-weight: 600; }
  .msg { min-height: 1.3em; font-size: 14px; margin-top: 8px; }
  code { background: #f6efe8; padding: 2px 6px; border-radius: 6px; font-size: 13px; word-break: break-all; }
  pre { background: #2b1d16; color: #f6efe8; padding: 10px; border-radius: 10px; font-size: 12px; white-space: pre-wrap; max-height: 260px; overflow: auto; }
  .bar { height: 8px; background: var(--line); border-radius: 8px; overflow: hidden; margin: 10px 0; }
  .bar i { display: block; height: 100%; background: var(--brand); width: 5%; transition: width 1s; }
  a.btn { display: block; text-align: center; background: var(--ok); color: #fff; text-decoration: none; padding: 15px; border-radius: 10px; font-weight: 700; font-size: 17px; margin: 10px 0; }
  #locked { display: none; }
</style>
</head>
<body>
<main>
  <h1>Sweet <span>Spot</span> server</h1>
  <p class="sub">Create the Sweet Spot ordering server on DigitalOcean, in three steps.</p>
  <section id="locked"><h2>This page needs its private link</h2><p>Open it with the link from the BCU server:
    <code>sudo claude-cloud-helper ui-link</code></p></section>

  <div id="app">
  <section id="s1"><h2><span class="n">1</span> DigitalOcean token</h2>
    <p>DigitalOcean → API → Generate New Token, with read + write access. It's saved on the BCU server where only the system can read it, and never shown again.</p>
    <div id="tokState"></div>
    <input id="tok" type="password" autocomplete="off" spellcheck="false" placeholder="dop_v1_…">
    <button id="tokSave">Save token</button><div class="msg" id="tokMsg"></div>
  </section>

  <section id="s2"><h2><span class="n">2</span> Sweet Spot kit</h2>
    <p>Choose <b>sweetspot-system.zip</b> (the file Claude sent you).</p>
    <div id="kitState"></div>
    <input id="kit" type="file" accept=".zip"> <button id="kitSave">Upload kit</button><div class="msg" id="kitMsg"></div>
  </section>

  <section id="s3"><h2><span class="n">3</span> Create the server</h2>
    <p>London, 2 GB, Ubuntu 24.04 — about <b>$12 a month</b> on your DigitalOcean bill. It installs Sweet Spot by itself (about 10 minutes).
      Only one Sweet Spot server can exist; this page can't delete or change servers.</p>
    <label><p>Own web address (optional) — leave empty to use the server's built-in one</p>
      <input id="dom" type="text" placeholder="orders.sweetspotcov.co.uk" autocomplete="off"></label>
    <button class="big" id="create" disabled>Create Sweet Spot server</button>
    <div class="msg" id="createMsg"></div>
  </section>

  <section id="s4" hidden><h2><span class="n">4</span> Progress</h2>
    <div id="prog"></div><div class="bar"><i id="barI"></i></div><div id="result"></div>
  </section>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let key = new URLSearchParams(location.hash.slice(1)).get("k") || sessionStorage.getItem("ssk") || "";
if (location.hash) { history.replaceState(null, "", location.pathname); }
if (key) sessionStorage.setItem("ssk", key);
const call = async (what, body = {}) => {
  const r = await fetch("/cloud/api/" + what, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key, ...body }) });
  const d = await r.json().catch(() => ({}));
  if (r.status === 403) { lock(); throw new Error("locked"); }
  if (!r.ok) throw new Error(d.error || "Something went wrong");
  return d;
};
function lock() { $("app").style.display = "none"; $("locked").style.display = "block"; sessionStorage.removeItem("ssk"); }
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let timer = null;

async function refresh() {
  let s;
  try { s = await call("status"); } catch (e) { if (e.message !== "locked") $("createMsg").textContent = e.message; return; }
  $("s1").classList.toggle("done", s.token_set);
  $("tokState").innerHTML = s.token_set ? '<p class="ok">✔ Token saved</p>' : "";
  $("s2").classList.toggle("done", !!s.kit);
  $("kitState").innerHTML = s.kit ? '<p class="ok">✔ Kit uploaded (' + Math.round(s.kit.bytes / 1024) + ' KB, ' + esc(s.kit.uploaded) + ' UTC)</p>' : "";
  const d = (s.droplets || [])[0], inst = s.install || {};
  const busy = inst.status === "installing" || inst.status === "creating";
  $("create").disabled = !(s.token_set && s.kit) || !!d || busy;
  if (d && !busy && inst.status !== "ready" && inst.status !== "failed") $("createMsg").innerHTML = '<span class="bad">A Sweet Spot server already exists: ' + esc(d.ip || d.name) + "</span>";
  if (!inst.status && !d) { $("s4").hidden = true; return; }
  $("s4").hidden = false;
  const started = inst.started_at ? " (started " + esc(inst.started_at) + ")" : "";
  if (busy) {
    const mins = inst.started_at ? (Date.now() - Date.parse(inst.started_at.replace(" UTC", "Z").replace(" ", "T"))) / 60000 : 0;
    $("barI").style.width = Math.min(95, 5 + mins * 9) + "%";
    $("prog").innerHTML = "<p>Installing — about 10 minutes" + started + ". You can close this page and come back.</p>" + (d && d.ip ? "<p>Server address: <code>" + esc(d.ip) + "</code></p>" : "");
    $("result").innerHTML = "";
    if (!timer) timer = setInterval(refresh, 15000);
  } else if (inst.status === "ready") {
    $("barI").style.width = "100%"; clearInterval(timer); timer = null;
    $("prog").innerHTML = '<p class="ok">✔ Sweet Spot server is ready (' + esc(inst.domain) + ")</p>";
    $("result").innerHTML = (inst.setup_url ? '<a class="btn" href="' + esc(inst.setup_url) + '" target="_blank" rel="noopener noreferrer">Open setup link — create your manager login</a><p>Works once. Then sign in at <code>https://' + esc(inst.domain) + '/panel</code> and enter your keys under Settings → Keys &amp; connections.</p>' : "") +
      (s.new_server_connector_url ? "<p><b>Let Claude manage it:</b> Claude settings → Connectors → Add custom connector, name <i>Sweet Spot Server</i>, address:</p><p><code>" + esc(s.new_server_connector_url) + "</code></p><p>Keep that address private: its long code is the password.</p>" : "");
  } else if (inst.status === "failed") {
    $("barI").style.width = "100%"; clearInterval(timer); timer = null;
    $("prog").innerHTML = '<p class="bad">Install failed' + started + ".</p><p>" + esc(inst.error || "") + "</p>";
    $("result").innerHTML = inst.log_tail ? "<pre>" + esc(inst.log_tail) + "</pre><p>Send this to Claude.</p>" : "";
  }
}
$("tokSave").onclick = async () => {
  const t = $("tok").value.trim();
  if (!t) return;
  $("tokMsg").textContent = "Checking…";
  try { const r = await call("token", { token: t }); $("tok").value = ""; $("tokMsg").innerHTML = '<span class="' + (r.works ? "ok" : "bad") + '">' + esc(r.message) + "</span>"; refresh(); }
  catch (e) { $("tokMsg").innerHTML = '<span class="bad">' + esc(e.message) + "</span>"; }
};
$("kitSave").onclick = async () => {
  const f = $("kit").files[0];
  if (!f) return;
  $("kitMsg").textContent = "Uploading…";
  const r = await fetch("/cloud/api/kit", { method: "POST", headers: { "Content-Type": "application/zip", "x-ui-key": key }, body: f });
  const d = await r.json().catch(() => ({}));
  if (r.status === 403) return lock();
  $("kitMsg").innerHTML = r.ok ? '<span class="ok">✔ Uploaded</span>' : '<span class="bad">' + esc(d.error || "Upload failed") + "</span>";
  refresh();
};
$("create").onclick = async () => {
  if (!confirm("Create the Sweet Spot server now? It costs about $12 a month on your DigitalOcean account.")) return;
  $("create").disabled = true; $("createMsg").textContent = "Creating…";
  try { await call("create", { confirm: true, custom_domain: $("dom").value.trim().toLowerCase() }); $("createMsg").textContent = ""; refresh(); }
  catch (e) { $("createMsg").innerHTML = '<span class="bad">' + esc(e.message) + "</span>"; refresh(); }
};
if (!key) lock(); else refresh();
</script>
</body>
</html>`;
