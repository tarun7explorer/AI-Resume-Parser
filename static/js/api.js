/* Resume-Parser API client.
 * - The job access token lives only in this closure (memory). It is never written to
 *   URLs, localStorage, sessionStorage, cookies, the DOM, or console output.
 */
(function () {
  "use strict";

  var REQUEST_TIMEOUT_MS = 60000;   // upload may be slow
  var STATUS_TIMEOUT_MS = 20000;
  var TERMINAL = { completed: true, failed: true };
  var KNOWN_STATES = { queued: true, processing: true, completed: true, failed: true };

  var token = null; // in-memory only

  function ApiError(kind, message, httpStatus, retryAfter) {
    this.name = "ApiError";
    this.kind = kind; // network | timeout | unauthorized | rate_limited | validation | server | invalid_response
    this.message = message;
    this.httpStatus = httpStatus || 0;
    this.retryAfter = retryAfter || null;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function isObj(v) { return v !== null && typeof v === "object" && !Array.isArray(v); }

  function safeMessageFrom(body, fallback) {
    // Accept {error:{message}}, {message}, or {detail:"..."}; never render raw objects.
    try {
      if (isObj(body)) {
        if (isObj(body.error) && typeof body.error.message === "string") return body.error.message.slice(0, 300);
        if (typeof body.message === "string") return body.message.slice(0, 300);
        if (typeof body.detail === "string") return body.detail.slice(0, 300);
      }
    } catch (e) { /* ignore */ }
    return fallback;
  }

  async function request(url, options, timeoutMs) {
    var controller = new AbortController();
    var timedOut = false;
    var timer = setTimeout(function () { timedOut = true; controller.abort(); }, timeoutMs);
    var response;
    try {
      options.signal = controller.signal;
      options.cache = "no-store";
      options.credentials = "same-origin";
      response = await fetch(url, options);
    } catch (err) {
      clearTimeout(timer);
      if (timedOut) throw new ApiError("timeout", "The server took too long to respond.");
      throw new ApiError("network", "Network error. Check your connection and try again.");
    }
    clearTimeout(timer);

    var body = null;
    var text = "";
    try { text = await response.text(); } catch (e) { text = ""; }
    if (text) {
      try { body = JSON.parse(text); } catch (e) { body = null; }
    }

    if (!response.ok) {
      var status = response.status;
      var retryAfter = parseInt(response.headers.get("Retry-After"), 10);
      if (!isFinite(retryAfter)) retryAfter = null;
      if (status === 401 || status === 403) {
        throw new ApiError("unauthorized", "Access to this analysis was denied. Start a new analysis.", status);
      }
      if (status === 404) {
        throw new ApiError("unauthorized", "This analysis could not be found or has expired.", status);
      }
      if (status === 429) {
        throw new ApiError("rate_limited", "Too many requests. Please wait a moment.", status, retryAfter);
      }
      if (status >= 400 && status < 500) {
        throw new ApiError("validation", safeMessageFrom(body, "The request was rejected. Check your inputs."), status);
      }
      throw new ApiError("server", safeMessageFrom(body, "The server had a problem. Please try again."), status);
    }
    return { status: response.status, body: body };
  }

  /** POST /api/screenings -> expects HTTP 202 {job_id, status, access_token, status_endpoint, poll_after_seconds} */
  async function createScreening(formData) {
    var res = await request("/api/screenings", { method: "POST", body: formData }, REQUEST_TIMEOUT_MS);
    var b = res.body;
    if (res.status !== 202 || !isObj(b) ||
        typeof b.job_id !== "string" || !b.job_id ||
        typeof b.access_token !== "string" || !b.access_token ||
        typeof b.status_endpoint !== "string" || b.status_endpoint !== "/api/status/" + b.job_id ||
        !KNOWN_STATES[b.status]) {
      throw new ApiError("invalid_response", "The server returned an unexpected response.");
    }
    token = b.access_token;
    var poll = Number(b.poll_after_seconds);
    if (!isFinite(poll) || poll <= 0) poll = 2;
    return { jobId: b.job_id, status: b.status, pollAfterSeconds: Math.min(Math.max(poll, 1), 30) };
  }

  /** GET /api/status/{job_id} with Authorization: Bearer <token> */
  async function getStatus(jobId) {
    if (!token) throw new ApiError("unauthorized", "No access token is held for this analysis.");
    var res = await request(
      "/api/status/" + encodeURIComponent(jobId),
      { method: "GET", headers: { "Authorization": "Bearer " + token, "Accept": "application/json" } },
      STATUS_TIMEOUT_MS
    );
    var b = res.body;
    if (!isObj(b) || !KNOWN_STATES[b.status]) {
      throw new ApiError("invalid_response", "The server returned an unexpected status response.");
    }
    if (b.status === "completed" && !Array.isArray(b.results)) {
      throw new ApiError("invalid_response", "The completed analysis contained no results list.");
    }
    var poll = Number(b.poll_after_seconds);
    return {
      status: b.status,
      results: b.status === "completed" ? b.results : null,
      error: b.status === "failed" ? b.error : null,
      pollAfterSeconds: isFinite(poll) && poll > 0 ? Math.min(Math.max(poll, 1), 30) : null
    };
  }

  function clearToken() { token = null; }

  window.RPApi = {
    createScreening: createScreening,
    getStatus: getStatus,
    clearToken: clearToken,
    isTerminal: function (s) { return TERMINAL[s] === true; },
    ApiError: ApiError
  };
})();
