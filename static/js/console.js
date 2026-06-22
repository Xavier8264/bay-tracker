/* ===========================================================================
   console.js -- the interactive logging view (central PC).

   Mirrors the dashboard grid, but every tile is clickable. The common path is
   optimized for the fewest clicks/keystrokes:
     * Initials are entered fresh in each action's pop-up -- the field is never
       pre-filled, so whoever logs the action always types their own initials
       (autocomplete from the admin roster still suggests as you type).
     * Product entry is type-to-filter, matching consecutive digits ANYWHERE in
       the number (so the last 3 digits find it), with an "Other" free-text path.
     * Barcode scanners just type into the focused field + Enter.
   It live-refreshes over SSE so two people editing don't clobber each other:
   every action re-reads current state server-side before logging.
   ========================================================================== */
(function () {
  let cfg = { reasons: [], products: [], initials: [], bays: [],
              layout: { grid_cols: 4, standard_rows: 3, extras_enabled: false } };

  async function loadConfig() {
    const r = await BT.get("/api/config");
    if (r.ok) cfg = r.data;
  }

  // ---- effective status (a bay on break still acts on its underlying run) ----
  function effStatus(t) { return t.status === "ON_BREAK" ? (t.paused_status || "IDLE") : t.status; }

  // ---- grid rendering (clickable) ----------------------------------------
  // PAUSED gets the ⏸ glyph; ON BREAK uses ❚❚ so the two frozen states never
  // read alike when both appear on the floor (a parked bay during a break).
  const ICON = { RUNNING: "▶", DELAYED: "⚠", IDLE: "○", ON_BREAK: "❚❚", DONE: "✔", PAUSED: "⏸" };
  const LABEL = { RUNNING: "RUNNING", DELAYED: "DELAYED", IDLE: "IDLE", ON_BREAK: "ON BREAK", DONE: "DONE", PAUSED: "PAUSED" };

  // Bottom-anchored time columns. Every occupied tile shows the unit's ELAPSED
  // (linear wall-clock) and TOTAL (time across every bay it has touched --
  // parallel bays summed); a delayed tile adds the live DELAYED clock after them
  // (elapsed · total · delayed). All tick while the plant clock is counting and
  // freeze on breaks/off-hours.
  function timeCols(t, delayed) {
    // A parked bay's unit clocks are frozen -- don't advance them locally.
    const accruing = effStatus(t) !== "PAUSED";
    const elapsed = BT.fmtElapsed(BT.liveSeconds(t.unit_elapsed_seconds, accruing));
    const total = BT.fmtElapsed(BT.liveSeconds(t.unit_total_seconds, accruing));
    let cols = `<div class="tcol"><span class="tlabel">elapsed</span><span class="tval">${elapsed}</span></div>`;
    cols += `<div class="tcol"><span class="tlabel">total</span><span class="tval">${total}</span></div>`;
    if (delayed) cols += `<div class="tcol"><span class="tlabel">delayed</span><span class="tval">${BT.fmtElapsed(BT.liveElapsed(t))}</span></div>`;
    return `<div class="times">${cols}</div>`;
  }

  function tileHTML(t) {
    const cls = "tile clickable " + t.status.toLowerCase();
    const state = `<span class="state-label"><span class="icon">${ICON[t.status]}</span>${LABEL[t.status]}</span>`;
    const head = `<div class="tile-head"><span class="bay-name">${BT.escapeHtml(t.name)}</span>${state}</div>`;

    if (t.status === "IDLE") {
      return `<div class="${cls}" data-bay="${t.bay_id}">${head}
        <div class="empty-msg">＋ tap to start</div></div>`;
    }
    const es = effStatus(t);
    // Product number is the primary read (most important on the floor); the work
    // order sits below it. No label above the number -- the product reads cleaner
    // on its own. Fall back to the work order as the headline if a (legacy)
    // record has no product number, so the big line is never blank.
    const hasProduct = !!t.product_number;
    const primary = BT.escapeHtml(hasProduct ? t.product_number : t.work_order);
    const subParts = [];
    if (hasProduct) subParts.push(t.work_order);
    if (t.component_label) subParts.push(t.component_label);
    const sub = subParts.map(BT.escapeHtml).join(" · ");
    const parallel = t.occupies_two ? `<span class="parallel-chip">∥ 2 bays</span>` : "";

    let body = `<div class="unit-num">${primary}</div>
      ${sub ? `<div class="sub-line">${sub}</div>` : ""}
      ${parallel}`;

    if (es === "DELAYED") {
      const d = t.delay || {};
      const cat = d.division ? `<span class="cat-chip">⚑ ${BT.escapeHtml(d.division)}</span>` : "";
      body += `<div class="delay-info"><div class="reason">${BT.escapeHtml(d.reason || "Delay")}</div>${cat}</div>`;
    } else if (es === "PAUSED") {
      body += `<div class="paused-note">Parked — unstaffed this shift · no alerts</div>`;
    }

    // RUNNING / DONE / ON_BREAK / PAUSED — neutral body; status reads from the
    // colored top band and the colored status word.
    return `<div class="${cls}" data-bay="${t.bay_id}">
      ${head}
      <div class="tile-body">${body}</div>
      ${timeCols(t, es === "DELAYED")}</div>`;
  }

  function render(snap) {
    const grid = document.getElementById("grid");
    const cols = cfg.layout.grid_cols || 4;
    grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
    const tiles = snap.tiles || [];
    const standard = tiles.filter(t => !t.is_extra);
    const extras = tiles.filter(t => t.is_extra);

    // Equal-height rows so the bays stretch to fill the screen (body.fillscreen
    // in app.css gives the grid the full remaining viewport height).
    const rows = (cfg.layout.extras_enabled ? 1 : 0) + Math.max(1, Math.ceil(standard.length / cols));
    grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;

    // demo-mode badge (the served DB is the demo one, not the live log)
    const dt = document.getElementById("demo-tag");
    if (dt) dt.classList.toggle("show", !!snap.demo_mode);
    let html = "";
    if (cfg.layout.extras_enabled) {
      for (let c = 1; c <= cols; c++) {
        const ex = extras.find(e => e.grid_col === c);
        html += ex ? tileHTML(ex) : `<div class="tile empty-cell"></div>`;
      }
    }
    standard.forEach(t => html += tileHTML(t));
    grid.innerHTML = html;

    // mode banner
    const banner = document.getElementById("mode-banner");
    banner.className = "";
    if (snap.on_break) { banner.classList.add("break");
      banner.textContent = "⏸ ON BREAK — timers paused (you can still log actions)"; }
    else if (snap.off_hours) { banner.classList.add("off");
      banner.textContent = "OFF-HOURS — timers paused (you can still log actions)"; }
  }

  // ---- modal helpers ------------------------------------------------------
  const backdrop = document.getElementById("backdrop");
  const modal = document.getElementById("modal");
  function openModal(html, wide) {
    modal.innerHTML = html;
    modal.classList.toggle("wide", !!wide);
    backdrop.classList.add("show");
  }
  function closeModal() { backdrop.classList.remove("show"); modal.innerHTML = ""; modal.classList.remove("wide"); }
  backdrop.addEventListener("click", e => { if (e.target === backdrop) closeModal(); });
  function initialsField(id = "f-initials") {
    // Never pre-filled: each action requires the operator to enter their own
    // initials. Autocomplete from the admin roster suggests as they type.
    const opts = (cfg.initials || []).map(i => `<option value="${BT.escapeHtml(i)}">`).join("");
    return `<label>Initials *</label><input id="${id}" value=""
            maxlength="8" autocomplete="off" list="initials-roster">
            <datalist id="initials-roster">${opts}</datalist>`;
  }
  const enteredInitials = () => {
    const el = document.getElementById("f-initials");
    return el ? el.value.trim() : "";
  };
  function errEl() { return `<div class="error-text" id="err"></div>`; }
  function setErr(msg) { const e = document.getElementById("err"); if (e) e.textContent = msg || ""; }

  async function doAction(payload, btn) {
    if (btn) btn.disabled = true;
    const r = await BT.post("/api/action", payload);
    if (r.ok) { closeModal(); }
    else { setErr((r.data && r.data.error) || "Something went wrong."); if (btn) btn.disabled = false; }
  }

  // ---- the per-bay action menu -------------------------------------------
  function openBay(bayId) {
    const snap = BT.getSnapshot(); if (!snap) return;
    const t = snap.tiles.find(x => x.bay_id === bayId); if (!t) return;
    const st = effStatus(t);
    if (st === "IDLE") return startModal(t);

    // Any OTHER occupied bay can be a merge target (combine the two bays into
    // one continuing unit). Same-work-order bays are the usual case (two halves
    // of one unit); a different work order is allowed and confirmed in the modal.
    // A parked (paused) bay is frozen, so it can't be a merge target.
    const mergeable = snap.tiles.filter(x => x.bay_id !== t.bay_id
      && x.work_order && effStatus(x) !== "IDLE" && effStatus(x) !== "PAUSED");

    const buttons = [];
    let allowMergeComplete = true;
    if (st === "PAUSED") {
      // Frozen/parked: resume to return it to monitoring (then act on it), or
      // close the whole unit out. No move/merge/delay while parked.
      buttons.push(`<button class="good" data-act="resume">▶ Resume bay</button>`);
      buttons.push(`<button data-act="unit_complete">★ Unit complete</button>`);
      allowMergeComplete = false;
    } else if (st === "DELAYED") {
      buttons.push(`<button class="good" data-act="clear">✓ Clear delay</button>`);
      buttons.push(`<button data-act="move">→ Move to bay…</button>`);
    } else if (st === "DONE") {
      // Work finished here; the part waits in the bay. It can move onward or be
      // parked if this shift won't touch it.
      buttons.push(`<button data-act="move">→ Move to bay…</button>`);
      buttons.push(`<button class="violet" data-act="pause">⏸ Pause bay (unstaff)</button>`);
    } else { // RUNNING
      buttons.push(`<button class="danger" data-act="delay">⚠ Flag delay</button>`);
      buttons.push(`<button data-act="move">→ Move to bay…</button>`);
      buttons.push(`<button class="warn" data-act="complete">✓ Work done at bay (stays here)</button>`);
      buttons.push(`<button class="violet" data-act="pause">⏸ Pause bay (unstaff)</button>`);
    }
    if (allowMergeComplete) {
      if (mergeable.length) buttons.push(`<button data-act="merge">⚯ Merge into bay…</button>`);
      buttons.push(`<button data-act="unit_complete">★ Unit complete</button>`);
    }

    const stLabel = st === "DONE" ? "done — awaiting next step"
      : st === "PAUSED" ? "paused — unstaffed this shift" : st;
    openModal(`<h2>${BT.escapeHtml(t.name)} — ${BT.escapeHtml(t.work_order)}</h2>
      <div class="sub">${BT.escapeHtml(t.product_number || "")} · ${stLabel}${t.component_label ? " · " + BT.escapeHtml(t.component_label) : ""}</div>
      <div class="action-menu">${buttons.join("")}</div>
      <div class="actions"><button id="cancel">Cancel</button></div>`);
    document.getElementById("cancel").onclick = closeModal;
    modal.querySelectorAll("[data-act]").forEach(b => b.onclick = () => routeAction(b.dataset.act, t, mergeable));
  }

  function routeAction(act, t, mergeable) {
    if (act === "clear") return confirmModal("Clear delay",
      `Clear the delay on <b>${BT.escapeHtml(t.name)}</b> (${BT.escapeHtml(t.work_order)})? The bay returns to running.`,
      (btn, ini) => doAction({ action: "clear_delay", bay_id: t.bay_id, initials: ini }, btn));
    if (act === "delay") return delayModal(t);
    if (act === "move") return moveModal(t);
    if (act === "complete") return confirmModal("Work done at bay",
      `Mark work on <b>${BT.escapeHtml(t.work_order)}</b> finished at ${BT.escapeHtml(t.name)}? ` +
      `The part stays in the bay (amber) until you move, merge, or complete the whole unit.`,
      (btn, ini) => doAction({ action: "complete_bay", bay_id: t.bay_id, initials: ini }, btn));
    if (act === "merge") return mergeModal(t, mergeable || []);
    if (act === "pause") return confirmModal("Pause bay",
      `Park <b>${BT.escapeHtml(t.name)}</b> (${BT.escapeHtml(t.work_order)})? Its clock freezes ` +
      `and it raises no alerts until someone resumes it — for a bay this shift won't work.`,
      (btn, ini) => doAction({ action: "pause_bay", bay_id: t.bay_id, initials: ini }, btn));
    if (act === "resume") return confirmModal("Resume bay",
      `Resume <b>${BT.escapeHtml(t.name)}</b> (${BT.escapeHtml(t.work_order)})? It returns to ` +
      `normal monitoring and its clock continues where it left off.`,
      (btn, ini) => doAction({ action: "resume_bay", bay_id: t.bay_id, initials: ini }, btn));
    if (act === "unit_complete") return confirmModal("Unit complete",
      `Mark work order <b>${BT.escapeHtml(t.work_order)}</b> as fully complete? This ends its journey.`,
      (btn, ini) => doAction({ action: "unit_complete", work_order: t.work_order, initials: ini }, btn));
  }

  // ---- Start ----
  function startModal(t, prefill) {
    prefill = prefill || {};
    openModal(`<h2>Start — ${BT.escapeHtml(t.name)}</h2>
      <label>Work Order * <span class="hint">(scan or type)</span></label>
      <input id="f-wo" autocomplete="off" value="${BT.escapeHtml(prefill.wo || "")}">
      <label>Product Number *</label>
      <input id="f-prod-filter" autocomplete="off" placeholder="Type any digits to filter…"
             value="${BT.escapeHtml(prefill.pn || "")}">
      <div class="filter-list" id="prod-list"></div>
      <div id="prod-other" style="display:none"><label>Other product number *</label>
        <input id="f-prod-other" autocomplete="off"></div>
      <label>Component label <span class="hint">(optional, e.g. "Half A")</span></label>
      <input id="f-comp" autocomplete="off">
      ${initialsField()}${errEl()}
      <div class="actions"><button id="cancel">Cancel</button>
        <button class="primary" id="go">Start</button></div>`);
    document.getElementById("cancel").onclick = closeModal;

    let selected = prefill.pn || null;
    let useOther = false;
    const listEl = document.getElementById("prod-list");
    const filterEl = document.getElementById("f-prod-filter");

    function renderList() {
      const q = filterEl.value.trim().toLowerCase();
      // Substring match anywhere in the number (consecutive digits anywhere).
      const matches = cfg.products.filter(p => p.number.toLowerCase().includes(q));
      let html = matches.slice(0, 30).map(p =>
        `<div class="opt ${selected === p.number ? "sel" : ""}" data-num="${BT.escapeHtml(p.number)}">
           ${BT.escapeHtml(p.number)}${p.description ? ' <span class="hint">· ' + BT.escapeHtml(p.description) + "</span>" : ""}</div>`).join("");
      html += `<div class="opt other" data-other="1">⊕ Other (type a new product number)</div>`;
      listEl.innerHTML = html;
      listEl.querySelectorAll("[data-num]").forEach(o => o.onclick = () => {
        selected = o.dataset.num; useOther = false;
        document.getElementById("prod-other").style.display = "none";
        filterEl.value = selected; renderList();
      });
      listEl.querySelector("[data-other]").onclick = () => {
        useOther = true; selected = null;
        document.getElementById("prod-other").style.display = "";
        document.getElementById("f-prod-other").focus();
      };
    }
    filterEl.addEventListener("input", () => { selected = null; renderList(); });
    renderList();
    document.getElementById("f-wo").focus();

    document.getElementById("go").onclick = (e) => {
      const pn = useOther ? document.getElementById("f-prod-other").value.trim() : (selected || "");
      doAction({ action: "start", bay_id: t.bay_id,
        work_order: document.getElementById("f-wo").value.trim(),
        product_number: pn,
        component_label: document.getElementById("f-comp").value.trim(),
        initials: document.getElementById("f-initials").value.trim() }, e.target);
    };
  }

  // ---- Flag delay ----
  function delayModal(t) {
    const opts = cfg.reasons.map(r =>
      `<option value="${r.id}" data-div="${BT.escapeHtml(r.division||"")}" data-ctrl="${BT.escapeHtml(r.in_out_of_control||"")}">
        ${BT.escapeHtml(r.label)}</option>`).join("");
    openModal(`<h2>Flag delay — ${BT.escapeHtml(t.name)}</h2>
      <div class="sub">${BT.escapeHtml(t.work_order)} · turns the bay red and alerts the floor</div>
      <label>Reason *</label><select id="f-reason">${opts}</select>
      <div class="hint" id="reason-meta"></div>
      <label>Note * <span class="hint">(required — what's going on?)</span></label>
      <textarea id="f-note" rows="3" autocomplete="off"></textarea>
      ${initialsField()}${errEl()}
      <div class="actions"><button id="cancel">Cancel</button>
        <button class="danger" id="go">⚠ Flag delay</button></div>`);
    document.getElementById("cancel").onclick = closeModal;
    const sel = document.getElementById("f-reason");
    function meta() {
      const o = sel.options[sel.selectedIndex]; if (!o) return;
      const div = o.dataset.div, ctrl = o.dataset.ctrl;
      document.getElementById("reason-meta").textContent =
        (div ? "Division: " + div : "No division") + (ctrl ? " · " + ctrl + " of control" : "");
    }
    sel.onchange = meta; meta();
    document.getElementById("f-note").focus();
    document.getElementById("go").onclick = (e) => doAction({ action: "flag_delay",
      bay_id: t.bay_id, reason_id: parseInt(sel.value, 10),
      note: document.getElementById("f-note").value.trim(),
      initials: document.getElementById("f-initials").value.trim() }, e.target);
  }

  // ---- Move ----
  function moveModal(t) {
    const snap = BT.getSnapshot();
    const idle = snap.tiles.filter(x => effStatus(x) === "IDLE");
    if (!idle.length) return openModal(`<h2>Move</h2><p>No empty bays available.</p>
      <div class="actions"><button id="cancel" class="primary">OK</button></div>`),
      document.getElementById("cancel").onclick = closeModal;
    const opts = idle.map(b => `<option value="${b.bay_id}">${BT.escapeHtml(b.name)}</option>`).join("");
    openModal(`<h2>Move ${BT.escapeHtml(t.work_order)}</h2>
      <div class="sub">Closes ${BT.escapeHtml(t.name)} and opens the target — atomic handoff.</div>
      <label>Target bay *</label><select id="f-target">${opts}</select>
      ${initialsField()}${errEl()}
      <div class="actions"><button id="cancel">Cancel</button>
        <button class="primary" id="go">Move</button></div>`);
    document.getElementById("cancel").onclick = closeModal;
    document.getElementById("go").onclick = (e) => doAction({ action: "move", bay_id: t.bay_id,
      target_bay_id: parseInt(document.getElementById("f-target").value, 10),
      initials: document.getElementById("f-initials").value.trim() }, e.target);
  }

  // ---- Merge (combine two occupied bays into one continuing bay) ----
  function mergeModal(t, mergeable) {
    if (!mergeable.length) return;
    // "Merge into bay X" => the unit continues in X; this bay (t) frees up.
    const opts = mergeable.map(b =>
      `<option value="${b.bay_id}" data-wo="${BT.escapeHtml(b.work_order)}">${BT.escapeHtml(b.name)} — ${BT.escapeHtml(b.work_order)}${b.component_label ? " · " + BT.escapeHtml(b.component_label) : ""}</option>`).join("");
    openModal(`<h2>Merge — ${BT.escapeHtml(t.name)}</h2>
      <div class="sub">Combine into one continuing unit. The merged unit stays in the target bay;
        <b>${BT.escapeHtml(t.name)}</b> (${BT.escapeHtml(t.work_order)}) frees up.</div>
      <label>Continue in (merge into) *</label><select id="f-target">${opts}</select>
      <div class="hint" id="merge-note"></div>
      ${initialsField()}${errEl()}
      <div class="actions"><button id="cancel">Cancel</button>
        <button class="primary" id="go">⚯ Merge</button></div>`);
    document.getElementById("cancel").onclick = closeModal;
    const sel = document.getElementById("f-target");
    function note() {
      const o = sel.options[sel.selectedIndex]; if (!o) return;
      document.getElementById("merge-note").textContent = (o.dataset.wo === t.work_order)
        ? "Same work order — joining the two halves of this unit."
        : `Different work orders — the merged unit continues as ${o.dataset.wo}; ${t.work_order} ends here (merged).`;
    }
    sel.onchange = note; note();
    document.getElementById("go").onclick = (e) => doAction({ action: "mate",
      keep_bay_id: parseInt(sel.value, 10),
      release_bay_id: t.bay_id,
      initials: enteredInitials() }, e.target);
  }

  // ---- confirm (actions that need no other input). Initials are entered
  // here, in the pop-up, like every other action. onYes(btn, initials).
  function confirmModal(title, html, onYes, danger) {
    openModal(`<h2>${title}</h2><div class="sub">${html}</div>${initialsField()}${errEl()}
      <div class="actions"><button id="cancel">Cancel</button>
        <button class="${danger ? "danger" : "primary"}" id="yes">Confirm</button></div>`);
    document.getElementById("cancel").onclick = closeModal;
    const ini = document.getElementById("f-initials");
    if (!ini.value) ini.focus();
    document.getElementById("yes").onclick = (e) => onYes(e.target, enteredInitials());
  }

  // ---- Shift changeover: one screen to staff/park the whole floor ---------
  // Pre-filled from the current floor: every occupied bay is staffed unless it
  // is already parked. Uncheck a bay to pause it (clock freezes, no alerts);
  // re-check a parked bay to resume it. Confirm logs only the bays that changed.
  function shiftChangeoverModal() {
    const snap = BT.getSnapshot(); if (!snap) return;
    const cols = cfg.layout.grid_cols || 4;
    const allTiles = snap.tiles || [];
    const standard = allTiles.filter(t => !t.is_extra);
    // Extra top-row bays are staffed here only when they're enabled on the board,
    // so the pop-up mirrors exactly what the floor shows.
    const extras = cfg.layout.extras_enabled ? allTiles.filter(t => t.is_extra) : [];
    const showExtrasRow = extras.length > 0;
    const tiles = extras.concat(standard);   // full staffable set (order-agnostic)
    const occupied = t => effStatus(t) !== "IDLE";

    const staffed = {};   // bay_id -> desired staffed?  (occupied bays only)
    const seed = () => tiles.forEach(t => {
      if (occupied(t)) staffed[t.bay_id] = effStatus(t) !== "PAUSED";
    });
    seed();

    const badge = snap.shift ? `<span class="sc-badge">${BT.escapeHtml(snap.shift)}</span>` : "";

    function cellHTML(t) {
      const n = BT.escapeHtml(t.name.replace(/^Bay\s*/i, ""));
      if (!occupied(t)) {
        return `<div class="sc-cell idle"><span class="sc-num">${n}</span>
          <span class="sc-empty">empty</span></div>`;
      }
      const on = !!staffed[t.bay_id];
      const num = BT.escapeHtml(t.product_number || t.work_order || t.name);
      return `<div class="sc-cell ${on ? "staffed" : "paused"}" data-bay="${t.bay_id}">
        <span class="sc-box">${on ? "✓" : ""}</span>
        <span class="sc-num">${num}</span><span class="sc-cap">${n}</span></div>`;
    }

    function render() {
      const grid = document.getElementById("sc-grid");
      grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
      let html = "";
      if (showExtrasRow) {
        // Mirror the board: a top row with each extra bay in its chosen column.
        for (let c = 1; c <= cols; c++) {
          const ex = extras.find(e => e.grid_col === c);
          html += ex ? cellHTML(ex) : `<div class="sc-cell blank"></div>`;
        }
      }
      html += standard.map(cellHTML).join("");
      grid.innerHTML = html;
      grid.querySelectorAll(".sc-cell[data-bay]").forEach(c => c.onclick = () => {
        const id = parseInt(c.dataset.bay, 10);
        staffed[id] = !staffed[id];
        render();
      });
      let s = 0, p = 0;
      Object.keys(staffed).forEach(k => (staffed[k] ? s++ : p++));
      document.getElementById("sc-counts").innerHTML =
        `<b>${s}</b> staffed · <b class="sc-pa">${p}</b> paused`;
    }

    openModal(`<div class="sc-head"><h2>Staffed bays this shift</h2>${badge}</div>
      <div class="sub">Pre-filled from the current floor — uncheck a bay to <b>pause</b> it
        (clock freezes, no alerts). It stays parked until someone resumes it.</div>
      <div class="sc-legend">
        <span><span class="sc-key staffed">✓</span> Staffed — normal alerts</span>
        <span><span class="sc-key paused">⏸</span> Paused — no delay alerts</span>
      </div>
      <div id="sc-grid" class="sc-grid"></div>
      <div class="sc-floor">▲ front of shop — grid mirrors the floor</div>
      <div class="sc-foot"><span id="sc-counts"></span>
        <span class="sc-reset" id="sc-reset">↺ Reset to current</span></div>
      ${initialsField()}${errEl()}
      <div class="actions"><button id="cancel">Cancel</button>
        <button class="primary" id="go">Confirm &amp; start shift</button></div>`, true);
    document.getElementById("cancel").onclick = closeModal;
    document.getElementById("sc-reset").onclick = () => { seed(); render(); };
    render();

    document.getElementById("go").onclick = (e) => {
      const pause = [], resume = [];
      tiles.forEach(t => {
        if (!occupied(t)) return;
        const wasPaused = effStatus(t) === "PAUSED";
        const want = !!staffed[t.bay_id];
        if (want && wasPaused) resume.push(t.bay_id);
        if (!want && !wasPaused) pause.push(t.bay_id);
      });
      if (!pause.length && !resume.length) return closeModal();  // nothing to log
      doAction({ action: "shift_changeover", pause, resume, initials: enteredInitials() }, e.target);
    };
  }

  // ---- wire up clicks -----------------------------------------------------
  document.getElementById("grid").addEventListener("click", e => {
    const tile = e.target.closest(".tile.clickable");
    if (tile) openBay(parseInt(tile.dataset.bay, 10));
  });
  document.getElementById("shift-btn").onclick = shiftChangeoverModal;

  // ---- boot ---------------------------------------------------------------
  loadConfig().then(() => {
    BT.connect({ onState: render });
    BT.startTicker(render);
    setInterval(loadConfig, 30000); // pick up admin changes to reasons/products
  });
})();
