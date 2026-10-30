"use strict";

document.addEventListener("DOMContentLoaded", async () => {
  const api = window.talentLensApi;
  const shell = await api.initShell();
  if (!shell || shell.error) return;
  const params = new URLSearchParams(window.location.search);
  const organizationId = params.get("organization") || api.organizationId;
  const screeningId = params.get("id");
  const loading = document.getElementById("report-loading");
  const content = document.getElementById("report-content");
  let currentReport = null;
  let polling = false;

  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }

  function failureText(code) {
    const messages = {
      invalid_pdf_signature: "The stored file is not a valid PDF. Upload a valid PDF to create a new screening.",
      pdf_parse_failed: "The PDF is corrupt or malformed. Re-export the document and submit it again.",
      pdf_encrypted: "Password-protected PDFs are not supported.",
      tesseract_executable_missing: "OCR is not installed on the worker. Contact your administrator.",
      ai_provider_not_configured: "AI processing is not configured on the worker. Contact your administrator.",
      ai_provider_auth_failed: "The AI provider credential was rejected. Contact your administrator.",
      ai_output_invalid: "The analysis response did not pass validation. Review the source document before retrying.",
      attempts_exhausted: "Automatic retries were exhausted. A manual retry is available if the failure was transient.",
    };
    return messages[code] || "Processing needs attention. The incomplete report is not a verified result.";
  }

  function renderEvidence(report) {
    const target = document.getElementById("evidence-body");
    target.replaceChildren();
    const assessments = report?.profile?.assessments;
    if (!Array.isArray(assessments) || assessments.length === 0) {
      target.append(node("p", "Evidence summaries appear after a screening completes successfully.", "muted"));
      return;
    }
    for (const item of assessments) {
      const card = node("article", null, "evidence-item");
      card.append(node("strong", item.requirement || "Requirement"));
      card.append(node("span", ` · ${String(item.status || "uncertain").replaceAll("_", " ")}`));
      card.append(node("p", item.explanation || "No explanation supplied."));
      for (const span of item.evidence || []) {
        card.append(node("blockquote", `“${span.quote}”`));
        card.append(node("small", `Page ${span.page}`, "muted"));
      }
      target.append(card);
    }
  }

  function renderReport(report) {
    currentReport = report;
    document.getElementById("candidate-name").textContent = report.candidate_name || "Candidate profile";
    document.getElementById("candidate-meta").textContent = report.candidate_email || report.original_filename || "Resume applicant";
    document.getElementById("report-job").textContent = report.job_title || "Untitled role";
    document.getElementById("report-created").textContent = `Added ${new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(report.created_at))}`;
    const status = document.getElementById("report-status");
    status.textContent = report.status.replaceAll("_", " ");
    status.className = `status-pill status-${report.status}`;
    const explanation = report.status === "completed"
      ? "Analysis is complete. Review each assessment with its cited resume page. Scores are provisional review aids."
      : report.status === "queued"
        ? "Queued. A worker will claim this screening; this page refreshes its status automatically."
        : report.status === "processing"
          ? `Processing attempt ${report.attempt_count || 1} of ${report.max_attempts || 3}. No percentage estimate is available.`
          : report.status === "retryable_failure"
            ? `${failureText(report.error_code)} Attempt ${report.attempt_count || 0} of ${report.max_attempts || 3}. You can request an immediate bounded retry.`
            : failureText(report.error_code);
    document.getElementById("status-explanation").textContent = explanation;
    document.getElementById("match-score").textContent = report.job_match_score == null ? "—" : Number(report.job_match_score).toFixed(1);
    document.getElementById("classification-score").textContent = report.classification_score == null ? "—" : Number(report.classification_score).toFixed(0);
    const reportBody = document.getElementById("report-body");
    reportBody.replaceChildren();
    if (report.report && typeof report.report.summary === "string") {
      reportBody.append(node("p", report.report.summary, "report-summary"));
      reportBody.append(node("p", report.report.classification_band?.replaceAll("_", " ") || "Provisional classification", "muted"));
      if (Array.isArray(report.report.strengths) && report.report.strengths.length) {
        reportBody.append(node("h3", "Strengths"));
        const strengths = document.createElement("ul");
        for (const item of report.report.strengths) strengths.append(node("li", item));
        reportBody.append(strengths);
      }
      if (Array.isArray(report.report.gaps) && report.report.gaps.length) {
        reportBody.append(node("h3", "Gaps"));
        const gaps = document.createElement("ul");
        for (const item of report.report.gaps) gaps.append(node("li", item));
        reportBody.append(gaps);
      }
      if (Array.isArray(report.report.missing_or_uncertain) && report.report.missing_or_uncertain.length) {
        reportBody.append(node("h3", "Missing or uncertain information"));
        const details = document.createElement("ul");
        for (const item of report.report.missing_or_uncertain) details.append(node("li", item));
        reportBody.append(details);
      }
    } else {
      reportBody.append(node("div", null, "pending-analysis"));
      reportBody.firstElementChild.append(node("h3", report.status === "failed" ? "Analysis did not complete" : "Analysis is not available yet"));
      reportBody.firstElementChild.append(node("p", report.error_code ? failureText(report.error_code) : "No score is shown until the worker completes and validates the report."));
    }
    renderEvidence(report.report);
    const membership = shell.organizations?.find((item) => item.organization_id === organizationId);
    const retry = document.getElementById("retry-screening");
    retry.hidden = report.status !== "retryable_failure" || !membership || !["owner", "admin", "recruiter"].includes(membership.role);
    const download = document.getElementById("download-resume");
    download.hidden = report.status === "failed" && report.error_code === "resume_deleted";
    document.getElementById("delete-resume").hidden = !membership || !["owner", "admin"].includes(membership.role);
    content.hidden = false;
    loading.hidden = true;
  }

  async function loadReport() {
    if (!organizationId || !screeningId) {
      loading.hidden = true;
      api.showNotice("This report link is incomplete. Return to screening history and open a candidate report.");
      return;
    }
    if (polling) return;
    polling = true;
    try {
      const report = await api.request(`/api/v1/organizations/${encodeURIComponent(organizationId)}/screenings/${encodeURIComponent(screeningId)}`);
      renderReport(report);
    } catch (error) {
      loading.hidden = true;
      api.showNotice(error.message || "The candidate report could not be loaded.");
    } finally { polling = false; }
  }

  document.getElementById("download-resume").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const signed = await api.request(`/api/v1/organizations/${encodeURIComponent(organizationId)}/resumes/${encodeURIComponent(currentReport.resume_document_id)}/signed-url`);
      window.location.assign(signed.signed_url);
    } catch (error) { api.showNotice(error.message || "The resume could not be opened."); }
    finally { button.disabled = false; }
  });

  document.getElementById("retry-screening").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await api.request(`/api/v1/organizations/${encodeURIComponent(organizationId)}/screenings/${encodeURIComponent(screeningId)}/retry`, { method: "POST" });
      api.showNotice("Retry queued. Status will update when a worker claims it.", "success");
      await loadReport();
    } catch (error) { api.showNotice(error.message || "This screening is not eligible for retry."); }
    finally { button.disabled = false; }
  });

  document.getElementById("delete-resume").addEventListener("click", async (event) => {
    if (!currentReport || !window.confirm("Delete this resume and its screening report? The private file will be removed by the cleanup worker.")) return;
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api.request(`/api/v1/organizations/${encodeURIComponent(organizationId)}/resumes/${encodeURIComponent(currentReport.resume_document_id)}`, { method: "DELETE" });
      api.showNotice(result.cleanup_status === "failed"
        ? "Resume access was removed, but private file cleanup needs administrator attention."
        : result.cleanup_status === "completed"
          ? "Resume and private file deleted."
          : "Resume access was removed. Private file cleanup is queued.", result.cleanup_status === "failed" ? "error" : "success");
      window.setTimeout(() => window.location.replace("/screening#history"), 700);
    } catch (error) { api.showNotice(error.message || "The resume could not be deleted."); }
    finally { button.disabled = false; }
  });

  document.getElementById("organization-select")?.addEventListener("change", () => {
    window.location.replace(`/screening#history`);
  });
  await loadReport();
  window.setInterval(() => {
    if (!document.hidden && currentReport && ["queued", "processing", "retryable_failure"].includes(currentReport.status)) loadReport();
  }, 5000);
});
