/* ===========================================================================
   common.js -- shared client runtime for every page.

   Responsibilities:
     * Live updates over Server-Sent Events, with automatic reconnect AND a
       polling fallback, so a flaky wifi link self-heals (Appendix A3).
     * An "OFFLINE / last updated X ago" indicator so a frozen TV is obvious.
     * Time formatting that NEVER shows seconds ("47 min" / "1:23").
     * A 1-second ticker that advances elapsed times locally between server
       updates -- but only while the server says time IS counting, so timers
       freeze correctly during breaks and off-hours.

   Because elapsed time is recomputed from server-provided counts, a dropped
   connection never desyncs: when the link returns, the next snapshot corrects
   everything.
   ========================================================================== */
const BT = (function () {
  const HEARTBEAT_MS = 5000;     // server pushes a snapshot this often
  const OFFLINE_MS = 13000;      // no data for > ~2 heartbeats => show OFFLINE
  const POLL_MS = 8000;          // if SSE goes quiet this long, actively poll

  let lastMessageAt = Date.now();
  let lastSseAt = Date.now();      // updated ONLY by real SSE events, never by polls
  let lastSseReopenAt = 0;
  let lastSnapshot = null;
  let snapshotAt = Date.now();
  let onStateCb = null;
  let onDelayCb = null;
  let es = null;
  let pollStartedAt = 0;
  let reloadScheduled = false;

  // ---- formatting ----
  function fmtElapsed(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const m = Math.floor(sec / 60);
    if (m < 60) return m + " min";
    const h = Math.floor(m / 60), mm = m % 60;
    return h + ":" + String(mm).padStart(2, "0");
  }
  function fmtAgo(ms) {
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + " sec ago";
    const m = Math.floor(s / 60);
    if (m < 60) return m + " min ago";
    return Math.floor(m / 60) + " hr ago";
  }

  // Live elapsed for a tile: the server's counted seconds, plus locally-ticked
  // time since the snapshot ONLY when the server said time is counting. A bay
  // ON BREAK or PAUSED is frozen, so it never ticks locally.
  function liveElapsed(tile) {
    let base = tile.elapsed_seconds || 0;
    if (lastSnapshot && lastSnapshot.is_counting
        && tile.status !== "ON_BREAK" && tile.status !== "PAUSED") {
      base += (Date.now() - snapshotAt) / 1000;
    }
    return base;
  }

  // Advance any counted-seconds value locally between snapshots, but only while
  // it is actively accruing AND the server says time is counting. Used for the
  // unit total/elapsed numbers: a RUNNING bay accrues; a DONE (work-finished,
  // waiting) bay is frozen.
  function liveSeconds(base, accruing) {
    base = Math.max(0, base || 0);
    if (accruing && lastSnapshot && lastSnapshot.is_counting) {
      base += (Date.now() - snapshotAt) / 1000;
    }
    return base;
  }

  // ---- connection handling ----
  function handleMessage(type, data) {
    lastMessageAt = Date.now();
    if (type === "state") {
      lastSnapshot = data;
      snapshotAt = Date.now();
      // A kiosk never reloads on its own, so without this it would run
      // pre-update JS against a post-update server FOREVER. When the server's
      // version changes, reload once after a random delay (staggered so every
      // TV doesn't hammer a freshly-restarted server at the same instant).
      if (data && data.version && window.BT_VERSION
          && data.version !== window.BT_VERSION && !reloadScheduled) {
        reloadScheduled = true;
        setTimeout(function () { location.reload(); },
                   5000 + Math.floor(Math.random() * 55000));
      }
      if (onStateCb) onStateCb(data);
    } else if (type === "delay") {
      if (onDelayCb) onDelayCb(data);
    }
  }

  function openSSE() {
    try {
      es = new EventSource("/events");
      es.addEventListener("state", (e) => { lastSseAt = Date.now(); handleMessage("state", JSON.parse(e.data)); });
      es.addEventListener("delay", (e) => { lastSseAt = Date.now(); handleMessage("delay", JSON.parse(e.data)); });
      es.onerror = function () {
        // EventSource auto-reconnects; if it fully closed, reopen shortly.
        if (es && es.readyState === EventSource.CLOSED) {
          setTimeout(openSSE, 2000);
        }
      };
    } catch (err) {
      setTimeout(openSSE, 2000);
    }
  }

  async function pollOnce() {
    // Timestamp guard, not a boolean: a fetch wedged on a half-dead connection
    // (a hard PC loss can hang it for many minutes) must only suppress polling
    // briefly, or one stuck request keeps the board stale long after the
    // server is back.
    if (Date.now() - pollStartedAt < 15000) return;
    pollStartedAt = Date.now();
    try {
      const r = await fetch("/api/state", { cache: "no-store" });
      if (r.ok) handleMessage("state", await r.json());
    } catch (e) { /* still offline */ } finally { pollStartedAt = 0; }
  }

  function watchdog() {
    const age = Date.now() - lastMessageAt;
    const banner = document.getElementById("offline-banner");
    if (banner) {
      if (age > OFFLINE_MS) {
        banner.classList.add("show");
        banner.textContent = "OFFLINE — reconnecting… last updated " + fmtAgo(age);
      } else {
        banner.classList.remove("show");
      }
    }
    // Keep the topbar pill honest (it ships as "LIVE" in the HTML).
    const pill = document.getElementById("conn-pill");
    if (pill) {
      const stale = age > OFFLINE_MS;
      pill.textContent = stale ? "OFFLINE" : "LIVE";
      pill.classList.toggle("bad", stale);
    }
    // If SSE has gone quiet, actively poll to self-correct.
    if (age > POLL_MS) pollOnce();
    // Half-open SSE: a dead TCP connection can sit at readyState OPEN forever
    // (no error event ever fires), leaving polls carrying "state" while the
    // "delay" takeover channel is silently dead. If no REAL SSE message has
    // arrived for 30 s (the server heartbeats every 5 s), force a reconnect —
    // rate-limited so a genuinely down server isn't hammered.
    const now = Date.now();
    if (now - lastSseAt > 30000 && now - lastSseReopenAt > 30000) {
      lastSseReopenAt = now;
      try { if (es) es.close(); } catch (e) { /* already dead */ }
      openSSE();
    }
  }

  // ---- public API ----
  function connect(opts) {
    onStateCb = opts.onState || null;
    onDelayCb = opts.onDelay || null;
    openSSE();
    setInterval(watchdog, 2000);
    // Kick off an immediate poll so the page paints fast even before SSE lands.
    pollOnce();
  }

  function startTicker(renderFn) {
    setInterval(() => { if (lastSnapshot) renderFn(lastSnapshot); }, 1000);
  }

  // Both helpers NEVER reject: a network-level fetch failure (server mid-
  // restart, wifi blip) becomes { ok: false } like any HTTP error. Callers all
  // check r.ok already; an unhandled rejection here used to kill a page's
  // whole boot chain (no SSE, no polling, no offline banner — a permanently
  // dead kiosk) and leave console modals stuck on a dead button.
  async function post(url, body) {
    try {
      const r = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      let data = {};
      try { data = await r.json(); } catch (e) {}
      return { ok: r.ok && data.ok !== false, status: r.status, data };
    } catch (e) {
      return { ok: false, status: 0, data: { error: "Can't reach the server — try again." } };
    }
  }
  async function get(url) {
    try {
      const r = await fetch(url, { cache: "no-store" });
      if (!r.ok) return { ok: false, status: r.status, data: null };
      return { ok: true, status: r.status, data: await r.json() };
    } catch (e) {
      return { ok: false, status: 0, data: null };
    }
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  return { fmtElapsed, fmtAgo, liveElapsed, liveSeconds, connect, startTicker,
           post, get, escapeHtml, getSnapshot: () => lastSnapshot };
})();

/* ---- EHS first-aid button (topbar, every page) ----------------------------
   Opens the incident report in a small popup window instead of navigating the
   page away, so a console/board left on screen keeps running. Reusing the
   window name focuses the already-open report instead of stacking new ones. */
(function () {
  const btn = document.getElementById("incident-btn");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const w = 720, h = 900;
    const left = Math.max(0, Math.round((screen.width - w) / 2));
    const top = Math.max(0, Math.round((screen.height - h) / 2));
    window.open(btn.dataset.url, "bt-incident",
      `popup=yes,width=${w},height=${h},left=${left},top=${top},resizable=yes,scrollbars=yes`);
  });
})();
