async function loadAdminData() {
    const response = await fetch("/api/admin/summary");
    const data = await response.json();

    console.log("Fetched data:", data);

    if (!Array.isArray(data)) {
        console.error("API did not return array:", data);
        return;
    }

    buildKPIs(data);
    buildTable(data);
    buildChart(data);
}

function buildKPIs(data) {
    const totalVendors = data.length;
    const avgScore = avg(data.map(d => d.overall_score));
    const avgCompleteness = avg(data.map(d => d.completeness_pct));
    const totalErrors = sum(data.map(d => d.error_count));
    const totalDeltas = sum(data.map(d => d.delta_count));

    const kpis = [
        { label: "Vendors", value: totalVendors },
        { label: "Avg Score", value: avgScore.toFixed(1) + "%" },
        { label: "Avg Completeness", value: avgCompleteness.toFixed(1) + "%" },
        { label: "Total Errors", value: totalErrors },
        { label: "Total Deltas", value: totalDeltas }
    ];

    const container = document.getElementById("kpiStrip");
    container.innerHTML = "";

    kpis.forEach(kpi => {
        container.innerHTML += `
        <div class="col-md">
            <div class="card text-center shadow-sm">
                <div class="card-body">
                    <h6>${kpi.label}</h6>
                    <h3>${kpi.value}</h3>
                </div>
            </div>
        </div>`;
    });
}

function buildTable(data) {
    console.log("Building table with:", data);
    const tbody = document.querySelector("#vendorTable tbody");
    tbody.innerHTML = "";

    data.forEach(v => {
        const scoreBadge = getScoreBadge(v.overall_score);

        tbody.innerHTML += `
            <tr>
                <td>${v.vendor}</td>
                <td>${scoreBadge}</td>
                <td>${v.completeness_pct || "-"}%</td>
                <td>${v.autofix_success_pct || "-"}%</td>
                <td>${v.error_count ?? "-"}</td>
                <td>${v.delta_count ?? "-"}</td>
                <td>${new Date(v.last_updated_utc).toLocaleString()}</td>
            </tr>
        `;
    });
}

function buildChart(data) {
    const ctx = document.getElementById("scoreChart").getContext("2d");

    new Chart(ctx, {
        type: "bar",
        data: {
            labels: data.map(d => d.vendor),
            datasets: [{
                label: "Overall Score",
                data: data.map(d => d.overall_score),
            }]
        }
    });
}

function getScoreBadge(score) {
    if (score >= 85)
        return `<span class="badge bg-success">${score}%</span>`;
    if (score >= 70)
        return `<span class="badge bg-warning text-dark">${score}%</span>`;
    return `<span class="badge bg-danger">${score}%</span>`;
}

function avg(arr) {
    return arr.reduce((a, b) => a + (b || 0), 0) / arr.length;
}

function sum(arr) {
    return arr.reduce((a, b) => a + (b || 0), 0);
}

document.addEventListener("DOMContentLoaded", function () {
    loadAdminData();
});