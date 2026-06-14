/* ===========================================================================
   admin.js -- the configuration editor.

   Loads everything from /api/admin/data, renders editable tables, and POSTs
   changes back. Nothing is pre-filled with invented values: the shift/break/
   operating times in particular start empty and the user enters the real ones.
   Config rows referenced by id (reasons, divisions, products, bays) are
   soft-retired (active=0), never hard-deleted, so historical events stay
   valid. Initials are the one exception: hard-deleted to keep the roster
   short (events snapshot the initials text, so history is unaffected).
   ========================================================================== */
(function () {
  const WEEK = [["mon","Mon"],["tue","Tue"],["wed","Wed"],["thu","Thu"],["fri","Fri"],["sat","Sat"],["sun","Sun"]];
  let data = null;
  let breaks = [];     // working copies of the editable arrays
  let shifts = [];

  const $ = id => document.getElementById(id);
  const esc = BT.escapeHtml;

  async function load() {
    const r = await BT.get("/api/admin/data");
    if (!r.ok) { if (r.status === 403) location.reload(); return; }
    data = r.data;
    breaks = (data.schedule.break_schedule || []).slice();
    shifts = (data.schedule.shifts || []).slice();
    renderAll();
  }
  async function send(url, body, btn) {
    const r = await BT.post(url, body);
    if (!r.ok) { alert((r.data && r.data.error) || "Save failed."); }
    else { toast("Saved ✓"); if (btn) flashSaved(btn); }
    await load();
    return r.ok;
  }

  // Two confirmations after a successful save: a banner toast at the top, and
  // the clicked button briefly turns green and reads "Saved ✓" -- so there is
  // no doubt the setting was stored.
  let _toastTimer = null;
  function toast(msg) {
    const el = $("toast"); if (!el) return;
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => el.classList.remove("show"), 2400);
  }
  function flashSaved(btn) {
    if (!btn || btn._flashing) return;
    btn._flashing = true;
    const orig = btn.textContent;
    btn.textContent = "Saved ✓";
    btn.classList.add("saved-flash");
    setTimeout(() => {
      btn.textContent = orig;
      btn.classList.remove("saved-flash");
      btn._flashing = false;
    }, 1600);
  }

  function renderAll() {
    renderDivisions(); renderReasons(); renderProducts(); renderInitials();
    renderBays(); renderBreaks(); renderShifts(); renderOC(); fillSettings(); fillPins();
    // reason division dropdown
    $("nr-div").innerHTML = `<option value="">—</option>` +
      data.divisions.filter(d => d.active).map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join("");
  }

  // ---- Divisions ----
  // All config lists use hard DELETE (not soft-retire): delay events snapshot
  // the division/reason text and products are stored as text, so deleting a
  // config row never rewrites history. Keeps the lists short year over year.
  function renderDivisions() {
    $("divisions").innerHTML = data.divisions.map(d =>
      `<tr><td>${esc(d.name)}</td>
        <td style="text-align:right">
          <button class="btn" data-edit-div="${d.id}" data-name="${esc(d.name)}">Rename</button>
          <button class="btn" data-delete-div="${d.id}" data-name="${esc(d.name)}">Delete</button>
        </td></tr>`).join("") || `<tr><td class="no-data">none yet</td></tr>`;
  }

  // ---- Reasons ----
  function renderReasons() {
    $("reasons").innerHTML = data.reasons.map(r => {
      const locked = r.is_other;
      return `<tr><td>${esc(r.label)}${locked ? ' <span class="badge">pinned</span>' : ""}</td>
        <td>${esc(r.division_name || "")}</td><td>${esc(r.in_out_of_control || "")}</td>
        <td style="text-align:right">
          ${locked ? "" : `<button class="btn" data-edit-reason='${JSON.stringify(r).replace(/'/g,"&#39;")}'>Edit</button>
          <button class="btn" data-delete-reason="${r.id}" data-label="${esc(r.label)}">Delete</button>`}
        </td></tr>`;
    }).join("");
  }

  // ---- Products ----
  function renderProducts() {
    $("products").innerHTML = data.products.map(p =>
      `<tr><td>${esc(p.number)}</td><td>${esc(p.description || "")}</td><td>${p.target_minutes ?? ""}</td>
        <td style="text-align:right">
          <button class="btn" data-edit-product='${JSON.stringify(p).replace(/'/g,"&#39;")}'>Edit</button>
          <button class="btn" data-delete-product="${p.id}" data-number="${esc(p.number)}">Delete</button>
        </td></tr>`).join("") || `<tr><td class="no-data">none yet</td></tr>`;
  }

  // ---- Initials ----
  // Initials are hard-DELETED (not soft-retired) on purpose: the roster is
  // only an autocomplete list and events keep the initials text itself, so
  // the list stays short instead of accumulating retired names for years.
  function renderInitials() {
    $("initials").innerHTML = data.initials.map(i =>
      `<tr><td>${esc(i.initials)}</td><td>${esc(i.name || "")}</td>
        <td style="text-align:right">
          <button class="btn" data-edit-initials='${JSON.stringify(i).replace(/'/g,"&#39;")}'>Edit</button>
          <button class="btn" data-delete-initials="${i.id}" data-ini="${esc(i.initials)}">Delete</button>
        </td></tr>`).join("") || `<tr><td class="no-data">none yet</td></tr>`;
  }

  // ---- Bays & layout ----
  function renderBays() {
    $("lay-cols").value = data.settings.grid_cols;
    $("lay-rows").value = data.settings.standard_rows;
    $("lay-extras").checked = !!data.settings.extras_enabled;
    $("bays").innerHTML = data.bays.map(b =>
      `<tr><td>${esc(b.name)}</td><td>${b.is_extra ? "extra" : "standard"}</td>
        <td>${b.is_extra ? (b.grid_col ?? "") : ""}</td>
        <td>${b.active ? "active" : '<span class="badge">hidden</span>'}</td>
        <td style="text-align:right">
          <button class="btn" data-rename-bay="${b.id}" data-name="${esc(b.name)}">Rename</button>
          ${b.is_extra ? `<button class="btn" data-col-bay="${b.id}" data-col="${b.grid_col ?? ""}">Set column</button>` : ""}
          <button class="btn" data-toggle-bay="${b.id}" data-op="${b.active ? "retire" : "activate"}">${b.active ? "Hide" : "Show"}</button>
        </td></tr>`).join("");
  }

  // ---- Breaks ----
  function renderBreaks() {
    $("breaks").innerHTML = breaks.map((b, i) =>
      `<tr><td><input value="${esc(b.start || "")}" data-bk="${i}" data-f="start" placeholder="11:30" style="width:90px"></td>
        <td><input type="number" value="${b.minutes ?? ""}" data-bk="${i}" data-f="minutes" style="width:80px"></td>
        <td><input value="${esc(b.label || "")}" data-bk="${i}" data-f="label" placeholder="Lunch"></td>
        <td><button class="btn" data-del-break="${i}">✕</button></td></tr>`).join("")
      || `<tr><td colspan="4" class="no-data">no breaks — add the real ones</td></tr>`;
  }

  // ---- Shifts ----
  function renderShifts() {
    $("shifts").innerHTML = shifts.map((s, i) =>
      `<tr><td><input value="${esc(s.name || "")}" data-sh="${i}" data-f="name" placeholder="Day"></td>
        <td><input value="${esc(s.start || "")}" data-sh="${i}" data-f="start" placeholder="06:00" style="width:90px"></td>
        <td><input value="${esc(s.end || "")}" data-sh="${i}" data-f="end" placeholder="14:00" style="width:90px"></td>
        <td><button class="btn" data-del-shift="${i}">✕</button></td></tr>`).join("")
      || `<tr><td colspan="4" class="no-data">no shifts — add the real ones</td></tr>`;
  }

  // ---- Operating calendar ----
  function renderOC() {
    const oc = data.schedule.operating_calendar;
    $("oc-enabled").checked = oc != null;
    $("oc-days").style.display = oc != null ? "" : "none";
    $("oc-rows").innerHTML = WEEK.map(([k, lbl]) => {
      const wins = (oc && oc[k]) ? oc[k].map(w => `${w[0]}-${w[1]}`).join(", ") : "";
      return `<div class="toolbar" style="margin-bottom:6px"><div style="width:60px">${lbl}</div>
        <input data-oc="${k}" value="${esc(wins)}" placeholder="closed"></div>`;
    }).join("");
  }

  function fillSettings() {
    $("set-takeover").value = data.settings.takeover_seconds;
    $("set-rate").value = data.settings.labor_rate ?? "";
    $("set-staled").value = data.settings.stale_delay_minutes;
    $("set-staler").value = data.settings.stale_run_minutes;
    $("set-backup").value = data.settings.backup_network_path || "";
  }
  function fillPins() {
    $("pin-stats-state").textContent = data.pins.stats_set ? "set" : "not set";
    $("pin-admin-state").textContent = data.pins.admin_set ? "set" : "not set";
  }

  // ===== modal edit =====
  const backdrop = $("backdrop"), modal = $("modal");
  function openModal(html) { modal.innerHTML = html; backdrop.classList.add("show"); }
  function closeModal() { backdrop.classList.remove("show"); }
  backdrop.addEventListener("click", e => { if (e.target === backdrop) closeModal(); });

  function editReason(r) {
    const opts = `<option value="">—</option>` + data.divisions.filter(d => d.active || d.id === r.division_id)
      .map(d => `<option value="${d.id}" ${d.id === r.division_id ? "selected" : ""}>${esc(d.name)}</option>`).join("");
    openModal(`<h2>Edit reason</h2>
      <label>Label</label><input id="m-label" value="${esc(r.label)}">
      <label>Division</label><select id="m-div">${opts}</select>
      <label>Control</label><select id="m-ctrl">
        <option value="">—</option><option value="in" ${r.in_out_of_control==="in"?"selected":""}>In control</option>
        <option value="out" ${r.in_out_of_control==="out"?"selected":""}>Out of control</option></select>
      <div class="actions"><button id="x">Cancel</button><button class="primary" id="ok">Save</button></div>`);
    $("x").onclick = closeModal;
    $("ok").onclick = async () => { await send("/api/admin/reason", { op: "update", id: r.id,
      label: $("m-label").value, division_id: $("m-div").value || null, in_out_of_control: $("m-ctrl").value || null }); closeModal(); };
  }
  function editProduct(p) {
    openModal(`<h2>Edit product</h2>
      <label>Number</label><input id="m-num" value="${esc(p.number)}">
      <label>Description</label><input id="m-desc" value="${esc(p.description || "")}">
      <label>Target minutes (optional, dormant)</label><input id="m-tgt" type="number" step="any" value="${p.target_minutes ?? ""}">
      <div class="actions"><button id="x">Cancel</button><button class="primary" id="ok">Save</button></div>`);
    $("x").onclick = closeModal;
    $("ok").onclick = async () => { await send("/api/admin/product", { op: "update", id: p.id,
      number: $("m-num").value, description: $("m-desc").value, target_minutes: $("m-tgt").value || null }); closeModal(); };
  }
  function editInitials(i) {
    openModal(`<h2>Edit initials</h2>
      <label>Initials</label><input id="m-ini" value="${esc(i.initials)}" maxlength="8">
      <label>Name</label><input id="m-name" value="${esc(i.name || "")}">
      <div class="actions"><button id="x">Cancel</button><button class="primary" id="ok">Save</button></div>`);
    $("x").onclick = closeModal;
    $("ok").onclick = async () => { await send("/api/admin/initials", { op: "update", id: i.id,
      initials: $("m-ini").value, name: $("m-name").value }); closeModal(); };
  }
  function promptModal(title, label, value, onok) {
    openModal(`<h2>${title}</h2><label>${label}</label><input id="m-v" value="${esc(value || "")}">
      <div class="actions"><button id="x">Cancel</button><button class="primary" id="ok">Save</button></div>`);
    $("x").onclick = closeModal; $("ok").onclick = async () => { await onok($("m-v").value); closeModal(); };
  }

  // ===== add buttons =====
  $("add-division").onclick = () => send("/api/admin/division", { op: "add", name: $("new-division").value });
  $("add-reason").onclick = () => send("/api/admin/reason", { op: "add", label: $("nr-label").value,
    division_id: $("nr-div").value || null, in_out_of_control: $("nr-ctrl").value || null });
  $("add-product").onclick = () => send("/api/admin/product", { op: "add", number: $("np-number").value,
    description: $("np-desc").value, target_minutes: $("np-target").value || null });
  $("add-initials").onclick = () => send("/api/admin/initials", { op: "add", initials: $("ni-ini").value, name: $("ni-name").value });
  $("add-bay").onclick = () => send("/api/admin/bay", { op: "add_extra", name: $("nb-name").value, grid_col: $("nb-col").value || null });

  $("save-layout").onclick = (e) => send("/api/admin/layout", {
    grid_cols: parseInt($("lay-cols").value, 10), standard_rows: parseInt($("lay-rows").value, 10),
    extras_enabled: $("lay-extras").checked }, e.currentTarget);

  // ===== breaks / shifts editing =====
  function syncBreaks() {
    document.querySelectorAll("[data-bk]").forEach(inp => {
      const i = +inp.dataset.bk, f = inp.dataset.f;
      breaks[i][f] = f === "minutes" ? (parseInt(inp.value, 10) || 0) : inp.value.trim();
    });
  }
  function syncShifts() {
    document.querySelectorAll("[data-sh]").forEach(inp => {
      shifts[+inp.dataset.sh][inp.dataset.f] = inp.value.trim();
    });
  }
  $("add-break").onclick = () => { syncBreaks(); breaks.push({ start: "", minutes: 10, label: "" }); renderBreaks(); };
  $("add-shift").onclick = () => { syncShifts(); shifts.push({ name: "", start: "", end: "" }); renderShifts(); };
  document.addEventListener("click", e => {
    const db_ = e.target.closest("[data-del-break]"); if (db_) { syncBreaks(); breaks.splice(+db_.dataset.delBreak, 1); renderBreaks(); }
    const ds = e.target.closest("[data-del-shift]"); if (ds) { syncShifts(); shifts.splice(+ds.dataset.delShift, 1); renderShifts(); }
  });
  function validHHMM(s) { return /^([01]?\d|2[0-4]):[0-5]\d$/.test(s); }
  $("save-breaks").onclick = (e) => {
    syncBreaks();
    for (const b of breaks) { if (!validHHMM(b.start)) return alert(`Bad break time: "${b.start}". Use HH:MM.`); }
    send("/api/admin/schedule", { break_schedule: breaks }, e.currentTarget);
  };
  $("save-shifts").onclick = (e) => {
    syncShifts();
    for (const s of shifts) {
      if (!s.name || !validHHMM(s.start) || !validHHMM(s.end))
        return alert(`Each shift needs a name plus HH:MM start and end (a window may wrap midnight, e.g. 22:00-06:00).`);
    }
    send("/api/admin/schedule", { shifts }, e.currentTarget);
  };

  // ===== operating calendar =====
  $("oc-enabled").onchange = function () { $("oc-days").style.display = this.checked ? "" : "none"; };
  $("oc-fill").onclick = () => {
    document.querySelectorAll("[data-oc]").forEach(inp => {
      inp.value = inp.dataset.oc === "sun" ? "" : "00:00-24:00";
    });
  };
  function parseWindows(text) {
    const out = [];
    text = text.trim(); if (!text) return out;
    for (const part of text.split(",")) {
      const m = part.trim().match(/^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$/);
      if (!m || !validHHMM(m[1]) || !validHHMM(m[2])) throw new Error(`Bad window: "${part.trim()}" (use HH:MM-HH:MM)`);
      out.push([m[1], m[2]]);
    }
    return out;
  }
  $("save-oc").onclick = (e) => {
    const btn = e.currentTarget;
    if (!$("oc-enabled").checked) return send("/api/admin/schedule", { operating_calendar: null }, btn);
    const oc = {};
    try {
      document.querySelectorAll("[data-oc]").forEach(inp => {
        const wins = parseWindows(inp.value);
        if (wins.length) oc[inp.dataset.oc] = wins;
      });
    } catch (err) { return alert(err.message); }
    send("/api/admin/schedule", { operating_calendar: oc }, btn);
  };

  // ===== settings & pins =====
  $("save-settings").onclick = (e) => send("/api/admin/settings", {
    takeover_seconds: parseInt($("set-takeover").value, 10),
    labor_rate: $("set-rate").value === "" ? null : parseFloat($("set-rate").value),
    stale_delay_minutes: parseInt($("set-staled").value, 10),
    stale_run_minutes: parseInt($("set-staler").value, 10),
    backup_network_path: $("set-backup").value.trim() || null }, e.currentTarget);
  $("save-pins").onclick = async () => {
    // A non-empty field sets a new PIN; the "Clear" checkbox removes it; an empty
    // field with no checkbox leaves that PIN unchanged (can't wipe by accident).
    const s = $("pin-stats").value, a = $("pin-admin").value;
    if ($("pin-stats-clear").checked) await BT.post("/api/admin/pin", { area: "stats", pin: "" });
    else if (s !== "") await BT.post("/api/admin/pin", { area: "stats", pin: s });
    if ($("pin-admin-clear").checked) await BT.post("/api/admin/pin", { area: "admin", pin: "" });
    else if (a !== "") await BT.post("/api/admin/pin", { area: "admin", pin: a });
    $("pin-stats").value = ""; $("pin-admin").value = "";
    $("pin-stats-clear").checked = false; $("pin-admin-clear").checked = false;
    toast("PINs saved ✓"); load();
  };
  $("lock-now").onclick = () => BT.post("/lock", {}).then(() => location.href = "/admin");

  // ===== delegated clicks for edit/retire =====
  document.addEventListener("click", e => {
    let b;
    if ((b = e.target.closest("[data-edit-div]"))) promptModal("Rename division", "Name", b.dataset.name,
      v => send("/api/admin/division", { op: "update", id: +b.dataset.editDiv, name: v }));
    else if ((b = e.target.closest("[data-delete-div]"))) {
      if (confirm(`Delete division "${b.dataset.name}"? Past delays keep their division either way.`))
        send("/api/admin/division", { op: "delete", id: +b.dataset.deleteDiv });
    }
    else if ((b = e.target.closest("[data-edit-reason]"))) editReason(JSON.parse(b.dataset.editReason.replace(/&#39;/g, "'")));
    else if ((b = e.target.closest("[data-delete-reason]"))) {
      if (confirm(`Delete reason "${b.dataset.label}"? Past delays keep their reason either way.`))
        send("/api/admin/reason", { op: "delete", id: +b.dataset.deleteReason });
    }
    else if ((b = e.target.closest("[data-edit-product]"))) editProduct(JSON.parse(b.dataset.editProduct.replace(/&#39;/g, "'")));
    else if ((b = e.target.closest("[data-delete-product]"))) {
      if (confirm(`Delete product "${b.dataset.number}"? Past runs keep their product number either way.`))
        send("/api/admin/product", { op: "delete", id: +b.dataset.deleteProduct });
    }
    else if ((b = e.target.closest("[data-edit-initials]"))) editInitials(JSON.parse(b.dataset.editInitials.replace(/&#39;/g, "'")));
    else if ((b = e.target.closest("[data-delete-initials]"))) {
      if (confirm(`Delete "${b.dataset.ini}" from the roster? Past log entries keep their initials either way.`))
        send("/api/admin/initials", { op: "delete", id: +b.dataset.deleteInitials });
    }
    else if ((b = e.target.closest("[data-rename-bay]"))) promptModal("Rename bay", "Name", b.dataset.name,
      v => send("/api/admin/bay", { op: "rename", id: +b.dataset.renameBay, name: v }));
    else if ((b = e.target.closest("[data-col-bay]"))) promptModal("Top-row column", "Column number", b.dataset.col,
      v => send("/api/admin/bay", { op: "set_col", id: +b.dataset.colBay, grid_col: parseInt(v, 10) || null }));
    else if ((b = e.target.closest("[data-toggle-bay]"))) send("/api/admin/bay", { op: b.dataset.op, id: +b.dataset.toggleBay });
  });

  load();
})();
