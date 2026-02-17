(async function () {

  const el = {
    container: document.getElementById("queueTable").parentElement.parentElement.parentElement,
    search: document.getElementById("search"),
    filterDecision: document.getElementById("filterDecision"),

    badgeVendors: document.getElementById("badge-vendors"),
    badgeTotal: document.getElementById("badge-total"),
    badgePending: document.getElementById("badge-pending"),
    badgeApproved: document.getElementById("badge-approved"),
    badgeRejected: document.getElementById("badge-rejected"),
  };

  let state = {
    items: [],
    summary: {}
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

  function groupByVendor(items) {
    return items.reduce((acc, item) => {
      if (!acc[item.vendor]) acc[item.vendor] = [];
      acc[item.vendor].push(item);
      return acc;
    }, {});
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
        <div class="text-center text-muted py-5">
          No items found.
        </div>`;
      return;
    }

    const grouped = groupByVendor(filtered);

    el.container.innerHTML = Object.entries(grouped).map(([vendor, items]) => {

      const rows = items.map(item => `
        <tr>
          <td class="fw-semibold">${item.part_number}</td>
          <td class="text-center">${item.row_inserts}</td>
          <td class="text-center">${item.row_updates}</td>
          <td class="text-center">${item.row_deletes}</td>
          <td>${decisionBadge(item.decision)}</td>
          <td>
            <div class="d-flex gap-1">
              <button class="btn btn-sm btn-success"
                onclick="updateDecision('${item.vendor}','${item.part_number}','approve')">✔</button>
              <button class="btn btn-sm btn-danger"
                onclick="updateDecision('${item.vendor}','${item.part_number}','reject')">✖</button>
              <button class="btn btn-sm btn-outline-secondary"
                onclick="updateDecision('${item.vendor}','${item.part_number}','pending')">⏸</button>
            </div>
          </td>
        </tr>
      `).join("");

      return `
        <div class="card shadow-sm mb-4">
          <div class="card-header bg-white d-flex justify-content-between align-items-center">
            <div class="fw-semibold">📦 ${vendor}</div>
            <div class="small text-muted">${items.length} part(s)</div>
          </div>
          <div class="table-responsive">
            <table class="table table-hover align-middle mb-0">
              <thead class="table-light">
                <tr>
                  <th>Part Number</th>
                  <th class="text-center">Insert</th>
                  <th class="text-center">Update</th>
                  <th class="text-center">Delete</th>
                  <th>Status</th>
                  <th style="width:180px;">Actions</th>
                </tr>
              </thead>
              <tbody>
                ${rows}
              </tbody>
            </table>
          </div>
        </div>
      `;
    }).join("");
  }

  window.updateDecision = async function(vendor, part, decision) {

    await fetch("/api/category-review/decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        vendor: vendor,
        part_number: part,
        decision: decision
      })
    });

    const item = state.items.find(i =>
      i.vendor === vendor && i.part_number === part
    );

    if (item) item.decision = decision;

    state.summary.pending = state.items.filter(i => i.decision === "pending").length;
    state.summary.approved = state.items.filter(i => i.decision === "approve").length;
    state.summary.rejected = state.items.filter(i => i.decision === "reject").length;

    renderSummary();
    render();
  };

  el.search.oninput = render;
  el.filterDecision.onchange = render;

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
        <div class="text-danger text-center py-5">
          Failed to load queue.
        </div>`;
      console.error(err);
    }
  }

  loadQueue();

})();
