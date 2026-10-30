"use strict";

document.addEventListener("DOMContentLoaded", async () => {
  const api = window.talentLensApi;
  const loading = document.getElementById("dashboard-loading");
  const empty = document.getElementById("no-organization");
  const content = document.getElementById("dashboard-content");
  let hasWorkInProgress = false;
  const shell = await api.initShell();
  if (!shell) { loading.hidden = true; return; }
  if (shell.error) { loading.hidden = true; return; }
  if (!shell.organizationId) {
    loading.hidden = true;
    empty.hidden = false;
    return;
  }

  async function loadDashboard() {
    api.hideNotice();
    loading.hidden = false;
    content.hidden = true;
    empty.hidden = true;
    try {
      const data = await api.request(`/api/v1/organizations/${encodeURIComponent(api.organizationId)}/dashboard`);
      document.getElementById("stat-jobs").textContent = data.active_jobs;
      document.getElementById("stat-candidates").textContent = data.candidates;
      document.getElementById("stat-progress").textContent = data.in_progress;
      document.getElementById("stat-attention").textContent = data.needs_attention;
      renderRecent(data.recent);
      hasWorkInProgress = data.in_progress > 0;
      content.hidden = false;
    } catch (error) {
      api.showNotice(error.message || "Your dashboard could not be loaded. Retry in a moment.");
    } finally { loading.hidden = true; }
  }

  function renderRecent(records) {
    const list = document.getElementById("recent-list");
    list.replaceChildren();
    if (!records.length) {
      const state = document.createElement("div");
      state.className = "empty-inline";
      const heading = document.createElement("h3");
      heading.textContent = "No screenings yet";
      const message = document.createElement("p");
      message.textContent = "Create a role and upload a resume to start a review queue.";
      state.append(heading, message);
      list.append(state);
      return;
    }
    for (const record of records) {
      const row = document.createElement("div");
      row.className = "recent-row";
      const description = document.createElement("div");
      const candidate = document.createElement("a");
      candidate.className = "recent-candidate";
      candidate.href = `/candidate?organization=${encodeURIComponent(api.organizationId)}&id=${encodeURIComponent(record.id)}`;
      candidate.textContent = record.candidate_name || "Candidate profile";
      const role = document.createElement("div");
      role.className = "recent-role";
      role.textContent = record.job_title || "Untitled role";
      description.append(candidate, role);
      const date = document.createElement("time");
      date.className = "recent-date";
      date.dateTime = record.created_at;
      date.textContent = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(record.created_at));
      const status = document.createElement("span");
      status.className = `status-pill status-${record.status}`;
      status.textContent = record.status.replaceAll("_", " ");
      row.append(description, date, status);
      list.append(row);
    }
  }

  document.getElementById("organization-select")?.addEventListener("change", loadDashboard);
  await loadDashboard();
  window.setInterval(() => {
    if (!document.hidden && hasWorkInProgress) loadDashboard();
  }, 10000);
});
