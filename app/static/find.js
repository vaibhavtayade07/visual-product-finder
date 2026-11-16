/* Shopper widget: upload/capture -> poll -> render cards / fallback (vanilla JS, no build). */
(function () {
  "use strict";

  const root = document.getElementById("finder");
  const API = root.dataset.api;
  const TENANT = root.dataset.tenant;
  const CONSENT_VERSION = root.dataset.consent;

  const LABELS = {
    closest_match: "Closest match",
    similar_style: "Similar style",
    same_category: "Same category",
  };
  const STOCK_LABELS = { in_stock: "In stock", low_stock: "Low stock", out_of_stock: "Out of stock" };
  const MAX_BYTES = 10 * 1024 * 1024;
  const MIN_SIDE = 200;
  const POLL_TIMEOUT_MS = 45000;

  const el = (id) => document.getElementById(id);
  const ui = {
    consent: el("consent-box"), fileInput: el("file-input"), cameraInput: el("camera-input"),
    clientError: el("client-error"), photoWarn: el("photo-warn"), previewWrap: el("preview-wrap"),
    preview: el("preview"), submit: el("submit-btn"), retake: el("retake-btn"),
    progress: el("progress"), progressText: el("progress-text"), resultsSection: el("results-section"),
    results: el("results"), resultsTitle: el("results-title"), filters: el("filters"),
    fallback: el("fallback-section"), leadForm: el("lead-form"), leadError: el("lead-error"),
    leadSuccess: el("lead-success"), popular: el("popular"), errorView: el("error-view"),
    errorText: el("error-text"), retry: el("retry-btn"), deletePhoto: el("delete-photo-btn"),
    degraded: el("degraded-banner"), refineFallback: el("refine-fallback"),
    refineFallbackText: el("refine-fallback-text"), clearFilters: el("clear-filters-btn"),
  };

  let sessionId = localStorage.getItem("vpf_session_id");
  if (!sessionId) {
    sessionId = (crypto.randomUUID && crypto.randomUUID()) ||
      (Date.now().toString(36) + "-" + Math.random().toString(36).slice(2));
    localStorage.setItem("vpf_session_id", sessionId);
  }

  let state = {
    file: null, queryId: null, pollTimer: null, pollStartedAt: 0,
    payload: null, fullPayload: null, activeFilters: { finish: [], color: [], size_class: [], in_stock_only: false },
  };

  function showError(target, message) {
    target.textContent = message;
    target.hidden = false;
  }
  function clearErrors() {
    ui.clientError.hidden = true;
    ui.photoWarn.hidden = true;
    ui.errorView.hidden = true;
  }

  async function apiJson(path, options) {
    const resp = await fetch(API + path, options);
    let body = null;
    try { body = await resp.json(); } catch (e) { /* noop */ }
    if (!resp.ok) {
      const message = (body && body.error && body.error.message) || "Something went wrong. Please try again.";
      const err = new Error(message);
      err.status = resp.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  // ---- upload / capture ----------------------------------------------------
  function onFilePicked(file) {
    clearErrors();
    if (!file) return;
    if (file.size > MAX_BYTES) { showError(ui.clientError, "JPEG, PNG or WebP under 10 MB."); return; }
    const typeOk = /^image\/(jpeg|png|webp|heic|heif)$/.test(file.type) ||
      (!file.type && /\.(jpe?g|png|webp|heic|heif)$/i.test(file.name));
    if (!typeOk) { showError(ui.clientError, "JPEG, PNG or WebP under 10 MB."); return; }

    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(img.src);
      if (Math.min(img.naturalWidth, img.naturalHeight) < MIN_SIDE) {
        showError(ui.clientError, "That photo is a bit small — the shortest side should be at least 200px.");
        return;
      }
      warnIfDark(file);
      state.file = file;
      ui.preview.src = URL.createObjectURL(file);
      ui.previewWrap.hidden = false;
      ui.fallback.hidden = true;
      ui.resultsSection.hidden = true;
    };
    img.onerror = () => showError(ui.clientError, "We couldn't read that file. Try a JPEG, PNG or WebP.");
    img.src = URL.createObjectURL(file);
  }

  function warnIfDark(file) {
    // E1: pre-warn only, never block.
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = 32; canvas.height = 32;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(img, 0, 0, 32, 32);
      const data = ctx.getImageData(0, 0, 32, 32).data;
      let sum = 0;
      for (let i = 0; i < data.length; i += 4) sum += 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
      const mean = sum / (data.length / 4);
      if (mean < 45) showError(ui.photoWarn, "That photo may be too dark — try another one if results look off.");
      URL.revokeObjectURL(img.src);
    };
    img.src = URL.createObjectURL(file);
  }

  ui.fileInput.addEventListener("change", (e) => onFilePicked(e.target.files[0]));
  ui.cameraInput.addEventListener("change", (e) => onFilePicked(e.target.files[0]));
  ui.retake.addEventListener("click", () => {
    state.file = null; state.queryId = null;
    ui.previewWrap.hidden = true; ui.clientError.hidden = true; ui.photoWarn.hidden = true;
    ui.fileInput.value = ""; ui.cameraInput.value = "";
  });

  // ---- submit + poll --------------------------------------------------------
  ui.submit.addEventListener("click", () => submit());
  ui.retry.addEventListener("click", () => submit());

  async function submit() {
    state.queryId = null;
    if (!state.file) return;
    if (!ui.consent.checked) {
      showError(ui.clientError, "Please tick the consent box before uploading your photo.");
      return;
    }
    clearErrors();
    ui.progress.hidden = false;
    ui.progressText.textContent = "Looking through the catalog…";
    ui.resultsSection.hidden = true;
    ui.fallback.hidden = true;

    const form = new FormData();
    form.append("image", state.file, state.file.name || "photo.jpg");
    form.append("session_id", sessionId);
    form.append("consent_version", CONSENT_VERSION);
    form.append("tenant", TENANT);

    try {
      const body = await apiJson("/search", { method: "POST", body: form });
      state.queryId = body.query_id;
      state.pollStartedAt = Date.now();
      poll(body.query_id, body.poll_after_ms || 1500);
    } catch (err) {
      ui.progress.hidden = true;
      showError(ui.clientError, err.message);
    }
  }

  function poll(queryId, afterMs) {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(async () => {
      try {
        const payload = await apiJson("/search/" + queryId);
        if (payload.status === "done") { render(payload); return; }
        if (payload.status === "failed") { renderFailure(payload); return; }
        if (payload.results && payload.results.length) { render(payload, true); return; }
        if (Date.now() - state.pollStartedAt > POLL_TIMEOUT_MS) {
          showError(ui.errorText, "This is taking longer than expected.");
          ui.progress.hidden = true;
          ui.errorView.hidden = false;
          return;
        }
        poll(queryId, payload.poll_after_ms || 1500);
      } catch (err) {
        ui.progress.hidden = true;
        showError(ui.errorText, err.message);
        ui.errorView.hidden = false;
      }
    }, afterMs);
  }

  function renderFailure(payload) {
    ui.progress.hidden = true;
    const message = (payload.error && payload.error.message) || "We couldn't process this photo.";
    showError(ui.errorText, message);
    ui.errorView.hidden = false;
  }

  // ---- render ---------------------------------------------------------------
  function render(payload, stillPolling) {
    state.payload = payload;
    ui.progress.hidden = !stillPolling;
    if (stillPolling) ui.progressText.textContent = "Style matches (still refining)…";
    ui.errorView.hidden = true;
    ui.fallback.hidden = true;
    ui.refineFallback.hidden = true;

    if (payload.degraded) ui.degraded.hidden = false; else ui.degraded.hidden = true;

    if (payload.results && payload.results.length) {
      state.fullPayload = payload;
      ui.resultsTitle.textContent = payload.band === "high" ? "Closest matches" : "Similar results";
      renderCards(ui.results, payload.results);
      renderFilters(payload.filters);
      ui.resultsSection.hidden = false;
    } else if (payload.fallback) {
      ui.resultsSection.hidden = true;
      renderFallback(payload.fallback);
    }
  }

  function renderCards(target, cards) {
    target.innerHTML = "";
    cards.forEach((card) => {
      const node = document.createElement("article");
      node.className = "card-product";
      const img = document.createElement("img");
      img.src = card.image_url || "";
      img.alt = card.title || card.sku;
      img.loading = "lazy";
      node.appendChild(img);

      const body = document.createElement("div");
      body.className = "body";

      const label = document.createElement("span");
      label.className = "badge label";
      label.textContent = LABELS[card.similarity_label] || LABELS.similar_style;
      body.appendChild(label);

      const title = document.createElement("a");
      title.className = "title";
      title.href = card.product_url || "#";
      title.target = "_blank";
      title.rel = "noopener";
      title.textContent = card.title || card.sku;
      title.addEventListener("click", () => logClick(card));
      body.appendChild(title);

      const price = document.createElement("div");
      price.className = "price";
      price.textContent = card.price != null ? (card.currency || "USD") + " " + Number(card.price).toFixed(2) : "";
      body.appendChild(price);

      const stock = document.createElement("span");
      stock.className = "badge " + (card.stock_status || "");
      stock.textContent = STOCK_LABELS[card.stock_status] || card.stock_status || "";
      body.appendChild(stock);

      if (card.reason) {
        const reason = document.createElement("div");
        reason.className = "reason";
        reason.textContent = card.reason;
        body.appendChild(reason);
      }

      node.appendChild(body);
      target.appendChild(node);
    });
  }

  function renderFallback(fallback) {
    ui.fallback.querySelector("h2").textContent = fallback.message || "We couldn't find a close match";
    renderCards(ui.popular, fallback.popular || []);
    ui.fallback.hidden = false;
  }

  // ---- refine ---------------------------------------------------------------
  function renderFilters(filters) {
    if (!filters) { ui.filters.hidden = true; return; }
    ui.filters.innerHTML = "";
    ["finish", "color", "size_class"].forEach((group) => {
      (filters[group] || []).forEach((value) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip";
        chip.textContent = value.replace(/_/g, " ");
        chip.addEventListener("click", () => toggleFilter(group, value, chip));
        ui.filters.appendChild(chip);
      });
    });
    const stockChip = document.createElement("button");
    stockChip.type = "button";
    stockChip.className = "chip";
    stockChip.textContent = "In stock only";
    stockChip.addEventListener("click", () => toggleFilter("in_stock_only", true, stockChip));
    ui.filters.appendChild(stockChip);
    ui.filters.hidden = false;
  }

  function toggleFilter(group, value, chip) {
    if (group === "in_stock_only") {
      state.activeFilters.in_stock_only = !state.activeFilters.in_stock_only;
      chip.classList.toggle("active", state.activeFilters.in_stock_only);
    } else {
      const list = state.activeFilters[group];
      const idx = list.indexOf(value);
      if (idx >= 0) list.splice(idx, 1); else list.push(value);
      chip.classList.toggle("active", idx < 0);
    }
    applyFilters();
  }

  async function applyFilters() {
    const anyActive = state.activeFilters.finish.length || state.activeFilters.color.length ||
      state.activeFilters.size_class.length || state.activeFilters.in_stock_only;
    if (!anyActive) { render(state.fullPayload || state.payload); return; }
    try {
      const payload = await apiJson("/search/" + state.queryId + "/refine", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filters: state.activeFilters, top_n: 12 }),
      });
      if (payload.results && payload.results.length) {
        ui.refineFallback.hidden = true;
        renderCards(ui.results, payload.results);
      } else {
        ui.results.innerHTML = "";
        ui.refineFallbackText.textContent = (payload.fallback && payload.fallback.message) || "No results with these filters";
        renderCards(ui.popular, (payload.fallback && payload.fallback.popular) || []);
        ui.refineFallback.hidden = false;
      }
    } catch (err) {
      showError(ui.clientError, err.message);
    }
  }

  ui.clearFilters.addEventListener("click", () => {
    state.activeFilters = { finish: [], color: [], size_class: [], in_stock_only: false };
    ui.filters.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
    render(state.fullPayload || state.payload);
  });

  // ---- clicks / lead / delete -------------------------------------------------
  function logClick(card) {
    if (!state.queryId) return;
    fetch(API + "/events/click", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, query_id: state.queryId, sku: card.sku, position: card.position || 0 }),
      keepalive: true,
    }).catch(() => {});
  }

  ui.leadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    ui.leadError.hidden = true;
    const data = Object.fromEntries(new FormData(ui.leadForm).entries());
    try {
      await apiJson("/leads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          search_query_id: state.queryId, session_id: sessionId,
          name: data.name, email: data.email, phone: data.phone, note: data.note,
        }),
      });
      ui.leadForm.hidden = true;
      ui.leadSuccess.hidden = false;
    } catch (err) {
      const fields = err.body && err.body.error && err.body.error.details && err.body.error.details.fields;
      const message = fields ? Object.values(fields).join(" ") : err.message;
      showError(ui.leadError, message);
    }
  });

  ui.deletePhoto.addEventListener("click", async () => {
    if (!state.queryId) return;
    try {
      await apiJson("/search/" + state.queryId + "?session_id=" + encodeURIComponent(sessionId), { method: "DELETE" });
      ui.deletePhoto.textContent = "Photo deleted";
      ui.deletePhoto.disabled = true;
    } catch (err) {
      showError(ui.clientError, err.message);
    }
  });
})();
