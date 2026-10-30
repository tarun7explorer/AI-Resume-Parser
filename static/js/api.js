"use strict";

(() => {
  async function request(path, options = {}) {
    let response;
    try {
      response = await fetch(path, { ...options, headers: new Headers(options.headers || {}) });
    } catch {
      throw new Error("The server could not be reached. Check that the local application is running and retry.");
    }
    if (response.status === 204) return null;
    const contentType = response.headers.get("content-type") || "";
    const body = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      const error = body && body.error ? body.error : {};
      const failure = new Error(error.message || `The request failed (${response.status}).`);
      failure.status = response.status;
      failure.code = error.code || "request_failed";
      throw failure;
    }
    return body;
  }

  function showNotice(message, kind = "error") {
    const notice = document.getElementById("page-notice");
    if (!notice) return;
    notice.textContent = message;
    notice.className = `notice${kind === "error" ? "" : ` ${kind}`}`;
    notice.hidden = false;
  }

  function hideNotice() {
    const notice = document.getElementById("page-notice");
    if (notice) {
      notice.hidden = true;
      notice.textContent = "";
    }
  }

  window.talentLensApi = Object.freeze({ request, showNotice, hideNotice });
})();
