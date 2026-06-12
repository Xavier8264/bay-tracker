/* ===========================================================================
   stats.js -- filters, charts, cost, and the corrections workflow.

   Charts are drawn with the locally-vendored Chart.js (no CDN, works offline).
   Every number comes from /api/stats, which derives from the same event log as
   the exports -- so the screen and a CSV/XLSX of the same range always agree.
   Empty groupings render "no data" (never interpolated -- Appendix C2).
   ========================================================================== */
(function () {
  let cfg = { bays: [], reasons: [], divisions: [], products: [], shifts: [] };
  const charts = {};
  const myInitials = () => localStorage.getItem("bt_initials") || "";

  // ---- filter dropdowns ---------------------------------------------------
  async function loadConfig() {
    const r = await BT.get("/api/config");
    if (!r.ok) return;
    cfg = r.data;
    fill("f-bay", cfg.bays.map(b => [b.id, b.name]));
    fill("f-reason", cfg.reasons.map(r => [r.label, r.label]));
    fill("f-division", (cfg.divisions || []).map(d => [d, d]));
    fill("f-product", cfg.products.map(p => [p.number, p.number]));
    fill("f-shift", (cfg.shifts || []).map(s => [s.name, s.name]));
  }
  function fill(id, pairs) {
    const el = document.getElementById(id);
    pairs.forEach(([v, t]) => {
      const o = document.createElement("option"); o.value = v; o.textContent = t; el.appendChild(o);
    });
  }

  // ---- date presets -------------------------------------------------------
  function ymd(d) { return d.toISOString().slice(0, 10); }
  function dtLocal(d) {
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:00`;
  }
  function presetRange(preset) {
    const now = new Date();
    const y = now.getFullYear();
    const startOfDay = new Date(y, now.getMonth(), now.getDate());
    if (preset === "today") return [ymd(startOfDay), ymd(now)];
    if (preset === "this_week") {
      const dow = (now.getDay() + 6) % 7; // Monday=0
      const mon = new Date(y, now.getMonth(), now.getDate() - dow);
      return [ymd(mon), ymd(now)];
    }
    if (preset === "mtd") return [ymd(new Date(y, now.getMonth(), 1)), ymd(now)];
    if (preset === "ytd") return [ymd(new Date(y, 0, 1)), ymd(now)];
    if (/^q[1-4]$/.test(preset)) {
      const q = parseInt(preset[1], 10) - 1;
      return [ymd(new Date(y, q*3, 1)), ymd(new Date(y, q*3+3, 0))];
    }
    if (preset === "last_quarter") {
      const cq = Math.floor(now.getMonth()/3);
      let lq = cq - 1, ly = y; if (lq < 0) { lq = 3; ly = y - 1; }
      return [ymd(new Date(ly, lq*3, 1)), ymd(new Date(ly, lq*3+3, 0))];
    }
    if (preset === "this_shift") {
      const shifts = cfg.shifts || [];
      if (!shifts.length) return [ymd(startOfDay), ymd(now)];
      // pick the latest cutoff <= now (wrap to last shift before the first cutoff)
      const mins = now.getHours()*60 + now.getMinutes();
      const toM = s => { const [h,m] = s.start.split(":"); return (+h)*60 + (+m); };
      const sorted = [...shifts].sort((a,b) => toM(a) - toM(b));
      let chosen = sorted[sorted.length-1], prevDay = mins < toM(sorted[0]);
      for (const s of sorted) if (toM(s) <= mins) { chosen = s; prevDay = false; }
      const [h, m] = chosen.start.split(":");
      const start = new Date(y, now.getMonth(), now.getDate() - (prevDay ? 1 : 0), +h, +m);
      return [dtLocal(start), dtLocal(now)];
    }
    return null; // custom
  }

  function buildFilters() {
    const preset = document.getElementById("preset").value;
    let start, end;
    if (preset === "custom") { start = document.getElementById("f-start").value;
                               end = document.getElementById("f-end").value; }
    else { const r = presetRange(preset); if (r) { [start, end] = r; } }
    const f = {};
    if (start) f.start = start;
    if (end) f.end = end;
    const map = { bay_id: "f-bay", reason: "f-reason", division: "f-division",
                  product_number: "f-product", shift: "f-shift" };
    for (const k in map) { const v = document.getElementById(map[k]).value; if (v) f[k] = v; }
    return f;
  }
  function toQuery(f) { return Object.keys(f).map(k => `${k}=${encodeURIComponent(f[k])}`).join("&"); }

  // ---- charts -------------------------------------------------------------
  function renderChart(canvasId, chartCfg, hasData) {
    const canvas = document.getElementById(canvasId);
    const wrap = canvas.parentElement;
    let nd = wrap.querySelector(".no-data");
    if (charts[canvasId]) { charts[canvasId].destroy(); delete charts[canvasId]; }
    if (!hasData) {
      canvas.style.display = "none";
      if (!nd) { nd = document.createElement("div"); nd.className = "no-data"; nd.textContent = "no data"; wrap.appendChild(nd); }
      return;
    }
    canvas.style.display = ""; if (nd) nd.remove();
    charts[canvasId] = new Chart(canvas, chartCfg);
  }
  const GRID = "#33414f", TICK = "#9fb0c0";
  function bar(labels, data, label, color) {
    return { type: "bar", data: { labels, datasets: [{ label, data, backgroundColor: color || "#4a86ff" }] },
      options: { plugins: { legend: { display: false } },
        scales: { x: { ticks: { color: TICK }, grid: { color: GRID } },
                  y: { beginAtZero: true, ticks: { color: TICK }, grid: { color: GRID } } } } };
  }
  function line(labels, data, label) {
    return { type: "line", data: { labels, datasets: [{ label, data, borderColor: "#25b866",
        backgroundColor: "rgba(37,184,102,.2)", tension: .25, fill: true }] },
      options: { plugins: { legend: { display: false } },
        scales: { x: { ticks: { color: TICK }, grid: { color: GRID } },
                  y: { beginAtZero: true, ticks: { color: TICK }, grid: { color: GRID } } } } };
  }

  // ---- main refresh -------------------------------------------------------
  async function refresh() {
    const f = buildFilters();
    const q = toQuery(f);
    document.getElementById("dl-xlsx").href = "/export.xlsx?" + q;
    document.getElementById("dl-csv").href = "/export.zip?" + q;
    document.getElementById("dl-xlsx-all").href = "/export.xlsx?everything=1";

    const r = await BT.get("/api/stats?" + q);
    if (!r.ok) { if (r.status === 403) location.reload(); return; }
    const s = r.data;
    document.getElementById("range-label").textContent =
      `Showing ${s.range.start} → ${s.range.end}`;

    // KPIs
    const thru = s.throughput.reduce((a, b) => a + b.units, 0);
    const kpis = [
      ["Current WIP", s.wip.current_wip],
      ["Open delays", s.counts.open_delays],
      ["Units completed", thru],
      ["Parallel 2-bay", s.parallel.pct == null ? "—" : s.parallel.pct + "%"],
      ["Avg queue (min)", s.queue.avg_queue_minutes == null ? "—" : s.queue.avg_queue_minutes],
      ["Delay cost", s.cost.total == null ? "—" : "$" + s.cost.total],
    ];
    document.getElementById("kpis").innerHTML = kpis.map(k =>
      `<div class="box"><div class="n">${k[1]}</div><div class="l">${k[0]}</div></div>`).join("");

    // Charts
    const pr = s.delay_pareto_reason;
    renderChart("c-pareto-reason", bar(pr.map(x => x.label), pr.map(x => x.minutes), "Delay minutes", "#d23b3b"), pr.length);
    const pd = s.delay_pareto_division;
    renderChart("c-pareto-div", bar(pd.map(x => x.label), pd.map(x => x.minutes), "Delay minutes", "#e6a417"), pd.length);
    const ut = s.bay_utilization.filter(x => x.utilization_pct != null);
    renderChart("c-util", bar(ut.map(x => x.bay), ut.map(x => x.utilization_pct), "Utilization %", "#1f9d57"), ut.length);
    const cy = s.avg_cycle_by_product;
    renderChart("c-cycle", bar(cy.map(x => x.product_number), cy.map(x => x.avg_cycle_minutes), "Avg cycle (min)"), cy.length);
    const th = s.throughput;
    renderChart("c-thru", line(th.map(x => x.day), th.map(x => x.units), "Units"), th.length);

    // Cost detail
    const cb = document.getElementById("cost-box");
    if (s.cost.rate == null) {
      cb.innerHTML = `<div class="no-data">No labor rate set — enter one in Admin to estimate cost.</div>`;
    } else {
      const rows = s.cost.by_reason.map(x => `<tr><td>${BT.escapeHtml(x.reason)}</td><td>$${x.cost}</td></tr>`).join("")
        || `<tr><td colspan="2" class="no-data">no delays in range</td></tr>`;
      cb.innerHTML = `<p class="hint">Rate: $${s.cost.rate}/hr · Total: <b>$${s.cost.total}</b></p>
        <table><thead><tr><th>Reason</th><th>Est. cost</th></tr></thead><tbody>${rows}</tbody></table>`;
    }
    loadOpenRecent();
  }

  // ---- open & recent / corrections ---------------------------------------
  async function loadOpenRecent() {
    const r = await BT.get("/api/open_recent");
    if (!r.ok) return;
    const d = r.data;
    document.getElementById("open-delays").innerHTML = d.open_delays.map(o =>
      `<tr class="${o.stale ? "stale" : ""}"><td>${BT.escapeHtml(o.bay)}</td><td>${BT.escapeHtml(o.work_order)}</td>
        <td>${BT.escapeHtml(o.reason || "")}</td><td>${o.started_at.slice(5,16)}</td>
        <td>${o.minutes} ${o.stale ? '<span class="badge stale">stale</span>' : ""}</td>
        <td>${BT.escapeHtml(o.flagged_by || "")}</td>
        <td><button class="btn" data-cd="${o.bay_id}">Close…</button></td></tr>`).join("")
      || `<tr><td colspan="7" class="no-data">none open</td></tr>`;
    document.getElementById("open-runs").innerHTML = d.open_runs.map(o =>
      `<tr class="${o.stale ? "stale" : ""}"><td>${BT.escapeHtml(o.bay)}</td><td>${BT.escapeHtml(o.work_order)}</td>
        <td>${BT.escapeHtml(o.product_number || "")}</td><td>${o.started_at.slice(5,16)}</td>
        <td>${o.minutes} ${o.stale ? '<span class="badge stale">stale</span>' : ""}</td>
        <td>${BT.escapeHtml(o.started_by || "")}</td>
        <td><button class="btn" data-cr="${o.bay_id}">Close…</button></td></tr>`).join("")
      || `<tr><td colspan="7" class="no-data">none open</td></tr>`;
    const recent = [
      ...d.recent_delays.map(x => ({ ...x, _t: "delay" })),
      ...d.recent_runs.map(x => ({ ...x, _t: "run" })),
    ].sort((a, b) => (b.ended_at || b.cleared_at || "").localeCompare(a.ended_at || a.cleared_at || "")).slice(0, 20);
    document.getElementById("recent").innerHTML = recent.map(x =>
      `<tr><td>${x._t}</td><td>${BT.escapeHtml(x.bay)}</td><td>${BT.escapeHtml(x.work_order)}</td>
        <td>${(x.started_at||"").slice(5,16)}</td><td>${((x.ended_at||x.cleared_at)||"").slice(5,16)}</td>
        <td><button class="btn" data-rt="${x.start_event_id}" data-st="${BT.escapeHtml(x.started_at)}">Retime start…</button></td></tr>`).join("")
      || `<tr><td colspan="6" class="no-data">nothing recent</td></tr>`;
  }

  // ---- correction modals --------------------------------------------------
  const backdrop = document.getElementById("backdrop"), modal = document.getElementById("modal");
  function open(html) { modal.innerHTML = html; backdrop.classList.add("show"); }
  function close() { backdrop.classList.remove("show"); }
  backdrop.addEventListener("click", e => { if (e.target === backdrop) close(); });
  function nowLocal() { const d = new Date(); const p = n => String(n).padStart(2,"0");
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; }
  function initF() { return `<label>Initials *</label><input id="ci" value="${BT.escapeHtml(myInitials())}" maxlength="8">`; }

  async function submit(payload, btn) {
    btn.disabled = true;
    const r = await BT.post("/api/correct", payload);
    if (r.ok) { close(); refresh(); }
    else { document.getElementById("cerr").textContent = (r.data && r.data.error) || "Failed."; btn.disabled = false; }
  }

  function endedAt() { return document.getElementById("cnow").checked ? "now" : document.getElementById("ce").value; }
  const endedFields = `<label><input type="checkbox" id="cnow" checked style="width:auto"> Ended just now (use exact current time)</label>
      <label>…or type when it actually ended</label><input type="datetime-local" id="ce" value="${nowLocal()}">`;

  function closeDelay(bayId) {
    open(`<h2>Close forgotten delay</h2><div class="sub">Set when the delay actually ended. Logged as a correction; the original stays intact.</div>
      ${endedFields}
      <label>Note</label><input id="cn" placeholder="why this correction">${initF()}
      <div class="error-text" id="cerr"></div>
      <div class="actions"><button id="x">Cancel</button><button class="primary" id="ok">Save correction</button></div>`);
    document.getElementById("x").onclick = close;
    document.getElementById("ok").onclick = e => submit({ kind: "close_delay", bay_id: bayId,
      ended_at: endedAt(), initials: document.getElementById("ci").value, note: document.getElementById("cn").value }, e.target);
  }
  function closeRun(bayId) {
    open(`<h2>Close forgotten run</h2><div class="sub">Set when the run actually ended.</div>
      ${endedFields}
      <label><input type="checkbox" id="cterm" style="width:auto"> This also completes the whole unit (UNIT_COMPLETE)</label>
      <label>Note</label><input id="cn">${initF()}
      <div class="error-text" id="cerr"></div>
      <div class="actions"><button id="x">Cancel</button><button class="primary" id="ok">Save correction</button></div>`);
    document.getElementById("x").onclick = close;
    document.getElementById("ok").onclick = e => submit({ kind: "close_run", bay_id: bayId,
      ended_at: endedAt(), terminal: document.getElementById("cterm").checked,
      initials: document.getElementById("ci").value, note: document.getElementById("cn").value }, e.target);
  }
  function retime(eventId, startedAt) {
    const v = (startedAt || "").replace(" ", "T").slice(0, 16) || nowLocal();
    open(`<h2>Adjust start time</h2><div class="sub">Supersede the logged start with the real time. Original event is kept.</div>
      <label>New start time *</label><input type="datetime-local" id="ce" value="${v}">
      <label>Note</label><input id="cn">${initF()}
      <div class="error-text" id="cerr"></div>
      <div class="actions"><button id="x">Cancel</button><button class="primary" id="ok">Save correction</button></div>`);
    document.getElementById("x").onclick = close;
    document.getElementById("ok").onclick = e => submit({ kind: "event_time", event_id: eventId,
      new_ts: document.getElementById("ce").value, initials: document.getElementById("ci").value, note: document.getElementById("cn").value }, e.target);
  }

  document.addEventListener("click", e => {
    const cd = e.target.closest("[data-cd]"); if (cd) return closeDelay(parseInt(cd.dataset.cd, 10));
    const cr = e.target.closest("[data-cr]"); if (cr) return closeRun(parseInt(cr.dataset.cr, 10));
    const rt = e.target.closest("[data-rt]"); if (rt) return retime(parseInt(rt.dataset.rt, 10), rt.dataset.st);
  });

  // ---- preset -> show/hide custom date inputs ----
  document.getElementById("preset").addEventListener("change", function () {
    const custom = this.value === "custom";
    document.getElementById("f-start").disabled = !custom;
    document.getElementById("f-end").disabled = !custom;
    if (!custom) { const r = presetRange(this.value); if (r) {
      document.getElementById("f-start").value = (r[0]||"").slice(0,10);
      document.getElementById("f-end").value = (r[1]||"").slice(0,10); } }
  });
  document.getElementById("apply").onclick = refresh;

  // ---- boot ---------------------------------------------------------------
  loadConfig().then(() => { document.getElementById("preset").dispatchEvent(new Event("change")); refresh(); });
})();
