/* ===========================================================================
   console.js -- the interactive logging view (central PC).

   Mirrors the dashboard grid, but every tile is clickable. The common path is
   optimized for the fewest clicks/keystrokes:
     * Initials are entered in each action's pop-up (pre-filled with the last
       initials used on this PC, with autocomplete from the admin roster).
     * Product entry is type-to-filter, matching consecutive digits ANYWHERE in
       the number (so the last 3 digits find it), with an "Other" free-text path.
     * Barcode scanners just type into the focused field + Enter.
   It live-refreshes over SSE so two people editing don't clobber each other:
   every action re-reads current state server-side before logging.
   ========================================================================== */
(function () {
  let cfg = { reasons: [], products: [], initials: [], bays: [],
              layout: { grid_cols: 4, standard_rows: 3, extras_enabled: false } };

  // Last-used initials, remembered per browser so the pop-up field is
  // pre-filled but always visible and editable for each individual action.
  const myInitials = () => (localStorage.getItem("bt_initials") || "").trim();
  function rememberInitials(v) {
    v = (v || "").trim();
    if (v) localStorage.setItem("bt_initials", v);
  }

  async function loadConfig() {
    const r = await BT.get("/api/config");
    if (r.ok) cfg = r.data;
  }

  // ---- effective status (a bay on break still acts on its underlying run) ----
  function effStatus(t) { return t.status === "ON_BREAK" ? (t.paused_status || "IDLE") : t.status; }

  // ---- grid rendering (clickable) ----------------------------------------
  const ICON = { RUNNING: "▶", DELAYED: "⚠", IDLE: "○", ON_BREAK: "⏸", DONE: "✔" };
  const LABEL = { RUNNING: "RUNNING", DELAYED: "DELAYED", IDLE: "IDLE", ON_BREAK: "ON BREAK", DONE: "DONE" };

  // Bottom-anchored time columns. Every occupied tile shows the unit's ELAPSED
  // (linear wall-clock) and TOTAL (time across every bay it has touched --
  // parallel bays summed); a delayed tile leads with the live DELAYED clock.
  // All tick while the plant clock is counting and freeze on breaks/off-hours.
  function timeCols(t, delayed) {
    const elapsed = BT.fmtElapsed(BT.liveSeconds(t.unit_elapsed_seconds, true));
    const total = BT.fmtElapsed(BT.liveSeconds(t.unit_total_seconds, true));
    let cols = "";
    if (delayed) cols += `<div class="tcol"><span class="tlabel">delayed</span><span class="tval">${BT.fmtElapsed(BT.liveElapsed(t))}</span></div>`;
    cols += `<div class="tcol"><span class="tlabel">elapsed</span><span class="tval">${elapsed}</span></div>`;
    cols += `<div class="tcol"><span class="tlabel">total</span><span class="tval">${total}</span></div>`;
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
    // order drops to the subline. If a (legacy) record has no product number,
    // fall back to the work order as the headline so the big line is never blank.
    const hasProduct = !!t.product_number;
    const primary = BT.escapeHtml(hasProduct ? t.product_number : t.work_order);
    const primaryLabel = hasProduct ? "product" : "work order";
    const subParts = [];
    if (hasProduct) subParts.push(t.work_order);
    if (t.component_label) subParts.push(t.component_label);
    const sub = subParts.map(BT.escapeHtml).join(" · ");
    const parallel = t.occupies_two ? `<span class="parallel-chip">∥ 2 bays</span>` : "";

    let body = `<div class="unit-label">${primaryLabel}</div>
      <div class="unit-num">${primary}</div>
      ${sub ? `<div class="sub-line">${sub}</div>` : ""}
      ${parallel}`;

    if (es === "DELAYED") {
      const d = t.delay || {};
      const cat = d.division ? `<span class="cat-chip">⚑ ${BT.escapeHtml(d.division)}</span>` : "";
      body += `<div class="delay-info"><div class="reason">${BT.escapeHtml(d.reason || "Delay")}</div>${cat}</div>`;
    }

    // RUNNING / DONE / ON_BREAK — neutral body; status reads from the colored
    // top band and the colored status word.
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
  function openModal(html) { modal.innerHTML = html; backdrop.classList.add("show"); }
  function closeModal() { backdrop.classList.remove("show"); modal.innerHTML = ""; }
  backdrop.addEventListener("click", e => { if (e.target === backdrop) closeModal(); });
  function initialsField(id = "f-initials") {
    // Autocomplete from the admin roster, but free typing is always allowed.
    const opts = (cfg.initials || []).map(i => `<option value="${BT.escapeHtml(i)}">`).join("");
    return `<label>Initials *</label><input id="${id}" value="${BT.escapeHtml(myInitials())}"
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
    if (r.ok) { rememberInitials(payload.initials); closeModal(); }
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
    const mergeable = snap.tiles.filter(x => x.bay_id !== t.bay_id
      && x.work_order && effStatus(x) !== "IDLE");

    const buttons = [];
    if (st === "DELAYED") {
      buttons.push(`<button class="good" data-act="clear">✓ Clear delay</button>`);
      buttons.push(`<button data-act="move">→ Move to bay…</button>`);
    } else if (st === "DONE") {
      // Work finished here; the part waits in the bay. It can only move onward.
      buttons.push(`<button data-act="move">→ Move to bay…</button>`);
    } else { // RUNNING
      buttons.push(`<button class="danger" data-act="delay">⚠ Flag delay</button>`);
      buttons.push(`<button data-act="move">→ Move to bay…</button>`);
      buttons.push(`<button class="warn" data-act="complete">✓ Work done at bay (stays here)</button>`);
    }
    if (mergeable.length) buttons.push(`<button data-act="merge">⚯ Merge into bay…</button>`);
    buttons.push(`<button data-act="unit_complete">★ Unit complete</button>`);

    const stLabel = st === "DONE" ? "done — awaiting next step" : st;
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

  // ---- wire up clicks -----------------------------------------------------
  document.getElementById("grid").addEventListener("click", e => {
    const tile = e.target.closest(".tile.clickable");
    if (tile) openBay(parseInt(tile.dataset.bay, 10));
  });

  // ---- boot ---------------------------------------------------------------
  loadConfig().then(() => {
    BT.connect({ onState: render });
    BT.startTicker(render);
    setInterval(loadConfig, 30000); // pick up admin changes to reasons/products
  });
})();
