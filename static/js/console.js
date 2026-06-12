/* ===========================================================================
   console.js -- the interactive logging view (central PC).

   Mirrors the dashboard grid, but every tile is clickable. The common path is
   optimized for the fewest clicks/keystrokes:
     * "Your initials" is set once and pre-fills every action.
     * Product entry is type-to-filter, matching consecutive digits ANYWHERE in
       the number (so the last 3 digits find it), with an "Other" free-text path.
     * Barcode scanners just type into the focused field + Enter.
   It live-refreshes over SSE so two people editing don't clobber each other:
   every action re-reads current state server-side before logging.
   ========================================================================== */
(function () {
  let cfg = { reasons: [], products: [], initials: [], bays: [],
              layout: { grid_cols: 4, standard_rows: 3, extras_enabled: false } };

  const myInitialsEl = document.getElementById("my-initials");
  myInitialsEl.value = localStorage.getItem("bt_initials") || "";
  myInitialsEl.addEventListener("input", () =>
    localStorage.setItem("bt_initials", myInitialsEl.value.trim()));
  const myInitials = () => myInitialsEl.value.trim();

  async function loadConfig() {
    const r = await BT.get("/api/config");
    if (r.ok) cfg = r.data;
  }

  // ---- effective status (a bay on break still acts on its underlying run) ----
  function effStatus(t) { return t.status === "ON_BREAK" ? (t.paused_status || "IDLE") : t.status; }

  // ---- grid rendering (clickable) ----------------------------------------
  const ICON = { RUNNING: "▶", DELAYED: "⚠", IDLE: "○", ON_BREAK: "⏸" };
  const LABEL = { RUNNING: "RUNNING", DELAYED: "DELAYED", IDLE: "IDLE", ON_BREAK: "ON BREAK" };

  function tileHTML(t) {
    const cls = "tile clickable " + t.status.toLowerCase();
    const state = `<span class="state-label"><span class="icon">${ICON[t.status]}</span>${LABEL[t.status]}</span>`;
    const twobay = t.occupies_two ? `<span class="twobay">2 BAYS</span>` : "";
    if (t.status === "IDLE") {
      return `<div class="${cls}" data-bay="${t.bay_id}">
        <div class="tile-row"><span class="bay-name">${BT.escapeHtml(t.name)}</span>${state}</div>
        <div class="meta">tap to start</div></div>`;
    }
    const elapsed = BT.fmtElapsed(BT.liveElapsed(t));
    if (effStatus(t) === "DELAYED") {
      const d = t.delay || {};
      const div = d.division ? `<span class="divtag">${BT.escapeHtml(d.division)}</span>` : "";
      return `<div class="${cls}" data-bay="${t.bay_id}">${twobay}
        <div class="tile-row"><span class="bay-name">${BT.escapeHtml(t.name)}</span>${state}</div>
        <div class="wo">${BT.escapeHtml(t.work_order)}</div>
        <div class="reason">${BT.escapeHtml(d.reason || "")} ${div}</div>
        <div class="tile-row"><span class="meta">delayed</span><span class="elapsed">${elapsed}</span></div></div>`;
    }
    const comp = t.component_label ? `<span class="meta">· ${BT.escapeHtml(t.component_label)}</span>` : "";
    return `<div class="${cls}" data-bay="${t.bay_id}">${twobay}
      <div class="tile-row"><span class="bay-name">${BT.escapeHtml(t.name)}</span>${state}</div>
      <div class="wo">${BT.escapeHtml(t.work_order)}</div>
      <div class="meta">${BT.escapeHtml(t.product_number || "")} ${comp}</div>
      <div class="tile-row"><span class="meta">${t.status === "ON_BREAK" ? "paused" : "active"}</span>
        <span class="elapsed">${elapsed}</span></div></div>`;
  }

  function render(snap) {
    const grid = document.getElementById("grid");
    const cols = cfg.layout.grid_cols || 4;
    grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
    const tiles = snap.tiles || [];
    const standard = tiles.filter(t => !t.is_extra);
    const extras = tiles.filter(t => t.is_extra);
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

    // WIP pool
    const wipCard = document.getElementById("wip-card");
    const wipList = document.getElementById("wip-list");
    if (snap.queue && snap.queue.length) {
      wipCard.style.display = "";
      wipList.innerHTML = snap.queue.map(q =>
        `<button class="btn" data-wo="${BT.escapeHtml(q.work_order)}" data-pn="${BT.escapeHtml(q.product_number||"")}">
           ${BT.escapeHtml(q.work_order)} <span class="hint">${BT.escapeHtml(q.product_number||"")}</span></button>`).join("");
    } else { wipCard.style.display = "none"; wipList.innerHTML = ""; }
  }

  // ---- modal helpers ------------------------------------------------------
  const backdrop = document.getElementById("backdrop");
  const modal = document.getElementById("modal");
  function openModal(html) { modal.innerHTML = html; backdrop.classList.add("show"); }
  function closeModal() { backdrop.classList.remove("show"); modal.innerHTML = ""; }
  backdrop.addEventListener("click", e => { if (e.target === backdrop) closeModal(); });
  function initialsField(id = "f-initials") {
    return `<label>Initials *</label><input id="${id}" value="${BT.escapeHtml(myInitials())}"
            maxlength="8" autocomplete="off">`;
  }
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

    const buttons = [];
    if (st === "DELAYED") {
      buttons.push(`<button class="good" data-act="clear">✓ Clear delay</button>`);
    } else { // RUNNING
      buttons.push(`<button class="danger" data-act="delay">⚠ Flag delay</button>`);
      buttons.push(`<button data-act="move">→ Move to bay…</button>`);
      buttons.push(`<button data-act="complete">✓ Complete at bay (to queue)</button>`);
      if (t.occupies_two) buttons.push(`<button data-act="mate">⚯ Mate (join 2 bays)</button>`);
    }
    buttons.push(`<button data-act="unit_complete">★ Unit complete</button>`);
    buttons.push(`<button class="danger" data-act="scrap">✗ Scrap</button>`);

    openModal(`<h2>${BT.escapeHtml(t.name)} — ${BT.escapeHtml(t.work_order)}</h2>
      <div class="sub">${BT.escapeHtml(t.product_number || "")} · ${st}${t.component_label ? " · " + BT.escapeHtml(t.component_label) : ""}</div>
      <div class="action-menu">${buttons.join("")}</div>
      <div class="actions"><button id="cancel">Cancel</button></div>`);
    document.getElementById("cancel").onclick = closeModal;
    modal.querySelectorAll("[data-act]").forEach(b => b.onclick = () => routeAction(b.dataset.act, t));
  }

  function routeAction(act, t) {
    if (act === "clear") return doAction({ action: "clear_delay", bay_id: t.bay_id, initials: myInitials() });
    if (act === "delay") return delayModal(t);
    if (act === "move") return moveModal(t);
    if (act === "complete") return doAction({ action: "complete_bay", bay_id: t.bay_id, initials: myInitials() });
    if (act === "mate") return mateModal(t);
    if (act === "unit_complete") return confirmModal("Unit complete",
      `Mark work order <b>${BT.escapeHtml(t.work_order)}</b> as fully complete? This ends its journey.`,
      () => doAction({ action: "unit_complete", work_order: t.work_order, initials: myInitials() }));
    if (act === "scrap") return confirmModal("Scrap unit",
      `Scrap work order <b>${BT.escapeHtml(t.work_order)}</b>? This is terminal.`,
      () => doAction({ action: "scrap", work_order: t.work_order, initials: myInitials() }), true);
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

  // ---- Mate ----
  function mateModal(t) {
    const snap = BT.getSnapshot();
    const other = snap.tiles.find(x => x.bay_id !== t.bay_id && x.work_order === t.work_order
                                    && effStatus(x) !== "IDLE");
    if (!other) return;
    openModal(`<h2>Mate ${BT.escapeHtml(t.work_order)}</h2>
      <div class="sub">Join the two streams into one continuing unit. Choose which bay it continues in;
        the other frees up.</div>
      <div class="action-menu">
        <button data-keep="${t.bay_id}" data-rel="${other.bay_id}">Continue in ${BT.escapeHtml(t.name)} — free ${BT.escapeHtml(other.name)}</button>
        <button data-keep="${other.bay_id}" data-rel="${t.bay_id}">Continue in ${BT.escapeHtml(other.name)} — free ${BT.escapeHtml(t.name)}</button>
      </div>${initialsField()}${errEl()}
      <div class="actions"><button id="cancel">Cancel</button></div>`);
    document.getElementById("cancel").onclick = closeModal;
    modal.querySelectorAll("[data-keep]").forEach(b => b.onclick = (e) => doAction({ action: "mate",
      keep_bay_id: parseInt(b.dataset.keep, 10), release_bay_id: parseInt(b.dataset.rel, 10),
      initials: document.getElementById("f-initials").value.trim() }, e.target));
  }

  // ---- Start-from-queue (WIP chip) ----
  function startFromQueue(wo, pn) {
    const snap = BT.getSnapshot();
    const idle = snap.tiles.filter(x => effStatus(x) === "IDLE");
    if (!idle.length) { alert("No empty bays available."); return; }
    const opts = idle.map(b => `<option value="${b.bay_id}">${BT.escapeHtml(b.name)}</option>`).join("");
    openModal(`<h2>Start ${BT.escapeHtml(wo)} from queue</h2>
      <div class="sub">${BT.escapeHtml(pn || "")}</div>
      <label>Into bay *</label><select id="f-target">${opts}</select>
      ${initialsField()}${errEl()}
      <div class="actions"><button id="cancel">Cancel</button>
        <button class="primary" id="go">Start</button></div>`);
    document.getElementById("cancel").onclick = closeModal;
    document.getElementById("go").onclick = (e) => doAction({ action: "start",
      bay_id: parseInt(document.getElementById("f-target").value, 10),
      work_order: wo, product_number: pn,
      initials: document.getElementById("f-initials").value.trim() }, e.target);
  }

  // ---- confirm (terminal actions) ----
  function confirmModal(title, html, onYes, danger) {
    openModal(`<h2>${title}</h2><div class="sub">${html}</div>${errEl()}
      <div class="actions"><button id="cancel">Cancel</button>
        <button class="${danger ? "danger" : "primary"}" id="yes">Confirm</button></div>`);
    document.getElementById("cancel").onclick = closeModal;
    document.getElementById("yes").onclick = (e) => onYes(e.target);
  }

  // ---- wire up clicks -----------------------------------------------------
  document.getElementById("grid").addEventListener("click", e => {
    const tile = e.target.closest(".tile.clickable");
    if (tile) openBay(parseInt(tile.dataset.bay, 10));
  });
  document.getElementById("wip-list").addEventListener("click", e => {
    const b = e.target.closest("[data-wo]");
    if (b) startFromQueue(b.dataset.wo, b.dataset.pn);
  });

  // ---- boot ---------------------------------------------------------------
  loadConfig().then(() => {
    BT.connect({ onState: render });
    BT.startTicker(render);
    setInterval(loadConfig, 30000); // pick up admin changes to reasons/products
  });
})();
