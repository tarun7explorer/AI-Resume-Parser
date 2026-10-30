"use strict";

document.addEventListener("DOMContentLoaded", async () => {
  const api = window.talentLensApi;
  const shell = await api.initShell();
  if (!shell || shell.error || !shell.organizationId) return;
  const org = () => encodeURIComponent(api.organizationId);
  const $ = (id) => document.getElementById(id);
  let jobs = [];
  let historyRows = [];
  let historyOffset = 0;
  let historyHasMore = false;
  let historySequence = 0;
  const selectedForComparison = new Set();
  const membershipForSelectedOrg = () => shell.organizations?.find((item) => item.organization_id === api.organizationId);
  const canRetry = () => ["owner", "admin", "recruiter"].includes(membershipForSelectedOrg()?.role);

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function skills(value) {
    return value.split(",").map((part) => part.trim()).filter(Boolean);
  }

  function setBusy(button, busy, busyText, normalText) {
    button.disabled = busy;
    button.textContent = busy ? busyText : normalText;
  }

  function renderJobs() {
    const list = $("jobs-list");
    const active = jobs.filter((job) => job.status === "active");
    $("job-count").textContent = active.length;
    const options = (select, placeholder) => {
      select.replaceChildren();
      const first = element("option", placeholder);
      first.value = "";
      select.append(first);
      for (const job of active) {
        const option = element("option", job.title);
        option.value = job.id;
        select.append(option);
      }
    };
    options($("upload-job"), "Choose an active job");
    options($("history-job"), "All jobs");
    list.replaceChildren();
    if (!active.length) {
      list.append(element("p", "No active jobs yet. Create a role to begin.", "muted small"));
      return;
    }
    for (const job of active) {
      const row = element("div", null, "job-card");
      row.setAttribute("role", "listitem");
      const info = document.createElement("div");
      info.append(element("h3", job.title), element("p", `${job.required_skills.length} required skills · ${new Date(job.created_at).toLocaleDateString()}`));
      const actions = element("div", null, "job-card-actions");
      const edit = element("button", "Edit", "button button-secondary");
      edit.type = "button";
      edit.addEventListener("click", () => editJob(job).catch((error) => api.showNotice(error.message || "The job could not be opened.")));
      const review = element("a", "History", "button button-quiet");
      review.href = `#history?job=${encodeURIComponent(job.id)}`;
      review.addEventListener("click", () => {
        $("history-job").value = job.id;
        loadHistory();
      });
      actions.append(edit, review);
      row.append(info, actions);
      list.append(row);
    }
  }

  async function loadJobs() {
    const response = await api.request(`/api/v1/organizations/${org()}/jobs`);
    jobs = response;
    renderJobs();
  }

  async function editJob(summary) {
    const job = await api.request(`/api/v1/organizations/${org()}/jobs/${encodeURIComponent(summary.id)}`);
    $("editing-job-id").value = job.id;
    $("job-title").value = job.title;
    $("job-description").value = job.description;
    $("required-skills").value = job.required_skills.join(", ");
    $("preferred-skills").value = job.preferred_skills.join(", ");
    $("experience-years").value = job.minimum_experience_years ?? "";
    $("qualifications").value = job.qualifications || "";
    $("responsibilities").value = job.responsibilities || "";
    $("job-form-heading").textContent = "Edit job details";
    $("save-job").textContent = "Save changes";
    $("cancel-edit").hidden = false;
    $("new-job").scrollIntoView({ behavior: "smooth", block: "start" });
    $("job-title").focus({ preventScroll: true });
  }

  function clearJobForm() {
    $("job-form").reset();
    $("editing-job-id").value = "";
    $("job-form-heading").textContent = "Create a job";
    $("save-job").textContent = "Save job";
    $("cancel-edit").hidden = true;
  }

  $("job-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    api.hideNotice();
    const button = $("save-job");
    const editId = $("editing-job-id").value;
    const fields = {
      title: $("job-title").value.trim(),
      description: $("job-description").value.trim(),
      required_skills: skills($("required-skills").value),
      preferred_skills: skills($("preferred-skills").value),
      minimum_experience_years: $("experience-years").value === "" ? null : Number($("experience-years").value),
      qualifications: $("qualifications").value.trim(),
      responsibilities: $("responsibilities").value.trim(),
    };
    try {
      setBusy(button, true, "Saving…", editId ? "Save changes" : "Save job");
      const url = `/api/v1/organizations/${org()}/jobs${editId ? `/${encodeURIComponent(editId)}` : ""}`;
      await api.request(url, {
        method: editId ? "PATCH" : "POST",
        headers: editId ? {} : { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(fields),
      });
      clearJobForm();
      await loadJobs();
      api.showNotice(editId ? "Job details updated." : "Job created. You can now add resumes.", "success");
      if (!editId) $("upload-job").value = jobs.find((job) => job.title === fields.title)?.id || "";
    } catch (error) { api.showNotice(error.message || "The job could not be saved."); }
    finally { setBusy(button, false, "Saving…", $("editing-job-id").value ? "Save changes" : "Save job"); }
  });

  $("cancel-edit").addEventListener("click", clearJobForm);

  $("job-pdf").addEventListener("change", () => {
    const file = $("job-pdf").files[0];
    $("job-pdf-name").textContent = file ? file.name : "No PDF selected";
  });

  $("extract-job-pdf").addEventListener("click", async () => {
    const file = $("job-pdf").files[0];
    if (!file) { api.showNotice("Choose a job-description PDF first."); return; }
    if (file.size > 10 * 1024 * 1024 || file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
      api.showNotice("Choose a PDF smaller than 10 MiB."); return;
    }
    const button = $("extract-job-pdf");
    setBusy(button, true, "Extracting…", "Extract text");
    try {
      const body = new FormData();
      body.append("file", file);
      const extracted = await api.request(`/api/v1/organizations/${org()}/jobs/parse-description`, { method: "POST", body });
      $("job-description").value = extracted.text;
      api.showNotice("Job description extracted. Review the text, then save the role.", "success");
    } catch (error) { api.showNotice(error.message || "Text extraction failed. You can paste the description instead."); }
    finally { setBusy(button, false, "Extracting…", "Extract text"); }
  });

  function selectedFiles() {
    const files = [...$("resume-files").files];
    $("selected-files").textContent = files.length ? `${files.length} selected · ${(files.reduce((sum, file) => sum + file.size, 0) / (1024 * 1024)).toFixed(1)} MiB total` : "";
  }
  $("resume-files").addEventListener("change", selectedFiles);

  $("resume-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const files = [...$("resume-files").files];
    const jobId = $("upload-job").value;
    if (!jobId) { api.showNotice("Choose a job before uploading resumes."); return; }
    if (!files.length) { api.showNotice("Choose at least one resume PDF."); return; }
    if (files.length > 20) { api.showNotice("Upload no more than 20 resumes at a time."); return; }
    const button = $("upload-resumes");
    const results = $("upload-results");
    results.replaceChildren();
    button.disabled = true;
    async function uploadOne(file, status, row, idempotencyKey) {
      status.textContent = "Sending";
      status.className = "";
      row.classList.remove("failed");
      const retry = row.querySelector("button");
      if (retry) retry.remove();
      try {
        const form = new FormData();
        form.append("file", file);
        const created = await api.request(`/api/v1/organizations/${org()}/jobs/${encodeURIComponent(jobId)}/screenings`, {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
          body: form,
        });
        status.textContent = `Queued · ${created.status}`;
        status.className = "success-text";
      } catch (error) {
        row.classList.add("failed");
        status.textContent = error.message || "Upload failed";
        status.className = "error-text";
        const retryButton = element("button", "Retry", "button button-secondary retry-button");
        retryButton.type = "button";
        retryButton.addEventListener("click", async () => {
          await uploadOne(file, status, row, idempotencyKey);
          await loadHistory(true);
        });
        row.append(retryButton);
      }
    }

    const requestSignature = `${jobId}|${files.map((file) => `${file.name}:${file.size}:${file.lastModified}`).join("|")}`;
    const tokenStorageKey = `talentlens.upload.${requestSignature}`;
    let idempotencyKeys = [];
    try { idempotencyKeys = JSON.parse(sessionStorage.getItem(tokenStorageKey) || "[]"); } catch { idempotencyKeys = []; }
    if (!Array.isArray(idempotencyKeys) || idempotencyKeys.length !== files.length) {
      idempotencyKeys = files.map(() => crypto.randomUUID());
      try { sessionStorage.setItem(tokenStorageKey, JSON.stringify(idempotencyKeys)); } catch { /* current page retains keys */ }
    }
    let anyFailed = false;
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      const row = element("div", null, "upload-result");
      const name = element("span", file.name);
      const status = element("strong", "Sending");
      row.append(name, status);
      results.append(row);
      const invalidReason = index >= 20
        ? "Batch limit is 20 files."
        : file.size === 0 || file.size > 10 * 1024 * 1024
          ? "File must be between 1 byte and 10 MiB."
          : !file.name.toLowerCase().endsWith(".pdf") || file.type && file.type !== "application/pdf"
            ? "Choose a PDF file."
            : null;
      if (invalidReason) {
        row.classList.add("failed");
        status.textContent = invalidReason;
        status.className = "error-text";
        anyFailed = true;
        continue;
      }
      button.textContent = `Sending ${index + 1} of ${Math.min(files.length, 20)}…`;
      await uploadOne(file, status, row, idempotencyKeys[index]);
      if (row.classList.contains("failed")) anyFailed = true;
    }
    button.disabled = false;
    button.textContent = "Start screening";
    $("resume-form").reset();
    selectedFiles();
    anyFailed = anyFailed || [...results.querySelectorAll(".failed")].length > 0;
    if (!anyFailed) { try { sessionStorage.removeItem(tokenStorageKey); } catch { /* optional */ } }
    api.showNotice(anyFailed ? "Some files could not be added. Review each file status and retry failed files." : "Resume files are safely stored and screening records were queued.", anyFailed ? "info" : "success");
    await loadHistory(true);
  });

  async function loadHistory(reset = false) {
    if (reset) historyOffset = 0;
    const requestId = ++historySequence;
    const body = $("history-rows");
    const loading = $("history-loading");
    const empty = $("history-empty");
    loading.hidden = false;
    empty.hidden = true;
    body.replaceChildren();
    try {
      const query = new URLSearchParams({ limit: "50", offset: String(historyOffset) });
      if ($("history-job").value) query.set("job_id", $("history-job").value);
      if ($("history-status").value) query.set("status", $("history-status").value);
      if ($("history-after").value) query.set("created_after", `${$("history-after").value}T00:00:00Z`);
      if ($("history-before").value) query.set("created_before", `${$("history-before").value}T23:59:59Z`);
      if ($("history-search").value.trim()) query.set("search", $("history-search").value.trim());
      const page = await api.request(`/api/v1/organizations/${org()}/screenings?${query}`);
      if (requestId !== historySequence) return;
      historyRows = page.items;
      historyHasMore = page.has_more;
      $("history-previous").disabled = page.offset === 0;
      $("history-next").disabled = !page.has_more;
      $("history-page-label").textContent = `Page ${Math.floor(page.offset / page.limit) + 1}`;
      renderHistory();
    } catch (error) {
      if (requestId === historySequence) api.showNotice(error.message || "Screening history could not be loaded. Try again.");
    } finally {
      if (requestId === historySequence) loading.hidden = true;
    }
  }

  function renderHistory() {
    const body = $("history-rows");
    body.replaceChildren();
    $("history-empty").hidden = historyRows.length > 0 || historyHasMore || historyOffset > 0;
    for (const record of historyRows) {
      const tr = document.createElement("tr");
      tr.className = "screening-row";
      const selectCell = document.createElement("td");
      const compareCheck = document.createElement("input");
      compareCheck.type = "checkbox";
      compareCheck.setAttribute("aria-label", `Select ${record.candidate_name || "candidate"} for comparison`);
      compareCheck.disabled = record.status !== "completed" && !selectedForComparison.has(record.id);
      compareCheck.checked = selectedForComparison.has(record.id);
      compareCheck.addEventListener("change", () => {
        if (compareCheck.checked && selectedForComparison.size < 4) selectedForComparison.add(record.id);
        else selectedForComparison.delete(record.id);
        updateCompareControls();
        renderHistory();
      });
      selectCell.append(compareCheck);
      const candidateCell = document.createElement("td");
      const report = element("a", record.candidate_name || "Candidate profile", "table-primary");
      report.href = `/candidate?organization=${encodeURIComponent(api.organizationId)}&id=${encodeURIComponent(record.id)}`;
      const email = element("span", record.candidate_email || "Resume applicant", "table-secondary");
      candidateCell.append(report, email);
      const job = element("td", record.job_title || "—");
      const statusCell = document.createElement("td");
      const status = element("span", record.status.replaceAll("_", " "), `status-pill status-${record.status}`);
      statusCell.append(status);
      if (record.error_code) {
        const error = element("small", friendlyFailure(record.error_code), "table-secondary");
        statusCell.append(error);
      } else if (record.attempt_count != null && record.status !== "completed") {
        statusCell.append(element("small", `Attempt ${record.attempt_count} of ${record.max_attempts}`, "table-secondary"));
      }
      const score = element("td", record.job_match_score == null ? "Not scored" : `${record.job_match_score.toFixed(1)} / 10`);
      const date = document.createElement("td");
      const time = document.createElement("time");
      time.dateTime = record.created_at;
      time.textContent = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" }).format(new Date(record.created_at));
      date.append(time);
      const action = document.createElement("td");
      if (record.status === "retryable_failure" && canRetry()) {
        const retry = element("button", "Retry now", "button button-secondary retry-button");
        retry.type = "button";
        retry.addEventListener("click", async () => {
          retry.disabled = true;
          try {
            await api.request(`/api/v1/organizations/${org()}/screenings/${encodeURIComponent(record.id)}/retry`, { method: "POST" });
            api.showNotice("Retry queued. The status will update when a worker claims it.", "success");
            await loadHistory();
          } catch (error) { api.showNotice(error.message || "This screening could not be retried."); }
          finally { retry.disabled = false; }
        });
        action.append(retry);
      }
      const link = element("a", "Open", "text-link");
      link.href = report.href;
      link.setAttribute("aria-label", `Open report for ${record.candidate_name || "candidate"}`);
      action.append(link);
      tr.append(selectCell, candidateCell, job, statusCell, score, date, action);
      body.append(tr);
    }
    updateCompareControls();
  }

  function friendlyFailure(code) {
    const messages = {
      invalid_pdf_signature: "The file is not a valid PDF.",
      pdf_parse_failed: "The PDF is corrupt or malformed.",
      pdf_encrypted: "Password-protected PDFs are not supported.",
      tesseract_executable_missing: "OCR is unavailable; contact your workspace administrator.",
      ai_provider_not_configured: "AI processing is not configured; contact your workspace administrator.",
      ai_provider_auth_failed: "AI provider credentials need attention.",
      ai_output_invalid: "The analysis could not be validated.",
      attempts_exhausted: "Automatic attempts were exhausted.",
    };
    return messages[code] || "Processing needs attention.";
  }

  function updateCompareControls() {
    $("compare-count").textContent = `${selectedForComparison.size} selected · choose 2–4 completed reports.`;
    $("compare-selected").disabled = selectedForComparison.size < 2 || selectedForComparison.size > 4;
  }

  $("history-filters").addEventListener("submit", (event) => { event.preventDefault(); loadHistory(true); });
  let searchTimer;
  $("history-search").addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => loadHistory(true), 250);
  });
  $("history-job").addEventListener("change", () => loadHistory(true));
  $("history-status").addEventListener("change", () => loadHistory(true));
  $("history-previous").addEventListener("click", () => { historyOffset = Math.max(0, historyOffset - 50); loadHistory(); });
  $("history-next").addEventListener("click", () => { if (historyHasMore) { historyOffset += 50; loadHistory(); } });
  $("compare-close").addEventListener("click", () => $("candidate-compare").close());
  $("compare-selected").addEventListener("click", async () => {
    const ids = [...selectedForComparison];
    if (ids.length < 2 || ids.length > 4) return;
    const button = $("compare-selected");
    setBusy(button, true, "Loading reports…", "Compare selected");
    const target = $("compare-content");
    target.replaceChildren(element("p", "Loading selected reports…", "muted"));
    try {
      const reports = await Promise.all(ids.map((id) => api.request(`/api/v1/organizations/${org()}/screenings/${encodeURIComponent(id)}`)));
      target.replaceChildren();
      const table = document.createElement("table");
      const head = document.createElement("thead");
      const header = document.createElement("tr");
      header.append(element("th", "Review dimension"));
      for (const report of reports) header.append(element("th", report.candidate_name || "Candidate"));
      head.append(header);
      const body = document.createElement("tbody");
      const values = [
        ["Status", (item) => item.status.replaceAll("_", " ")],
        ["Job match", (item) => item.job_match_score == null ? "Not scored" : `${item.job_match_score} / 10`],
        ["Classification", (item) => item.classification_score == null ? "Not scored" : `${item.classification_score} / 100`],
        ["Strengths", (item) => (item.report?.strengths || []).join(" · ") || "No completed evidence yet"],
        ["Gaps", (item) => (item.report?.gaps || []).join(" · ") || "No completed evidence yet"],
      ];
      for (const [label, value] of values) {
        const row = document.createElement("tr");
        row.append(element("th", label));
        for (const report of reports) row.append(element("td", value(report)));
        body.append(row);
      }
      table.append(head, body);
      target.append(table);
      $("candidate-compare").showModal();
    } catch (error) {
      target.replaceChildren(element("p", error.message || "Selected reports could not be compared.", "error-text"));
      $("candidate-compare").showModal();
    } finally { setBusy(button, false, "Loading reports…", "Compare selected"); }
  });
  $("export-csv").addEventListener("click", async () => {
    try {
      const query = new URLSearchParams();
      if ($("history-job").value) query.set("job_id", $("history-job").value);
      if ($("history-status").value) query.set("status", $("history-status").value);
      if ($("history-after").value) query.set("created_after", `${$("history-after").value}T00:00:00Z`);
      if ($("history-before").value) query.set("created_before", `${$("history-before").value}T23:59:59Z`);
      if ($("history-search").value.trim()) query.set("search", $("history-search").value.trim());
      const response = await api.request(`/api/v1/organizations/${org()}/screenings/export.csv?${query}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "screening-history.csv";
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { api.showNotice(error.message || "The export could not be generated."); }
  });

  try { await loadJobs(); await loadHistory(); }
  catch (error) { api.showNotice(error.message || "The screening workspace could not be loaded."); }
  document.getElementById("organization-select")?.addEventListener("change", async () => {
    try { selectedForComparison.clear(); await loadJobs(); await loadHistory(true); }
    catch (error) { api.showNotice(error.message || "The screening workspace could not be loaded."); }
  });
  window.setInterval(() => {
    if (!document.hidden && historyRows.some((row) => ["queued", "processing", "retryable_failure"].includes(row.status))) loadHistory();
  }, 5000);
});
