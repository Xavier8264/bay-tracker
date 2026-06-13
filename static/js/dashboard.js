/* ===========================================================================
   dashboard.js -- the read-only TV view.

   * Renders the bay grid (4x3, or 4x4 when extra top-row bays are enabled).
   * Bays are ALWAYS in fixed positions -- never reordered by status.
   * Full-screen takeover when a delay is flagged; respects this screen's
     ?division filter and queues multiple delays so none are skipped.
   * Kiosk niceties: keep-awake (Wake Lock), auto-fullscreen on first tap.
   ========================================================================== */
(function () {
  const DIVISION = (window.BT_DIVISION || "").trim();
  let layout = { grid_cols: 4, standard_rows: 3, extras_enabled: false };

  // ---- layout (refreshed periodically so admin changes appear on TVs) ----
  async function loadLayout() {
    const r = await BT.get("/api/config");
    if (r.ok && r.data && r.data.layout) layout = r.data.layout;
  }

  // ---- grid rendering -----------------------------------------------------
  const ICON = { RUNNING: "▶", DELAYED: "⚠", IDLE: "○", ON_BREAK: "⏸" };
  const LABEL = { RUNNING: "RUNNING", DELAYED: "DELAYED", IDLE: "IDLE", ON_BREAK: "ON BREAK" };

  function tileHTML(t) {
    const cls = "tile " + t.status.toLowerCase();
    const state = `<span class="state-label"><span class="icon">${ICON[t.status]}</span>${LABEL[t.status]}</span>`;
    const twobay = t.occupies_two ? `<span class="twobay">2 BAYS</span>` : "";

    if (t.status === "IDLE") {
      return `<div class="${cls}" data-bay="${t.bay_id}">
        <div class="tile-row"><span class="bay-name">${BT.escapeHtml(t.name)}</span>${state}</div>
        <div></div></div>`;
    }

    const elapsed = BT.fmtElapsed(BT.liveElapsed(t));
    if (t.status === "DELAYED" || (t.status === "ON_BREAK" && t.paused_status === "DELAYED")) {
      const d = t.delay || {};
      const div = d.division ? `<span class="divtag">${BT.escapeHtml(d.division)}</span>` : "";
      return `<div class="${cls} clickable" data-bay="${t.bay_id}">${twobay}
        <div class="tile-row"><span class="bay-name">${BT.escapeHtml(t.name)}</span>${state}</div>
        <div class="wo">${BT.escapeHtml(t.work_order)}</div>
        <div class="reason">${BT.escapeHtml(d.reason || "")} ${div}</div>
        <div class="tile-row"><span class="meta">delayed</span><span class="elapsed">${elapsed}</span></div>
      </div>`;
    }

    // RUNNING (or ON_BREAK over a running bay)
    const comp = t.component_label ? `<span class="meta">· ${BT.escapeHtml(t.component_label)}</span>` : "";
    return `<div class="${cls} clickable" data-bay="${t.bay_id}">${twobay}
      <div class="tile-row"><span class="bay-name">${BT.escapeHtml(t.name)}</span>${state}</div>
      <div class="wo">${BT.escapeHtml(t.work_order)}</div>
      <div class="meta">${BT.escapeHtml(t.product_number || "")} ${comp}</div>
      <div class="tile-row"><span class="meta">${t.status === "ON_BREAK" ? "paused" : "active"}</span>
        <span class="elapsed">${elapsed}</span></div>
    </div>`;
  }

  function emptyCellHTML() { return `<div class="tile empty-cell"></div>`; }

  function render(snap) {
    const grid = document.getElementById("grid");
    const cols = layout.grid_cols || 4;
    grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;

    const tiles = snap.tiles || [];
    const standard = tiles.filter(t => !t.is_extra);
    const extras = tiles.filter(t => t.is_extra);

    // Explicit equal-height rows so the grid fills the whole screen (the
    // kiosk CSS stretches the grid to viewport height; 1fr rows then split
    // that height evenly). Rows are derived from what is actually rendered.
    const rows = (layout.extras_enabled ? 1 : 0) + Math.max(1, Math.ceil(standard.length / cols));
    grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;

    // demo-mode badge (the served DB is the demo one, not the live log)
    const dt = document.getElementById("demo-tag");
    if (dt) dt.classList.toggle("show", !!snap.demo_mode);
    let html = "";

    if (layout.extras_enabled) {
      // Top row: one cell per column; an extra bay sits in its chosen column.
      for (let c = 1; c <= cols; c++) {
        const ex = extras.find(e => e.grid_col === c);
        html += ex ? tileHTML(ex) : emptyCellHTML();
      }
    }
    // Standard bays fill the remaining rows in fixed order.
    standard.forEach(t => { html += tileHTML(t); });
    grid.innerHTML = html;

    // mode banner (ON BREAK / OFF-HOURS)
    const banner = document.getElementById("mode-banner");
    banner.className = "";
    if (snap.on_break) {
      banner.classList.add("break");
      const ends = (snap.on_break.ends_at || "").slice(11, 16);
      banner.textContent = "⏸ ON BREAK" + (ends ? " — resumes " + ends : "") + " · timers paused";
    } else if (snap.off_hours) {
      banner.classList.add("off");
      banner.textContent = "OFF-HOURS — outside operating schedule · timers paused";
    }
  }

  // ---- click-to-expand detail (read-only) ---------------------------------
  function showDetail(bayId) {
    const snap = BT.getSnapshot(); if (!snap) return;
    const t = snap.tiles.find(x => x.bay_id === bayId); if (!t || t.status === "IDLE") return;
    const d = t.delay || {};
    let rows = [
      ["Bay", t.name], ["Status", t.status.replace("_", " ")],
      ["Work order", t.work_order], ["Product", t.product_number],
      ["Component", t.component_label], ["Started by", t.started_by],
      ["Elapsed", BT.fmtElapsed(BT.liveElapsed(t))],
    ];
    if (t.delay) {
      rows = rows.concat([
        ["Delay reason", d.reason], ["Division", d.division],
        ["In/Out of control", d.in_out_of_control], ["Flagged by", d.flagged_by],
        ["Delay note", d.note],
      ]);
    }
    const body = rows.filter(r => r[1]).map(r =>
      `<tr><th style="width:42%">${r[0]}</th><td>${BT.escapeHtml(r[1])}</td></tr>`).join("");
    const m = document.getElementById("detail-modal");
    m.innerHTML = `<h2>${BT.escapeHtml(t.name)}</h2><table>${body}</table>
      <div class="actions"><button class="primary" id="dt-close">Close</button></div>`;
    document.getElementById("detail-backdrop").classList.add("show");
    document.getElementById("dt-close").onclick = closeDetail;
  }
  function closeDetail() { document.getElementById("detail-backdrop").classList.remove("show"); }

  document.getElementById("grid").addEventListener("click", (e) => {
    const tile = e.target.closest(".tile.clickable");
    if (tile) showDetail(parseInt(tile.dataset.bay, 10));
  });
  document.getElementById("detail-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "detail-backdrop") closeDetail();
  });

  // ---- full-screen delay takeover ----------------------------------------
  const takeoverQueue = [];
  let takeoverActive = false;

  function onDelay(d) {
    // A division-filtered screen only takes over for its own division's delays.
    if (DIVISION && (d.division || "") !== DIVISION) return;
    takeoverQueue.push(d);
    if (!takeoverActive) runTakeover();
  }

  function runTakeover() {
    const d = takeoverQueue.shift();
    if (!d) { takeoverActive = false; return; }
    takeoverActive = true;
    const seconds = Math.max(3, parseInt(d.seconds, 10) || 12);

    document.getElementById("to-bay").textContent = d.bay || "";
    document.getElementById("to-wo").textContent = "Work order " + (d.work_order || "—");
    document.getElementById("to-reason").textContent = d.reason || "Delay";
    const ctrl = d.in_out_of_control ? " · " + d.in_out_of_control + " of control" : "";
    document.getElementById("to-meta").textContent =
      (d.division ? d.division : "") + ctrl + (d.flagged_by ? " · flagged by " + d.flagged_by : "");
    document.getElementById("to-note").textContent = d.note || "";

    const overlay = document.getElementById("takeover");
    overlay.classList.add("show");

    let remaining = seconds;
    const countEl = document.getElementById("to-count");
    countEl.textContent = (takeoverQueue.length ? (takeoverQueue.length + " more · ") : "") + remaining + "s";
    const iv = setInterval(() => {
      remaining -= 1;
      countEl.textContent = (takeoverQueue.length ? (takeoverQueue.length + " more · ") : "") + Math.max(0, remaining) + "s";
      if (remaining <= 0) {
        clearInterval(iv);
        overlay.classList.remove("show");
        setTimeout(runTakeover, 350); // brief gap, then next queued delay
      }
    }, 1000);
  }

  // ---- kiosk helpers: keep-awake + fullscreen ----------------------------
  async function keepAwake() {
    try { if ("wakeLock" in navigator) { await navigator.wakeLock.request("screen"); } }
    catch (e) { /* not supported; rely on device sleep settings */ }
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) keepAwake(); });
  document.body.addEventListener("click", function goFull() {
    const el = document.documentElement;
    if (el.requestFullscreen && !document.fullscreenElement) el.requestFullscreen().catch(() => {});
  }, { once: true });

  // ---- clock + connection corner -----------------------------------------
  function tickClock() {
    const now = new Date();
    document.getElementById("clock").textContent =
      now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  setInterval(tickClock, 1000); tickClock();

  // ---- boot ---------------------------------------------------------------
  loadLayout().then(() => {
    BT.connect({ onState: render, onDelay: onDelay });
    BT.startTicker(render);          // advance elapsed locally each second
    setInterval(loadLayout, 60000);  // pick up layout/extra-bay changes
    keepAwake();
  });
})();
