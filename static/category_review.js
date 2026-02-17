(async function () {

  const el = {
    table: document.getElementById("queueTable"),
    search: document.getElementById("search"),
    filterDecision: document.getElementById("filterDecision"),

    badgeVendors: document.getElementById("badge-vendors"),
    badgeTotal: document.getElementById("badge-total"),
    badgePending: document.getElementById("badge-pending"),
    badgeApproved: document.getElementById("badge-approved"),
    badgeRejected: document.getElementById("badge-rejected"),

    detailCard: document.getElementById("detailCard"),
    detailTitle: document.getElementById("detailTitle"),
    detailMeta: document.getElementById("detailMeta"),
    detailBody: document.getElementById("detailBody"),
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

  function renderTable() {

    const q = (el.search.value || "").toLowerCase();
    const f = el.filterDecision.value;

    const filtered = state.items
      .filter(i =>
        (!q ||
          i.part_number.toLowerCase().includes(q) ||
          i.vendor.toLowerCase().includes(q)
        )
      )
      .filter(i => f === "all" || i.decision === f);

    if (!filtered.length) {
      el.table.innerHTML = `
        <tr>
          <td colspan="7" class="text-center text-muted py-4">
            No items found.
          </td>
        </tr>`;
      return;
    }

    el.table.innerHTML = filtered.map(item => `
      <tr>
        <td>${item.vendor}</td>
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

    renderTable();
    updateCounts();
  };

  function updateCounts() {
    state.summary.pending = state.items.filter(i => i.decision === "pending").length;
    state.summary.approved = state.items.filter(i => i.decision === "approve").length;
    state.summary.rejected = state.items.filter(i => i.decision === "reject").length;
    renderSummary();
  }

  el.search.oninput = renderTable;
  el.filterDecision.onchange = renderTable;

  async function loadQueue() {
    try {
      const res = await fetch("/api/category-review/work-queue");
      const data = await res.json();

      state.items = data.items || [];
      state.summary = data.summary || {};

      renderSummary();
      renderTable();

    } catch (err) {
      el.table.innerHTML = `
        <tr>
          <td colspan="7" class="text-danger text-center py-4">
            Failed to load queue.
          </td>
        </tr>`;
      console.error(err);
    }
  }

  loadQueue();

})();
