const HOURS_TARGET = 8;

let sortKey = "member_name";
let sortDir = "asc";

function hoursClass(hours) {
  const h = parseFloat(hours);
  if (h >= HOURS_TARGET) return "ok";
  if (h > 0) return "partial";
  return "zero";
}

function formatSyncedAt(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function updateStats(members) {
  const totalHours = members.reduce((s, m) => s + parseFloat(m.hours_logged || 0), 0);
  const logged = members.filter((m) => parseFloat(m.hours_logged || 0) > 0).length;
  const avg = members.length ? totalHours / members.length : 0;

  document.getElementById("stat-total").textContent = totalHours.toFixed(1);
  document.getElementById("stat-logged").textContent = `${logged} / ${members.length}`;
  document.getElementById("stat-avg").textContent = avg.toFixed(1);
}

function sortMembers(members) {
  return [...members].sort((a, b) => {
    let av = a[sortKey];
    let bv = b[sortKey];
    if (sortKey === "hours_logged" || sortKey === "worklog_count") {
      av = parseFloat(av) || 0;
      bv = parseFloat(bv) || 0;
    } else {
      av = String(av || "").toLowerCase();
      bv = String(bv || "").toLowerCase();
    }
    if (av < bv) return sortDir === "asc" ? -1 : 1;
    if (av > bv) return sortDir === "asc" ? 1 : -1;
    return 0;
  });
}

function renderTable(members) {
  const tbody = document.getElementById("worklog-body");
  const sorted = sortMembers(members);

  if (!sorted.length) {
    tbody.innerHTML =
      '<tr><td colspan="4" class="empty-state">No worklog data yet. Run the sync workflow to populate this page.</td></tr>';
    return;
  }

  tbody.innerHTML = sorted
    .map(
      (m) => `
    <tr>
      <td>${escapeHtml(m.member_name)}</td>
      <td><span class="hours ${hoursClass(m.hours_logged)}">${parseFloat(m.hours_logged || 0).toFixed(2)}</span></td>
      <td>${m.worklog_count || 0}</td>
      <td class="issue-keys">${escapeHtml(m.issue_keys || "—")}</td>
    </tr>`
    )
    .join("");

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.classList.remove("sorted-asc", "sorted-desc");
    if (th.dataset.sort === sortKey) {
      th.classList.add(sortDir === "asc" ? "sorted-asc" : "sorted-desc");
    }
  });
}

function escapeHtml(text) {
  const el = document.createElement("span");
  el.textContent = text;
  return el.innerHTML;
}

function bindSortHandlers(members) {
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (sortKey === key) {
        sortDir = sortDir === "asc" ? "desc" : "asc";
      } else {
        sortKey = key;
        sortDir = key === "hours_logged" ? "desc" : "asc";
      }
      renderTable(members);
    });
  });
}

async function loadData() {
  const body = document.getElementById("worklog-body");
  body.innerHTML = '<tr><td colspan="4" class="empty-state">Loading…</td></tr>';

  try {
    const res = await fetch("data/latest.json?" + Date.now());
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const members = data.members || [];

    document.getElementById("worklog-date").textContent = data.date || "—";
    document.getElementById("synced-at").textContent = formatSyncedAt(data.synced_at);
    document.getElementById("timezone").textContent = data.timezone || "—";

    updateStats(members);
    renderTable(members);
    bindSortHandlers(members);
  } catch (err) {
    body.innerHTML = `<tr><td colspan="4" class="error-state">Failed to load worklog data: ${escapeHtml(err.message)}</td></tr>`;
  }
}

loadData();
