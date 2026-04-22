(async function () {

  const el = {
    container: document.getElementById("queueTable"),
    search: document.getElementById("search"),
    filterDecision: document.getElementById("filterDecision"),

    publishGoldBtn: document.getElementById("publishGoldBtn"),

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
    if (decision === "auto_delete") return `<span class="badge text-bg-warning">Discontinued</span>`;
    return `<span class="badge text-bg-secondary">Pending</span>`;
  }

  function renderSummary() {
    el.badgeVendors.textContent = `vendors: ${state.summary.vendors || 0}`;
    el.badgeTotal.textContent = `parts: ${state.summary.parts_total || 0}`;
    el.badgePending.textContent = `pending: ${state.summary.pending || 0}`;
    el.badgeApproved.textContent = `approved: ${state.summary.approved || 0}`;
    el.badgeRejected.textContent = `rejected: ${state.summary.rejected || 0}`;
  }

  function togglePublishButton() {
    if (!el.publishGoldBtn) return;

    // 🔥 Count only actionable items
    const actionable = state.items.filter(i =>
      !(Number(i.row_deletes) > 0 &&
        Number(i.row_inserts) === 0 &&
        Number(i.row_updates) === 0)
    );

    const pendingActionable = actionable.filter(i => i.decision === "pending");

    if (pendingActionable.length === 0 && actionable.length > 0) {
      el.publishGoldBtn.style.display = "inline-block";
    } else {
      el.publishGoldBtn.style.display = "none";
    }
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
            ? `<span class="badge text-bg-warning"> No actions needed </span>`
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

      // Detect delete-only row
  const isDeleteOnly =
    Number(item.row_deletes) > 0 &&
    Number(item.row_inserts) === 0 &&
    Number(item.row_updates) === 0;

  // Show / hide modal buttons
  if (isDeleteOnly) {
    el.modalApprove.style.display = "none";
    el.modalReject.style.display = "none";
    el.modalHold.style.display = "none";
  } else {
    el.modalApprove.style.display = "inline-block";
    el.modalReject.style.display = "inline-block";
    el.modalHold.style.display = "inline-block";
  }

  let deltaLabel = "";

  if (Number(item.row_deletes) > 0 &&
      Number(item.row_inserts) === 0 &&
      Number(item.row_updates) === 0) {
    deltaLabel = " No Longer in Vendor Catalogue";
  }
  else if (Number(item.row_inserts) > 0 &&
          Number(item.row_updates) === 0) {
    deltaLabel = "New Item in Vendor Catalogue";
  }
  else if (Number(item.row_updates) > 0) {
    deltaLabel = "Part Attributes Updated in Vendor Catalogue";
  }

  el.modalTitle.textContent =
    `Part ${item.part_number} (${item.vendor}) - ${deltaLabel}`;

  el.modalBody.innerHTML = `<div class="text-center py-4">Loading intelligence...</div>`;
  el.modal.show();

  try {

    const res = await fetch(
      `/api/category-review/part-intelligence?vendor=${item.vendor}&part=${item.part_number}`
    );

    const data = await res.json();

    // =====================================================
    // DELETE MODE
    // =====================================================
if (data.mode === "delete") {

  el.modalBody.innerHTML = `
    <div class="alert alert-warning mb-3">
      ⚠ This product will be marked as Inactive.
    </div>

    <div class="row">

      <div class="col-md-8">
        <table class="table table-sm table-bordered">
          <tr><th>Brand</th><td>${data.brand || "-"}</td></tr>
          <tr><th>Category</th><td>${data.category || "-"}</td></tr>
          <tr><th>Status</th><td>${data.status || "-"}</td></tr>
          <tr><th>Short Description</th><td>${data.short_description || "-"}</td></tr>
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

  return;
}

// =====================================================
// UPDATE MODE – Show Field-Level Diff + Image
// =====================================================

if (data.mode === "update") {

  let diffContent = "";

  if (!data.changes || data.changes.length === 0) {
    diffContent = `
      <div class="alert alert-info">
        No attribute-level differences detected.
      </div>
    `;
  } else {
    diffContent = `
      <table class="table table-sm table-bordered">
        <thead>
          <tr>
            <th>Field</th>
            <th>Before</th>
            <th>After</th>
          </tr>
        </thead>
        <tbody>
          ${data.changes.map(c => {

            let fieldLabel = c.field;

            // 🔥 Extended Info
            if (c.section === "Extended_Info" && c.field === "Extended Info Value") {
              const match = c.context?.match(/Code=(.*?)\]/);
              if (match) {
                fieldLabel = `Extended Info Value (${match[1]})`;
              }
            }

            // 🔥 Descriptions
            if (c.section === "Descriptions" && c.field === "Description Value") {
              const match = c.context?.match(/Code=(.*?)\]/);
              if (match) {
                fieldLabel = `Description (${match[1]})`;
              }
            }

            return `
              <tr>
                <td>${fieldLabel}</td>
                <td class="text-danger">${c.before || "-"}</td>
                <td class="text-success">${c.after || "-"}</td>
              </tr>
            `;

          }).join("")}
        </tbody>
      </table>
    `;
  }

  el.modalBody.innerHTML = `
    <div class="alert alert-warning mb-3">
      ⚠ If disapproved, this product will be marked as <strong>Inactive</strong>.
    </div>

    <div class="row">

      <div class="col-md-8">
        ${diffContent}
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
      togglePublishButton();   // ✅ ADD THIS LINE
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


  // Enable search + filter reactivity
  el.search.addEventListener("input", render);
  el.filterDecision.addEventListener("change", render);
  loadQueue();

  if (el.publishGoldBtn) {
    el.publishGoldBtn.onclick = async function () {

      if (!state.items.length) return;

      // 🔥 Get vendor (since queue is per vendor batch)
      const vendor = state.items[0].vendor;

      const res = await fetch("/api/category-review/publish-gold", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ vendor })
      });

      const data = await res.json();

      alert(data.message || "Gold updated");

      // Refresh UI
      await loadQueue();
    };
  }if (el.publishGoldBtn) {
    el.publishGoldBtn.onclick = async function () {

      if (!state.items.length) return;

      // 🔥 Get vendor (since queue is per vendor batch)
      const vendor = state.items[0].vendor;

      const res = await fetch("/api/category-review/publish-gold", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ vendor })
      });

      const data = await res.json();

      alert(data.message || "Gold updated");

      // Refresh UI
      await loadQueue();
    };
  }

})();
