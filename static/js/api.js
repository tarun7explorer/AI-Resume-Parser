"use strict";

(() => {
  const ACCESS_KEY = "talentlens.access";
  const REFRESH_KEY = "talentlens.refresh";
  let refreshInFlight = null;

  function readToken(key) {
    try { return window.sessionStorage.getItem(key); } catch { return null; }
  }

  function saveSession(session) {
    try {
      window.sessionStorage.setItem(ACCESS_KEY, session.access_token);
      window.sessionStorage.setItem(REFRESH_KEY, session.refresh_token);
    } catch {
      throw new Error("This browser cannot securely keep a sign-in session for this tab.");
    }
  }

  function clearSession() {
    try {
      window.sessionStorage.removeItem(ACCESS_KEY);
      window.sessionStorage.removeItem(REFRESH_KEY);
      window.sessionStorage.removeItem("talentlens.organization");
    } catch { /* Storage may be unavailable in restricted browser modes. */ }
  }

  function signalExpired() {
    clearSession();
    window.dispatchEvent(new CustomEvent("talentlens:session-expired"));
  }

  async function decodeResponse(response) {
    if (response.status === 204) return null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const body = await response.json();
      if (!response.ok) {
        const error = body && body.error ? body.error : {};
        const failure = new Error(error.message || `Request failed (${response.status})`);
        failure.status = response.status;
        failure.code = error.code || "request_failed";
        throw failure;
      }
      return body;
    }
    if (!response.ok) {
      const failure = new Error(`Request failed (${response.status})`);
      failure.status = response.status;
      throw failure;
    }
    return response;
  }

  async function refreshSession() {
    if (refreshInFlight) return refreshInFlight;
    const refreshToken = readToken(REFRESH_KEY);
    if (!refreshToken) return false;
    refreshInFlight = (async () => {
      try {
        const response = await fetch("/api/v1/auth/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
        if (!response.ok) return false;
        saveSession(await response.json());
        return true;
      } catch { return false; }
      finally { refreshInFlight = null; }
    })();
    return refreshInFlight;
  }

  async function request(path, options = {}) {
    const makeRequest = () => {
      const headers = new Headers(options.headers || {});
      headers.set("Accept", headers.get("Accept") || "application/json");
      if (typeof options.body === "string" && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
      const token = readToken(ACCESS_KEY);
      if (token) headers.set("Authorization", `Bearer ${token}`);
      return fetch(path, { ...options, headers });
    };
    let response = await makeRequest();
    if (response.status === 401 && readToken(ACCESS_KEY) && await refreshSession()) {
      response = await makeRequest();
    }
    if (response.status === 401 && readToken(ACCESS_KEY)) signalExpired();
    return decodeResponse(response);
  }

  async function signIn(email, password) {
    const session = await decodeResponse(await fetch("/api/v1/auth/sign-in", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ email, password }),
    }));
    saveSession(session);
    return session.user;
  }

  async function signOut() {
    try {
      if (readToken(ACCESS_KEY)) await request("/api/v1/auth/sign-out", { method: "POST" });
    } finally { clearSession(); }
  }

  async function requireUser() {
    if (!readToken(ACCESS_KEY)) {
      window.location.replace("/");
      return null;
    }
    try { return await request("/api/v1/auth/me"); }
    catch (error) {
      if (error.status === 401) window.location.replace("/");
      else showNotice(error.message || "Could not verify your session. Retry or sign in again.");
      return null;
    }
  }

  async function initShell() {
    const user = await requireUser();
    if (!user) return null;
    const email = document.getElementById("user-email");
    if (email) email.textContent = user.email || "Recruiter";
    const select = document.getElementById("organization-select");
    let organizations;
    try { organizations = await request("/api/v1/organizations"); }
    catch (error) {
      showNotice(error.message || "Could not load your organization.");
      return { user, organizations: null, organizationId: null, error };
    }
    if (!select) return { user, organizations, organizationId: null };
    select.replaceChildren();
    for (const membership of organizations) {
      const option = document.createElement("option");
      option.value = membership.organization_id;
      option.textContent = membership.organization_name;
      select.append(option);
    }
    let selected = null;
    try { selected = window.sessionStorage.getItem("talentlens.organization"); } catch { /* optional */ }
    if (organizations.length && !organizations.some((item) => item.organization_id === selected)) {
      selected = organizations[0].organization_id;
    }
    if (selected) {
      select.value = selected;
      try { window.sessionStorage.setItem("talentlens.organization", selected); } catch { /* optional */ }
    }
    select.disabled = organizations.length < 2;
    select.addEventListener("change", () => {
      try { window.sessionStorage.setItem("talentlens.organization", select.value); } catch { /* optional */ }
      window.dispatchEvent(new CustomEvent("talentlens:organization-changed"));
    });
    const signOutButton = document.getElementById("sign-out");
    signOutButton?.addEventListener("click", async () => {
      signOutButton.disabled = true;
      try { await signOut(); } catch { clearSession(); }
      window.location.replace("/");
    });
    window.addEventListener("talentlens:session-expired", () => window.location.replace("/"), { once: true });
    return { user, organizations, organizationId: selected };
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
    if (notice) { notice.hidden = true; notice.textContent = ""; }
  }

  window.talentLensApi = Object.freeze({
    request,
    signIn,
    signOut,
    requireUser,
    initShell,
    showNotice,
    hideNotice,
    get organizationId() {
      try { return window.sessionStorage.getItem("talentlens.organization"); } catch { return null; }
    },
    get hasSession() { return Boolean(readToken(ACCESS_KEY)); },
    clearSession,
  });
})();
