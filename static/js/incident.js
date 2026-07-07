/* ===========================================================================
   incident.js -- the EHS "Report an incident" page.

   Recreated from the Bay Tracker Design System "Incident Report" template. The
   flow:

     1. Choose Accident or Near miss. The choice LOCKS (the chooser is replaced
        by a banner) so an accident can't be double-reported.
     2. (Accident only) optionally fire an immediate PRELIMINARY alert to
        leadership before filling the form -- POST /api/incident/prelim. That
        also files the incident row, so an alert fired then abandoned still
        leaves a record.
     3. Fill the form and submit -- POST /api/incident/submit -- which files the
        full details and sends the DETAILED alert.

   Both POSTs only write to the DB + outbox; the background worker sends the
   texts/emails, so a dropped connection never loses an alert. Server responses
   echo back exactly what was queued, which we show to the operator.
   ========================================================================== */
(function () {
  const $ = (id) => document.getElementById(id);
  const wrap = $("ir-wrap");

  // ---- state (mirrors the design's component state) ----
  const state = {
    type: null,          // 'ACCIDENT' | 'NEAR_MISS'
    by: "",              // reporter initials (shared by both initials inputs)
    prelimSent: false,
    confirming: false,
    submitted: false,
    incidentId: null,    // set once a preliminary alert has filed the row
  };

  const els = {
    chooser: $("ir-chooser"),
    bannerAcc: $("ir-banner-acc"),
    bannerNm: $("ir-banner-nm"),
    form: $("ir-form"),
    notify: $("ir-notify"),
    notifyBy: $("ir-notify-by"),
    notifyBtn: $("ir-notify-btn"),
    confirm: $("ir-confirm"),
    confirmCancel: $("ir-confirm-cancel"),
    confirmSend: $("ir-confirm-send"),
    prelimSent: $("ir-prelim-sent"),
    by: $("ir-by"),
    what: $("ir-what"),
    submit: $("ir-submit"),
    error: $("ir-error"),
    done: $("ir-done"),
    doneLabel: $("ir-done-label"),
    sentLog: $("ir-sent-log"),
    newBtn: $("ir-new"),
  };

  // Default the "When" field to now (local), like the design's componentDidMount.
  function nowLocal() {
    const d = new Date(), p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  $("ir-when").value = nowLocal();

  // ---- render: reflect state into the DOM ----
  function render() {
    wrap.classList.toggle("is-accident", state.type === "ACCIDENT");
    wrap.classList.toggle("is-nearmiss", state.type === "NEAR_MISS");

    const isAccident = state.type === "ACCIDENT";
    const showForm = !!state.type && !state.submitted;

    els.chooser.hidden = !!state.type;
    els.bannerAcc.hidden = !(isAccident && !state.submitted);
    els.bannerNm.hidden = !(state.type === "NEAR_MISS" && !state.submitted);
    els.form.hidden = !showForm;

    els.notify.hidden = !(isAccident && showForm && !state.prelimSent && !state.confirming);
    els.confirm.hidden = !(state.confirming && showForm);
    els.prelimSent.hidden = !(state.prelimSent && showForm);

    els.done.hidden = !state.submitted;

    // Keep both initials inputs in sync with state.by.
    if (els.by.value !== state.by) els.by.value = state.by;
    if (els.notifyBy.value !== state.by) els.notifyBy.value = state.by;

    els.notifyBtn.disabled = !state.by.trim();
    els.submit.disabled = !(state.by.trim() && els.what.value.trim());
  }

  function setError(msg) {
    if (msg) {
      els.error.textContent = msg;
      els.error.classList.add("is-error");
    } else {
      els.error.textContent = 'Reported-by and "what happened" are required to submit.';
      els.error.classList.remove("is-error");
    }
  }

  // ---- choose a type (locks) ----
  els.chooser.querySelectorAll(".ir-choice").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (state.type) return;               // already locked
      state.type = btn.dataset.type;
      render();
    });
  });

  // ---- initials inputs (kept in sync) ----
  function onBy(e) { state.by = e.target.value; render(); }
  els.by.addEventListener("input", onBy);
  els.notifyBy.addEventListener("input", onBy);
  els.what.addEventListener("input", render);

  // ---- preliminary alert flow (accident only) ----
  els.notifyBtn.addEventListener("click", () => {
    if (state.prelimSent || !state.by.trim()) return;
    state.confirming = true;
    render();
  });
  els.confirmCancel.addEventListener("click", () => {
    state.confirming = false;
    render();
  });
  els.confirmSend.addEventListener("click", async () => {
    els.confirmSend.disabled = true;
    const r = await BT.post("/api/incident/prelim", {
      type: state.type,
      location: $("ir-loc").value,
      by: state.by,
    });
    els.confirmSend.disabled = false;
    if (!r.ok) {
      setError((r.data && r.data.error) || "Could not send the preliminary alert.");
      return;
    }
    setError(null);
    state.prelimSent = true;
    state.confirming = false;
    state.incidentId = r.data.incident_id;
    appendSent(r.data.sent);
    render();
  });

  // ---- submit the full report ----
  els.submit.addEventListener("click", async () => {
    if (els.submit.disabled) return;
    els.submit.disabled = true;
    const payload = {
      type: state.type,
      incident_id: state.incidentId,   // null unless a preliminary already filed the row
      loc: $("ir-loc").value,
      when: $("ir-when").value,
      by: state.by,
      severity: $("ir-severity").value,
      potential: $("ir-potential").value,
      person: $("ir-person").value,
      injury: $("ir-injury").value,
      medical: $("ir-medical").value,
      what: els.what.value,
      action: $("ir-action").value,
      equip: $("ir-equip").value,
    };
    const r = await BT.post("/api/incident/submit", payload);
    if (!r.ok) {
      els.submit.disabled = false;
      setError((r.data && r.data.error) || "Could not submit the report.");
      return;
    }
    setError(null);
    state.submitted = true;
    els.doneLabel.textContent = state.type === "ACCIDENT" ? "Accident" : "Near miss";
    appendSent(r.data.sent);
    render();
  });

  // ---- reset (Cancel / Report another) ----
  $("ir-cancel").addEventListener("click", reset);
  els.newBtn.addEventListener("click", reset);

  function reset() {
    state.type = null;
    state.by = "";
    state.prelimSent = false;
    state.confirming = false;
    state.submitted = false;
    state.incidentId = null;
    ["ir-person", "ir-injury", "ir-what", "ir-action", "ir-equip"].forEach((id) => { $(id).value = ""; });
    $("ir-when").value = nowLocal();
    els.sentLog.innerHTML = "";
    setError(null);
    render();
  }

  // ---- render a queued-message card (echoes what the server queued) ----
  function appendSent(sent) {
    if (!sent) return;
    const rc = sent.recipients || { email: 0, sms: 0 };
    const people = [];
    if (rc.email) people.push(rc.email + " email" + (rc.email > 1 ? "s" : ""));
    if (rc.sms) people.push(rc.sms + " text" + (rc.sms > 1 ? "s" : ""));
    const to = people.length ? people.join(" · ") : "no recipients configured yet";
    const card = document.createElement("div");
    card.className = "ir-sent-card" + (sent.kind === "PRELIMINARY" ? " prelim" : "");
    card.innerHTML =
      '<div class="ir-sent-meta">' + BT.escapeHtml(sent.kind) + " · " +
      BT.escapeHtml(sent.time) + " · " + BT.escapeHtml(to) + "</div>" +
      '<div class="ir-sent-body">' + BT.escapeHtml(sent.body) + "</div>";
    els.sentLog.prepend(card);
  }

  render();
})();
