/* Admin console: import, hygiene, reindex, config (vanilla JS, no build). */
(function () {
  "use strict";

  const root = document.getElementById("admin");
  const API = root.dataset.api;
  const TOKEN_KEY = "vpf_admin_token";
  const tokenInput = document.getElementById("token");
  tokenInput.value = localStorage.getItem(TOKEN_KEY) || root.dataset.token || "";
  tokenInput.addEventListener("change", () => localStorage.setItem(TOKEN_KEY, tokenInput.value));

  const el = (id) => document.getElementById(id);
  let pollTimer = null;

  function headers(json) {
    const h = { Authorization: "Bearer " + (tokenInput.value || "") };
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  async function api(path, options) {
    const resp = await fetch(API + path, options || {});
    let body = null;
    try { body = await resp.json(); } catch (e) { /* noop */ }
    if (!resp.ok) {
      const message = (body && body.error && body.error.message) || ("request failed (" + resp.status + ")");
      throw new Error(message);
    }
    return body;
  }

  // ---- import ---------------------------------------------------------------
  el("csv-input").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    await startImport(form);
  });

  el("mock-btn").addEventListener("click", async () => {
    const count = parseInt(el("mock-count").value || "300", 10);
    await startImport(JSON.stringify({ adapter: "mock", count: count }), true);
  });

  el("shopify-btn").addEventListener("click", async () => {
    await startImport(JSON.stringify({ adapter: "shopify" }), true);
  });

  async function startImport(body, isJson) {
    el("import-error").hidden = true;
    el("import-status").hidden = true;
    try {
      const result = await api("/admin/imports", {
        method: "POST",
        headers: headers(isJson),
        body: body,
      });
      el("import-status").hidden = false;
      el("import-summary").textContent = "Import " + result.import_id + " — status: " + result.status;
      if (result.validation_errors && result.validation_errors.length) {
        renderValidation(result.validation_errors);
      }
      pollImport(result.import_id);
    } catch (err) {
      el("import-error").textContent = err.message;
      el("import-error").hidden = false;
    }
  }

  function pollImport(importId) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      try {
        const status = await api("/admin/imports/" + importId, { headers: headers(false) });
        el("import-summary").textContent = "Import " + importId + " — " + status.status +
          " (" + status.progress_pct + "%)";
        el("import-counts").innerHTML =
          "<tr><th>total</th><th>imported</th><th>skipped</th><th>failed</th></tr>" +
          "<tr><td>" + status.counts.total + "</td><td>" + status.counts.imported + "</td><td>" +
          status.counts.skipped + "</td><td>" + status.counts.failed + "</td></tr>" +
          "<tr><td colspan='4'>re-embedded: " + status.reembedded +
          " · unchanged (embed reused): " + status.skipped_unchanged +
          (status.coverage_pct != null ? " · coverage: " + status.coverage_pct + "%" : "") + "</td></tr>";
        renderValidation(status.validation_errors || []);
        if (status.status === "done" || status.status === "failed") { loadHygiene(); return; }
        pollImport(importId);
      } catch (err) {
        el("import-error").textContent = err.message;
        el("import-error").hidden = false;
      }
    }, 1000);
  }

  function renderValidation(errors) {
    el("validation-details").hidden = errors.length === 0;
    el("validation-count").textContent = errors.length;
    el("validation-table").innerHTML = errors.slice(0, 100).map((e) =>
      "<tr><td>row " + e.row + "</td><td>" + (e.sku || "—") + "</td><td>" + escapeHtml(e.reason || "") + "</td></tr>"
    ).join("");
  }

  // ---- hygiene ----------------------------------------------------------------
  el("hygiene-btn").addEventListener("click", loadHygiene);

  async function loadHygiene() {
    try {
      const report = await api("/admin/hygiene", { headers: headers(false) });
      el("hygiene-summary").textContent = "Coverage " + report.coverage_pct + "% (" + report.indexed +
        " of " + report.active + " active SKUs indexed) · generated " + report.generated_at;
      const container = el("hygiene-findings");
      container.innerHTML = "";
      Object.entries(report.findings).forEach(([type, items]) => {
        const details = document.createElement("details");
        details.open = items.length > 0 && type !== "discontinued";
        const summary = document.createElement("summary");
        summary.textContent = type + " (" + items.length + ")";
        details.appendChild(summary);
        const table = document.createElement("table");
        table.innerHTML = "<tr><th>SKU</th><th>Product URL</th><th>Detail</th></tr>" +
          items.slice(0, 100).map((f) =>
            "<tr><td>" + f.sku + "</td><td>" + (f.product_url || "") + "</td><td>" +
            escapeHtml(JSON.stringify(f.detail || {})) + "</td></tr>").join("");
        details.appendChild(table);
        container.appendChild(details);
      });
      el("hygiene-csv").href = API + "/admin/hygiene?format=csv";
    } catch (err) {
      el("hygiene-summary").textContent = err.message;
    }
  }

  // ---- reindex ----------------------------------------------------------------
  el("reindex-btn").addEventListener("click", async () => {
    const sku = el("reindex-sku").value.trim();
    if (!sku) return;
    try {
      const result = await api("/admin/skus/" + encodeURIComponent(sku) + "/reindex", {
        method: "POST", headers: headers(false),
      });
      el("reindex-result").textContent = "queued job " + result.job_id;
    } catch (err) {
      el("reindex-result").textContent = err.message;
    }
  });

  // ---- config -----------------------------------------------------------------
  async function loadConfig() {
    try {
      const data = await api("/admin/config", { headers: headers(false) });
      el("config-json").value = JSON.stringify(data.config, null, 2);
      el("config-result").textContent = "loaded";
    } catch (err) {
      el("config-result").textContent = err.message;
    }
  }
  el("config-reload").addEventListener("click", loadConfig);
  el("config-save").addEventListener("click", async () => {
    try {
      const payload = JSON.parse(el("config-json").value);
      const data = await api("/admin/config", {
        method: "PUT", headers: headers(true), body: JSON.stringify(payload),
      });
      el("config-json").value = JSON.stringify(data.config, null, 2);
      el("config-result").textContent = "saved";
    } catch (err) {
      el("config-result").textContent = err.message;
    }
  });

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  loadHygiene();
  loadConfig();
})();
