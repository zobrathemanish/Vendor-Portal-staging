(async function () {

  const el = {
    container: document.getElementById("queueTable"),
    search: document.getElementById("search"),
    filterDecision: document.getElementById("filterDecision"),

    badgeVendors: document.getElementById("badge-vendors"),
    badgeTotal: document.getElementById("badge-total"),
    badgePending: document.getElementById("badge-pending"),
    badgeApproved: document.getElementById("badge-approved"),
    badgeRejected: document.getElementById("badge-rejected"),

    modal: new bootstrap.Modal(document.getElementById("partModal")),
    modalTitle: document.getElementById("modalTitle"),
    modalBody: document.getElementById("modalBody"),
    modalApprove: document.getElementById("modalApprove"),
    modalReject: document.getElementById("modalReject"),
    modalHold: document.getElementById("modalHold"),
  };

  let state = {
    items: [],
    summary: {},
    selected: null
  };

  // =====================================================
  // MODAL DECISION BUTTONS
  // =====================================================

  el.modalApprove.onclick = async function () {
    if (!state.selected) return;

    await sendDecision(
      state.selected.vendor,
      state.selected.part_number,
      "approve"
    );

    el.modal.hide();
  };

  el.modalReject.onclick = async function () {
    if (!state.selected) return;

    await sendDecision(
      state.selected.vendor,
      state.selected.part_number,
      "reject"
    );

    el.modal.hide();
  };

  el.modalHold.onclick = async function () {
    if (!state.selected) return;

    await sendDecision(
      state.selected.vendor,
      state.selected.part_number,
      "pending"
    );

    el.modal.hide();
  };

  async function handleModalDecision(decision) {
    if (!state.selected) return;

    el.modalApprove.disabled = true;
    el.modalReject.disabled = true;
    el.modalHold.disabled = true;

    await sendDecision(
      state.selected.vendor,
      state.selected.part_number,
      decision
    );

    el.modal.hide();

    el.modalApprove.disabled = false;
    el.modalReject.disabled = false;
    el.modalHold.disabled = false;
  }

  el.modalApprove.onclick = () => handleModalDecision("approve");
  el.modalReject.onclick = () => handleModalDecision("reject");
  el.modalHold.onclick = () => handleModalDecision("pending");

  function decisionBadge(decision) {
    if (decision === "approve") return `<span class="badge text-bg-success">Approved</span>`;
    if (decision === "reject") return `<span class="badge text-bg-danger">Rejected</span>`;
    return `<span class="badge text-bg-secondary">Pending</span>`;
  }

  function renderSummary() {
    el.badgeVendors.textContent = `vendors: ${state.summary.vendors || 0}`;
    el.badgeTotal.textContent = `parts: ${state.summary.parts_total || 0}`;
    el.badgePending.textContent = `pending: ${state.summary.pending || 0}`;
    el.badgeApproved.textContent = `approved: ${state.summary.approved || 0}`;
    el.badgeRejected.textContent = `rejected: ${state.summary.rejected || 0}`;
  }

  function render() {

    const q = (el.search.value || "").toLowerCase();
    const f = el.filterDecision.value;

    let filtered = state.items
      .filter(i =>
        (!q ||
          i.part_number.toLowerCase().includes(q) ||
          i.vendor.toLowerCase().includes(q)
        )
      )
      .filter(i => f === "all" || i.decision === f);

    if (!filtered.length) {
      el.container.innerHTML = `
        <tr>
          <td colspan="7" class="text-center text-muted py-5">
            No items found.
          </td>
        </tr>`;
      return;
    }

    el.container.innerHTML = filtered.map(item => `
      <tr class="queue-row" data-vendor="${item.vendor}" data-part="${item.part_number}">
        <td>${item.vendor}</td>
        <td class="fw-semibold text-primary">${item.part_number}</td>
        <td class="text-center">${item.row_inserts}</td>
        <td class="text-center">${item.row_updates}</td>
        <td class="text-center">${item.row_deletes}</td>
        <td>${decisionBadge(item.decision)}</td>
        <td>
          ${
            (Number(item.row_deletes) > 0 &&
            Number(item.row_inserts) === 0 &&
            Number(item.row_updates) === 0)
            ? `<span class="badge text-bg-warning">Auto Delete</span>`
            : `
              <div class="d-flex gap-1">
                <button class="btn btn-sm btn-success btn-approve"
                        data-vendor="${item.vendor}"
                        data-part="${item.part_number}">✔</button>

                <button class="btn btn-sm btn-danger btn-reject"
                        data-vendor="${item.vendor}"
                        data-part="${item.part_number}">✖</button>

                <button class="btn btn-sm btn-outline-secondary btn-hold"
                        data-vendor="${item.vendor}"
                        data-part="${item.part_number}">⏸</button>
              </div>
            `
          }
        </td>

      </tr>
    `).join("");

    // 🔥 Make entire row clickable
    document.querySelectorAll(".queue-row").forEach(row => {
      row.style.cursor = "pointer";
      row.onclick = function (e) {

        // prevent clicking action buttons triggering modal
        if (e.target.tagName === "BUTTON") return;

        const vendor = this.dataset.vendor;
        const part = this.dataset.part;

        const item = state.items.find(i =>
          i.vendor === vendor && i.part_number === part
        );

        if (item) openModal(item);
      };
    });

        // Approve
    document.querySelectorAll(".btn-approve").forEach(btn => {
      btn.onclick = async function(e) {
        e.stopPropagation();

        await sendDecision(
          this.dataset.vendor,
          this.dataset.part,
          "approve"
        );
      };
    });

    // Reject
    document.querySelectorAll(".btn-reject").forEach(btn => {
      btn.onclick = async function(e) {
        e.stopPropagation();

        await sendDecision(
          this.dataset.vendor,
          this.dataset.part,
          "reject"
        );
      };
    });

    // Hold
    document.querySelectorAll(".btn-hold").forEach(btn => {
      btn.onclick = async function(e) {
        e.stopPropagation();

        await sendDecision(
          this.dataset.vendor,
          this.dataset.part,
          "pending"
        );
      };
    });

      }

  async function openModal(item) {

  state.selected = item;

  el.modalTitle.textContent = `Part ${item.part_number} (${item.vendor})`;

  el.modalBody.innerHTML = `<div class="text-center py-4">Loading intelligence...</div>`;
  el.modal.show();

  try {

    const res = await fetch(
      `/api/category-review/part-intelligence?vendor=${item.vendor}&part=${item.part_number}`
    );

    const data = await res.json();

    // =====================================================
    // UPDATE MODE – Show Field-Level Diff
    // =====================================================
    if (data.mode === "update") {

      if (!data.changes || data.changes.length === 0) {
        el.modalBody.innerHTML = `
          <div class="alert alert-info">
            No attribute-level differences detected.
          </div>
        `;
        return;
      }

      el.modalBody.innerHTML = `
        <table class="table table-sm table-bordered">
          <thead>
            <tr>
              <th>Field</th>
              <th>Before</th>
              <th>After</th>
            </tr>
          </thead>
          <tbody>
            ${data.changes.map(c => `
              <tr>
                <td>${c.field}</td>
                <td class="text-danger">${c.before || "-"}</td>
                <td class="text-success">${c.after || "-"}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
      return;
    }

    // =====================================================
    // INSERT MODE – Existing Intelligence View
    // =====================================================
    el.modalBody.innerHTML = `
      <div class="row">

        <div class="col-md-8">
          <table class="table table-sm table-bordered">
            <tr><th>Brand</th><td>${data.brand || "-"}</td></tr>
            <tr><th>Hazardous?</th><td>${data.hazmat || "-"}</td></tr>
            <tr><th>Category</th><td>${data.category || "-"}</td></tr>
            <tr><th>Product Status</th><td>${data.status || "-"}</td></tr>
            <tr><th>Short Description</th><td>${data.short_description || "-"}</td></tr>
            <tr><th>Country of Origin</th><td>${data.country_of_origin || "-"}</td></tr>
            <tr><th>HSB</th><td>${data.hsb || "-"}</td></tr>
            <tr><th>Image Completeness</th><td>${data.image_completeness || "-"}</td></tr>
            <tr><th>Data Quality Score</th><td>${data.data_quality_score || 0}%</td></tr>
            <tr>
              <th>Missing Attributes</th>
              <td>${
                (data.missing_attributes && data.missing_attributes.length)
                  ? data.missing_attributes.join(", ")
                  : "None"
              }</td>
            </tr>
          </table>
        </div>

        <div class="col-md-4 text-center">
          ${
            data.image_preview_url
              ? `<img src="${data.image_preview_url}" 
                     class="img-fluid rounded shadow-sm"
                     style="max-height:300px;">`
              : `<div class="border rounded p-3 bg-light">
                   No Image Available
                 </div>`
          }
        </div>

      </div>
    `;

  } catch (err) {

    el.modalBody.innerHTML = `
      <div class="alert alert-danger">
        Failed to load intelligence data.
      </div>
    `;
    console.error(err);
  }
}


  async function loadQueue() {
    try {
      const res = await fetch("/api/category-review/work-queue");
      const data = await res.json();

      console.log("📦 Queue data received:", data);

      state.items = data.items || [];
      state.summary = data.summary || {};

      console.log("📊 Updated summary:", state.summary);

      renderSummary();
      render();

    } catch (err) {
      el.container.innerHTML = `
        <tr>
          <td colspan="7" class="text-danger text-center py-5">
            Failed to load queue.
          </td>
        </tr>`;
      console.error("❌ Failed to load queue:", err);
    }
  }

  async function sendDecision(vendor, part, decision) {

    console.log("➡ Sending decision:", {
      vendor,
      part,
      decision
    });

    const res = await fetch("/api/category-review/decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        vendor: vendor,
        part_number: part,
        decision: decision
      })
    });

    const result = await res.json();

    console.log("✅ Backend response:", result);

    await loadQueue();

    console.log("🔄 Queue refreshed");
  }



  loadQueue();

})();
