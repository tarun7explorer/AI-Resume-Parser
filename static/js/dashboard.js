"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const api = window.talentLensApi;
  const form = document.getElementById("demo-form");
  const resumeInput = document.getElementById("resume-files");
  const jobPdfInput = document.getElementById("job-description-pdf");
  const descriptionInput = document.getElementById("job-description");
  const submit = document.getElementById("start-screening");
  const resultsSection = document.getElementById("results-section");
  const resultsList = document.getElementById("screening-results");
  const comparisonOutput = document.getElementById("comparison-output");
  const compareButton = document.getElementById("compare-selected");
  let currentResults = [];

  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }

  function renderFiles() {
    const files = [...resumeInput.files];
    const selected = document.getElementById("selected-files");
    selected.textContent = files.length
      ? `${files.length} PDF${files.length === 1 ? "" : "s"} selected · ${(files.reduce((total, file) => total + file.size, 0) / (1024 * 1024)).toFixed(1)} MiB total`
      : "No resumes selected";
  }

  resumeInput.addEventListener("change", renderFiles);
  jobPdfInput.addEventListener("change", () => {
    const file = jobPdfInput.files[0];
    document.getElementById("job-pdf-name").textContent = file ? file.name : "Or paste the description above";
    if (file) descriptionInput.value = "";
  });
  descriptionInput.addEventListener("input", () => {
    if (descriptionInput.value.trim()) {
      jobPdfInput.value = "";
      document.getElementById("job-pdf-name").textContent = "Or paste the description above";
    }
  });

  function renderReport(result, resultIndex) {
    const card = node("article", null, `demo-result-card${result.status === "failed" ? " failed" : ""}`);
    const heading = node("div", null, "demo-result-heading");
    heading.append(node("h3", result.file_name), node("span", result.status === "completed" ? "Completed" : "Could not analyze", `status-pill status-${result.status}`));
    card.append(heading);
    if (result.status !== "completed" || !result.report) {
      card.append(node("p", result.message, "demo-error-message"));
      return card;
    }

    const report = result.report;
    const selectLabel = node("label", null, "demo-compare-select");
    const selectCandidate = document.createElement("input");
    selectCandidate.type = "checkbox";
    selectCandidate.className = "compare-candidate";
    selectCandidate.dataset.resultIndex = String(resultIndex);
    selectLabel.append(selectCandidate, document.createTextNode(" Select for comparison"));
    card.append(selectLabel);
    const scoreRow = node("div", null, "demo-score-row");
    scoreRow.append(
      node("div", `${report.job_match_score} / 10`, "demo-score"),
      node("div", `${report.classification_score} / 100`, "demo-classification"),
      node("div", report.classification_band.replaceAll("_", " "), "demo-band"),
    );
    card.append(scoreRow, node("p", report.summary, "demo-summary"));

    const skillsSection = node("section", null, "demo-detail-section");
    skillsSection.append(node("h4", "Required skill evidence"));
    const matched = result.matched_skills || [];
    const missing = result.missing_requirements || [];
    skillsSection.append(node("p", `Matched: ${matched.length ? matched.join(", ") : "None found"}`));
    skillsSection.append(node("p", `Not found in extracted text: ${missing.length ? missing.join(", ") : "None"}`));
    card.append(skillsSection);

    const scoreSection = node("section", null, "demo-detail-section");
    scoreSection.append(node("h4", "How the provisional score is calculated"));
    const scoreBreakdown = node("ul");
    for (const category of report.category_breakdown) {
      if (category.applicable_items) {
        scoreBreakdown.append(node("li", `${category.category.replaceAll("_", " ")} (${Math.round(category.weight * 100)}% starting weight): ${category.explanation}`));
      }
    }
    scoreSection.append(scoreBreakdown);
    card.append(scoreSection);

    const assessmentsHeading = node("h4", "Requirement evidence");
    card.append(assessmentsHeading);
    const assessments = node("ul", null, "demo-assessments");
    for (const item of report.profile.assessments) {
      const row = node("li", null, "demo-assessment");
      const label = node("strong", `${item.requirement} · ${item.status}`);
      const explanation = node("span", item.explanation);
      row.append(label, explanation);
      for (const evidence of item.evidence) row.append(node("q", `Page ${evidence.page}: ${evidence.quote}`, "demo-evidence"));
      assessments.append(row);
    }
    card.append(assessments);

    const details = node("div", null, "demo-detail-grid");
    const linkSection = node("section", null, "demo-detail-section");
    linkSection.append(node("h4", "Profile links"));
    const links = node("ul");
    for (const item of report.links) {
      const row = node("li", null);
      row.append(node("strong", `${item.platform}: ${item.status}`));
      if (item.url) {
        const link = node("a", item.url);
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        row.append(document.createTextNode(" · "), link);
      }
      links.append(row);
    }
    linkSection.append(links);

    const integritySection = node("section", null, "demo-detail-section");
    integritySection.append(node("h4", "Document review signals"));
    if (report.integrity_findings.length) {
      const findings = node("ul");
      for (const finding of report.integrity_findings) {
        findings.append(node("li", `Page ${finding.page} · ${finding.signal.replaceAll("_", " ")}: ${finding.explanation} Excerpt: ${finding.excerpt}`));
      }
      integritySection.append(findings);
    } else {
      integritySection.append(node("p", "No layout signals were detected. This does not establish document authenticity."));
    }
    details.append(linkSection, integritySection);
    card.append(details);

    const disclaimer = node("p", result.message, "small muted demo-report-note");
    card.append(disclaimer);
    return card;
  }

  resultsList.addEventListener("change", (event) => {
    if (!event.target.matches(".compare-candidate")) return;
    compareButton.disabled = resultsList.querySelectorAll(".compare-candidate:checked").length < 2;
    comparisonOutput.hidden = true;
    comparisonOutput.replaceChildren();
  });

  compareButton.addEventListener("click", () => {
    const selected = [...resultsList.querySelectorAll(".compare-candidate:checked")]
      .map((input) => currentResults[Number(input.dataset.resultIndex)])
      .filter((result) => result && result.report);
    if (selected.length < 2) return;
    const table = document.createElement("table");
    table.className = "demo-comparison-table";
    const head = document.createElement("thead");
    const headerRow = document.createElement("tr");
    headerRow.append(node("th", "Measure"));
    for (const result of selected) headerRow.append(node("th", result.file_name));
    head.append(headerRow);
    const body = document.createElement("tbody");
    const rows = [
      ["Job match", (result) => `${result.report.job_match_score} / 10`],
      ["Classification score", (result) => `${result.report.classification_score} / 100`],
      ["Classification band", (result) => result.report.classification_band.replaceAll("_", " ")],
      ["Summary", (result) => result.report.summary],
    ];
    for (const [label, value] of rows) {
      const row = document.createElement("tr");
      row.append(node("th", label));
      for (const result of selected) row.append(node("td", value(result)));
      body.append(row);
    }
    table.append(head, body);
    comparisonOutput.replaceChildren(node("h3", "Candidate comparison"), table);
    comparisonOutput.hidden = false;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    api.hideNotice();
    const resumes = [...resumeInput.files];
    const jobPdf = jobPdfInput.files[0];
    const description = descriptionInput.value.trim();
    if (!jobPdf && !description) { api.showNotice("Paste a job description or choose its PDF."); return; }
    if (jobPdf && description) { api.showNotice("Use the pasted job description or its PDF, not both."); return; }
    if (!resumes.length || resumes.length > 5) { api.showNotice("Choose between 1 and 5 PDF resumes."); return; }
    const files = jobPdf ? [...resumes, jobPdf] : resumes;
    if (files.some((file) => !file.name.toLowerCase().endsWith(".pdf") || file.type && file.type !== "application/pdf")) {
      api.showNotice("Every uploaded document must be a PDF."); return;
    }
    if (files.some((file) => file.size > 10 * 1024 * 1024)) { api.showNotice("Each PDF must be 10 MiB or smaller."); return; }
    if (!document.getElementById("privacy-acknowledged").checked) { api.showNotice("Confirm you are authorized to process these resumes."); return; }

    const body = new FormData();
    body.append("job_description", description);
    body.append("privacy_acknowledged", "true");
    if (jobPdf) body.append("job_description_pdf", jobPdf);
    for (const file of resumes) body.append("resumes", file);
    submit.disabled = true;
    submit.textContent = "Analyzing resumes…";
    resultsSection.hidden = true;
    resultsList.replaceChildren();
    try {
      const response = await api.request("/api/v1/demo/screenings", { method: "POST", body });
      currentResults = response.results;
      for (const [index, result] of currentResults.entries()) resultsList.append(renderReport(result, index));
      compareButton.disabled = true;
      comparisonOutput.hidden = true;
      document.getElementById("result-count").textContent = `${response.results.filter((item) => item.status === "completed").length} of ${response.results.length} completed`;
      resultsSection.hidden = false;
      resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      currentResults = [];
      api.showNotice(error.message || "The screening could not be completed. Check the server configuration and retry.");
    } finally {
      submit.disabled = false;
      submit.textContent = "Analyze resumes";
    }
  });
});
