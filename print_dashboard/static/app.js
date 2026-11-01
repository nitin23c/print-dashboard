const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const size = kb => kb >= 1024 ? (kb / 1024).toFixed(1) + " MB" : kb + " KB";
const when = t => {
  const d = new Date(t * 1000), now = new Date();
  const time = d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
  return d.toDateString() === now.toDateString() ? time : d.toLocaleDateString([], {day: "numeric", month: "short"}) + ", " + time;
};
const num = n => n == null ? "–" : n.toLocaleString();
const pretty = n => esc(n === "mobile" ? "iPhone" : n === "dashboard" ? "Dashboard" : n);
// Only touch the page when something changed, so a refresh can't swallow a tap.
const put = (id, html) => { if ($(id).dataset.html !== html) { $(id).dataset.html = html; $(id).innerHTML = html; } };
let chosen = [], copies = 1, limits = {upload_mb: 100, copies: 20};

function headline(s) {
  const printing = s.jobs.find(j => j.state === "printing");
  if (s.queue.online === false) return ["bad", "Print server problem", {
    login: "CUPS refused the login. Check CUPSADMIN and CUPSPASSWORD.",
    unreachable: "Can't reach CUPS or the queue. Check CUPS_URL and CUPS_QUEUE.",
  }[s.queue.error] || "The queue is not responding."];
  if (s.queue.login === false) return ["bad", "CUPS login failed", "Check CUPSADMIN and CUPSPASSWORD. Job names, Cancel and colour need it."];
  if (s.printer.online === false) return ["bad", "Printer not reachable", "Check it is on and connected to Wi-Fi. Jobs will wait in the queue."];
  if (s.problems.length) return ["bad", s.problems.join(", "), s.jobs.length ? "Printing resumes once this is fixed." : "Fix this before printing."];
  if (printing) return ["go", "Printing", s.jobs.length > 1 ? (s.jobs.length - 1) + " more waiting" : ""];
  if (s.jobs.length) return ["go", "Starting", "A job is being prepared."];
  if (s.queue.stopped) return ["warn", "Queue paused", "Printing has stopped. Check the printer."];
  return ["ok", s.printer.sleeping ? "Ready (sleeping)" : "Ready", "It wakes up when a job arrives."];
}

function jobCard(j) {
  const total = j.pages, done = Math.min(j.printed || 0, total || Infinity);
  const known = total && j.state === "printing";
  const label = j.state === "printing"
    ? (total ? `Page ${Math.min(done + 1, total)} of ${total}` : `${done} printed`)
    : (total ? `${total} pages, ${j.state}` : j.state);
  return `<div class="card job"><div class="row"><span class="name">${esc(j.name)}</span>
    <button data-cancel="${j.id}" data-name="${esc(j.name)}">Cancel</button></div>
    <div class="bar ${known ? "" : "unknown"}"><i style="${known ? `width:${Math.round(done / total * 100)}%` : ""}"></i></div>
    <div class="row muted"><span>${label}</span><span>${size(j.size_kb)}, sent ${when(j.created)}</span></div></div>`;
}

function render(s) {
  const [kind, title, sub] = headline(s);
  limits = s.limits;
  document.title = s.title;
  $("title").textContent = s.title;
  $("hero").className = "card hero " + kind;
  put("hero", `<span class="dot"></span><div><b>${esc(title)}</b><span class="muted">${esc(sub)}</span></div>`);
  put("notices", (s.printer.notices || []).map(n => `<div class="card notice">${esc(n)}</div>`).join(""));
  $("color").setAttribute("aria-checked", !!s.queue.color);
  $("color").disabled = s.jobs.length > 0;
  $("colorField").hidden = !s.queue.color;
  if (!s.queue.color) $("useColor").checked = false;
  $("colorNote").textContent = s.jobs.length ? "Can be changed once printing finishes."
    : s.queue.color ? "On: colour prints only if chosen for that print." : "Off: everything prints in black and white.";
  put("jobs", s.jobs.length ? s.jobs.map(jobCard).join("") : `<div class="card muted">Nothing printing.</div>`);
  const stats = [["Today", s.totals.today.pages], ["This month", s.totals.month.pages], ["Black & white, all time", s.printer.mono], ["Colour, all time", s.printer.color]]
    .filter(([, v]) => v != null);
  put("stats", stats.map(([k, v]) => `<div class="card stat"><b>${num(v)}</b><span class="muted">${k}</span></div>`).join(""));
  put("history", s.history.length ? s.history.map(j => `<div class="item"><div><span class="name">${esc(j.name)}</span>
    <div class="muted">${j.pages ? j.pages + (j.pages === 1 ? " page, " : " pages, ") : ""}${pretty(j.user)}, ${when(j.finished)}</div></div>
    <span class="tag ${esc(j.state)}">${esc(j.state)}</span></div>`).join("") : `<div class="item muted">No jobs recorded yet.</div>`);
  const counters = [["sheets printed in total", s.printer.sheets], ["jams", s.printer.jams], ["misfeeds", s.printer.misfeeds]]
    .filter(([, v]) => v != null).map(([k, v]) => `${num(v)} ${k}`).join(", ");
  const info = [s.queue.model, counters, s.note].filter(Boolean);
  $("infoBlock").hidden = !info.length;
  put("info", info.map(esc).join("<br>"));
  $("updated").textContent = s.updated ? "Updated " + new Date(s.updated * 1000).toLocaleTimeString() : "";
}

async function refresh() {
  try {
    const response = await fetch("/api/status");
    render(await response.json());
  } catch {
    $("hero").className = "card hero bad";
    put("hero", `<span class="dot"></span><div><b>Dashboard offline</b><span class="muted">Trying again…</span></div>`);
  }
}

function showChosen() {
  const any = chosen.length > 0;
  $("options").hidden = !any;
  $("pick").hidden = any;
  $("copies").textContent = copies;
  $("less").disabled = copies <= 1;
  $("more").disabled = copies >= limits.copies;
  $("files").innerHTML = chosen.map(f => `<div class="file"><span class="name">${esc(f.name)}</span><span class="muted">${size(Math.ceil(f.size / 1024))}</span></div>`).join("");
}

function result(kind, text) {
  $("result").className = "result " + kind;
  $("result").textContent = text;
}

function upload(file, color, progress) {
  return new Promise(resolve => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/print?copies=${copies}&color=${color ? 1 : 0}`);
    xhr.setRequestHeader("X-Print-Dashboard", "1");
    xhr.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
    xhr.upload.onprogress = e => progress(e.loaded / e.total);
    xhr.onload = () => {
      let answer = {};
      try { answer = JSON.parse(xhr.responseText); } catch {}
      resolve(xhr.status === 200 ? null : answer.error || "The server refused the file.");
    };
    xhr.onerror = () => resolve("The upload failed. Check your connection and try again.");
    xhr.send(file);
  });
}

$("file").addEventListener("change", () => {
  chosen = [...$("file").files];
  copies = 1;
  $("file").value = "";
  const big = chosen.find(f => f.size > limits.upload_mb * 1024 * 1024);
  if (big) {
    result("bad", `${big.name} is over ${limits.upload_mb} MB.`);
    chosen = [];
  } else {
    result("", "");
  }
  showChosen();
});

$("less").addEventListener("click", () => { copies--; showChosen(); });
$("more").addEventListener("click", () => { copies++; showChosen(); });
$("clear").addEventListener("click", () => { chosen = []; result("", ""); showChosen(); });

$("send").addEventListener("click", async () => {
  const files = chosen, color = $("useColor").checked;
  $("options").hidden = true;
  $("sending").hidden = false;
  result("", "");
  let sent = 0, failure = null;
  for (const [i, file] of files.entries()) {
    $("sendNote").textContent = `Sending ${file.name}. Keep this page open until it's done.`;
    failure = await upload(file, color, part => { $("sent").style.width = Math.round((i + part) / files.length * 100) + "%"; });
    if (failure) break;
    sent++;
  }
  $("sending").hidden = true;
  $("sent").style.width = "0";
  chosen = failure ? files.slice(sent) : [];
  showChosen();
  if (failure) result("bad", (sent ? `${sent} sent. ` : "") + `${chosen[0].name}: ${failure}`);
  else result("ok", (files.length > 1 ? `${files.length} files sent.` : "Sent.") + " You can close this page; it will keep printing.");
  refresh();
});

$("color").addEventListener("click", async () => {
  const on = $("color").getAttribute("aria-checked") !== "true";
  $("color").disabled = true;
  const response = await fetch("/api/color/" + (on ? "on" : "off"), {method: "POST", headers: {"X-Print-Dashboard": "1"}});
  if (!response.ok) alert("Couldn't change the print mode.");
  setTimeout(refresh, 2500);
});

document.addEventListener("click", async e => {
  const button = e.target.closest("[data-cancel]");
  if (!button || !confirm(`Cancel "${button.dataset.name}"?`)) return;
  button.disabled = true;
  const response = await fetch("/api/cancel/" + button.dataset.cancel, {method: "POST", headers: {"X-Print-Dashboard": "1"}});
  if (!response.ok) alert("Couldn't cancel that job.");
  refresh();
});

refresh();
setInterval(refresh, 3000);
