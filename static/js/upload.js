/* Upload form, client-side validation, and polling controller.
 * Client limits mirror .env.example defaults for early feedback only; the backend is authoritative.
 */
(function () {
  "use strict";

  var LIMITS = { maxBytes: 10 * 1024 * 1024, maxResumes: 10, maxJdChars: 20000 };
  var MAX_POLL_MS = 15 * 60 * 1000;
  var MAX_CONSECUTIVE_NET_ERRORS = 4;

  var $ = function (id) { return document.getElementById(id); };
  var form = $("screening-form"), analyzeBtn = $("analyze-btn"), analyzeLabel = $("analyze-label"),
      analyzeHelp = $("analyze-help"), spinner = analyzeBtn.querySelector(".spinner");
  var jdText = $("jd-text"), jdTextError = $("jd-text-error"), jdCount = $("jd-text-count");
  var jdTextPanel = $("jd-text-panel"), jdPdfPanel = $("jd-pdf-panel");
  var jdInput = $("jd-file-input"), jdList = $("jd-file-list"), jdPdfError = $("jd-pdf-error");
  var resumeInput = $("resume-file-input"), resumeList = $("resume-list"), resumeEmpty = $("resume-empty"),
      resumeCount = $("resume-count"), resumeClear = $("resume-clear"), resumeError = $("resume-error");
  var errorPanel = $("error-panel"), errorTitle = $("error-title"), errorMessage = $("error-message");
  var statusPanel = $("status-panel"), statusText = $("status-text"), statusMeta = $("status-meta");
  var resultsPanel = $("results-panel"), resultsContainer = $("results-container"), resultsTitle = $("results-title");

  $("resume-limits").textContent = "PDF only · up to " + LIMITS.maxResumes + " files · " + Math.round(LIMITS.maxBytes / 1048576) + " MB each";

  var state = { jdFile: null, resumes: [], busy: false, pollTimer: null, runId: 0 };

  function fmtSize(b) { return b < 1048576 ? Math.max(1, Math.round(b / 1024)) + " KB" : (b / 1048576).toFixed(1) + " MB"; }
  function jdSource() { return form.querySelector('input[name="jd_source"]:checked').value; }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function showErr(node, msg) { node.textContent = msg; node.hidden = !msg; }

  function validatePdf(file) {
    var name = (file.name || "").toLowerCase();
    if (!name.endsWith(".pdf")) return "Only PDF files are accepted.";
    if (file.type && file.type !== "application/pdf") return "Only PDF files are accepted.";
    if (file.size === 0) return "This file is empty.";
    if (file.size > LIMITS.maxBytes) return "File is larger than " + Math.round(LIMITS.maxBytes / 1048576) + " MB.";
    return "";
  }

  function fileItem(file, problem, onRemove) {
    var info = document.createElement("div"); info.className = "file-info";
    var n = document.createElement("p"); n.className = "file-name"; n.textContent = file.name;
    var s = document.createElement("p"); s.className = "file-size"; s.textContent = fmtSize(file.size);
    info.appendChild(n); info.appendChild(s);
    if (problem) { var p = document.createElement("p"); p.className = "file-problem"; p.textContent = problem; info.appendChild(p); }
    var btn = document.createElement("button");
    btn.type = "button"; btn.className = "btn btn-remove"; btn.textContent = "Remove";
    btn.setAttribute("aria-label", "Remove " + file.name);
    btn.addEventListener("click", onRemove);
    var li = document.createElement("li"); li.className = "file-item" + (problem ? " has-error" : "");
    li.appendChild(info); li.appendChild(btn);
    return li;
  }

  function renderLists() {
    clear(jdList);
    if (state.jdFile) {
      jdList.appendChild(fileItem(state.jdFile, validatePdf(state.jdFile), function () { state.jdFile = null; jdInput.value = ""; renderLists(); }));
    }
    clear(resumeList);
    state.resumes.forEach(function (f, i) {
      resumeList.appendChild(fileItem(f, validatePdf(f), function () { state.resumes.splice(i, 1); renderLists(); }));
    });
    resumeCount.textContent = String(state.resumes.length);
    resumeEmpty.hidden = state.resumes.length > 0;
    resumeClear.hidden = state.resumes.length === 0;
    updateState();
  }

  function validateAll(showMessages) {
    var ok = true, jdMsg = "", pdfMsg = "", resMsg = "";
    if (jdSource() === "text") {
      var t = jdText.value.trim();
      if (!t) { ok = false; jdMsg = showMessages ? "Paste a job description or switch to PDF upload." : ""; }
      else if (jdText.value.length > LIMITS.maxJdChars) { ok = false; jdMsg = "Job description exceeds " + LIMITS.maxJdChars.toLocaleString() + " characters."; }
    } else {
      if (!state.jdFile) { ok = false; pdfMsg = showMessages ? "Add a job description PDF or switch to pasted text." : ""; }
      else { var p = validatePdf(state.jdFile); if (p) { ok = false; pdfMsg = p; } }
    }
    if (!state.resumes.length) { ok = false; resMsg = showMessages ? "Add at least one resume PDF." : ""; }
    else if (state.resumes.length > LIMITS.maxResumes) { ok = false; resMsg = "Too many resumes. The maximum is " + LIMITS.maxResumes + "."; }
    else if (state.resumes.some(function (f) { return validatePdf(f); })) { ok = false; resMsg = "Remove or replace the files marked with errors."; }
    showErr(jdTextError, jdMsg); jdText.setAttribute("aria-invalid", jdMsg ? "true" : "false");
    showErr(jdPdfError, pdfMsg); showErr(resumeError, resMsg);
    return ok;
  }

  function updateState() {
    var ok = validateAll(false);
    analyzeBtn.disabled = state.busy || !ok;
    spinner.hidden = !state.busy;
    analyzeLabel.textContent = state.busy ? "Submitting…" : "Analyze resumes";
    analyzeHelp.hidden = ok && !state.busy;
  }

  function addResumes(fileList) {
    Array.prototype.forEach.call(fileList, function (f) {
      var dup = state.resumes.some(function (x) { return x.name === f.name && x.size === f.size && x.lastModified === f.lastModified; });
      if (!dup) state.resumes.push(f);
    });
    renderLists();
    validateAll(true);
  }

  function wireDropzone(zone, onFiles) {
    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("is-over"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove("is-over"); });
    });
    zone.addEventListener("drop", function (e) { if (e.dataTransfer && e.dataTransfer.files) onFiles(e.dataTransfer.files); });
  }

  wireDropzone($("resume-dropzone"), addResumes);
  wireDropzone($("jd-dropzone"), function (files) { state.jdFile = files[0] || null; renderLists(); validateAll(true); });
  $("resume-browse").addEventListener("click", function () { resumeInput.click(); });
  $("jd-browse").addEventListener("click", function () { jdInput.click(); });
  resumeInput.addEventListener("change", function () { addResumes(resumeInput.files); resumeInput.value = ""; });
  jdInput.addEventListener("change", function () { state.jdFile = jdInput.files[0] || null; renderLists(); validateAll(true); });
  resumeClear.addEventListener("click", function () { state.resumes = []; renderLists(); });
  jdText.addEventListener("input", function () { jdCount.textContent = jdText.value.length.toLocaleString() + " characters"; updateState(); });

  Array.prototype.forEach.call(form.querySelectorAll('input[name="jd_source"]'), function (r) {
    r.addEventListener("change", function () {
      var isText = jdSource() === "text";
      jdTextPanel.hidden = !isText; jdPdfPanel.hidden = isText;
      updateState();
    });
  });

  function showError(title, msg) {
    errorTitle.textContent = title; errorMessage.textContent = msg; errorPanel.hidden = false;
    errorPanel.scrollIntoView({ block: "nearest" });
  }
  function hideError() { errorPanel.hidden = true; }

  function stopPolling() { state.runId++; if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; } }

  function resetToForm() {
    stopPolling();
    RPApi.clearToken();
    state.busy = false;
    hideError();
    statusPanel.hidden = true; resultsPanel.hidden = true;
    clear(resultsContainer);
    form.hidden = false;
    updateState();
    form.scrollIntoView({ block: "start" });
    jdSource() === "text" ? jdText.focus() : $("jd-browse").focus();
  }

  function failTerminal(title, msg) {
    stopPolling();
    RPApi.clearToken();
    statusPanel.hidden = true;
    state.busy = false;
    showError(title, msg);
    $("error-restart").focus();
  }

  function failedJobMessage(err) {
    if (err && typeof err === "object") {
      if (typeof err.message === "string" && err.message) return err.message.slice(0, 300);
    }
    return "The analysis could not be completed. You can start a new analysis.";
  }

  function poll(jobId, firstDelay) {
    var run = ++state.runId;
    var started = Date.now();
    var netErrors = 0;

    function schedule(seconds) {
      state.pollTimer = setTimeout(tick, seconds * 1000);
    }

    async function tick() {
      if (run !== state.runId) return;
      if (Date.now() - started > MAX_POLL_MS) {
        return failTerminal("Taking too long", "The analysis did not finish in the expected time. Please start a new analysis.");
      }
      try {
        var s = await RPApi.getStatus(jobId);
        if (run !== state.runId) return;
        netErrors = 0;
        if (s.status === "completed") {
          stopPolling(); RPApi.clearToken();
          statusPanel.hidden = true; state.busy = false;
          RPResults.render(resultsContainer, s.results);
          resultsPanel.hidden = false; resultsTitle.focus();
          return;
        }
        if (s.status === "failed") {
          return failTerminal("Analysis failed", failedJobMessage(s.error));
        }
        statusText.textContent = s.status === "queued" ? "Queued — waiting for a worker to pick up your job." : "Processing — reading resumes and verifying evidence.";
        statusMeta.textContent = "Status: " + s.status + " · checking again shortly";
        schedule(s.pollAfterSeconds || 2);
      } catch (err) {
        if (run !== state.runId) return;
        var kind = err && err.kind;
        if (kind === "unauthorized") return failTerminal("Access denied", err.message);
        if (kind === "invalid_response") return failTerminal("Unexpected response", err.message);
        if (kind === "rate_limited") { statusMeta.textContent = "Rate limited — retrying shortly."; return schedule(Math.min(Math.max(err.retryAfter || 5, 2), 60)); }
        netErrors++;
        if (netErrors >= MAX_CONSECUTIVE_NET_ERRORS) {
          return failTerminal("Connection problem", "We lost contact with the server. Your analysis may still be running, but it cannot be recovered from this page. Please start a new analysis.");
        }
        statusMeta.textContent = "Connection problem — retrying (" + netErrors + "/" + (MAX_CONSECUTIVE_NET_ERRORS - 1) + ")…";
        schedule(Math.min(2 * Math.pow(2, netErrors), 20));
      }
    }
    schedule(firstDelay);
  }

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    if (state.busy || !validateAll(true)) return;
    hideError();
    state.busy = true; updateState();

    var fd = new FormData();
    if (jdSource() === "text") fd.append("job_description_text", jdText.value.trim());
    else fd.append("job_description_pdf", state.jdFile, state.jdFile.name);
    state.resumes.forEach(function (f) { fd.append("resumes", f, f.name); });

    try {
      var job = await RPApi.createScreening(fd);
      form.hidden = true;
      statusPanel.hidden = false;
      statusText.textContent = "Queued — waiting for a worker to pick up your job.";
      statusMeta.textContent = "";
      poll(job.jobId, job.pollAfterSeconds);
    } catch (err) {
      state.busy = false; updateState();
      showError("Could not start analysis", err && err.message ? err.message : "Unexpected error.");
    }
  });

  $("error-restart").addEventListener("click", resetToForm);
  $("new-analysis").addEventListener("click", resetToForm);

  renderLists();
})();
