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
          <div class="d-flex gap-1">
            <button class="btn btn-sm btn-success">✔</button>
            <button class="btn btn-sm btn-danger">✖</button>
            <button class="btn btn-sm btn-outline-secondary">⏸</button>
          </div>
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
  }

  async function openModal(item) {

    state.selected = item;

    el.modalTitle.textContent = `Part ${item.part_number} (${item.vendor})`;

    const isInsert =
        Number(item.row_inserts) > 0 &&
        Number(item.row_updates) === 0 &&
        Number(item.row_deletes) === 0;

    if (!isInsert) {
        el.modalBody.innerHTML = `
        <div class="alert alert-info">
            Intelligence view available for INSERT only.
        </div>
        `;
        el.modal.show();
        return;
    }
    

    el.modalBody.innerHTML = `<div class="text-center py-4">Loading intelligence...</div>`;
    el.modal.show();

    const res = await fetch(
        `/api/category-review/part-intelligence?vendor=${item.vendor}&part=${item.part_number}`
    );

    const data = await res.json();

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
            <tr><th>Image Completeness</th><td>${data.image_completeness}</td></tr>
            <tr><th>Data Quality Score</th><td>${data.data_quality_score}%</td></tr>
            <tr><th>Missing Attributes</th>
            <td>${(data.missing_attributes && data.missing_attributes.length)
                    ? data.missing_attributes.join(", ")
                    : "None"}</td></tr>
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
    }



  async function loadQueue() {
    try {
      const res = await fetch("/api/category-review/work-queue");
      const data = await res.json();

      state.items = data.items || [];
      state.summary = data.summary || {};

      renderSummary();
      render();

    } catch (err) {
      el.container.innerHTML = `
        <tr>
          <td colspan="7" class="text-danger text-center py-5">
            Failed to load queue.
          </td>
        </tr>`;
      console.error(err);
    }
  }

  loadQueue();

})();
