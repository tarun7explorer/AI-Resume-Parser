"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("sign-in-form");
  const notice = document.getElementById("auth-notice");
  const submit = form.querySelector("button[type=submit]");

  function showError(message) {
    notice.textContent = message;
    notice.hidden = false;
  }

  if (window.talentLensApi.hasSession) {
    window.talentLensApi.request("/api/v1/auth/me").then(() => {
      window.location.replace("/dashboard");
    }).catch(() => window.talentLensApi.clearSession());
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    notice.hidden = true;
    submit.disabled = true;
    submit.firstElementChild.textContent = "Signing in…";
    const data = new FormData(form);
    try {
      await window.talentLensApi.signIn(String(data.get("email")).trim(), String(data.get("password")));
      window.location.replace("/dashboard");
    } catch (error) {
      showError(error.message || "Sign-in failed. Check your details and try again.");
      submit.disabled = false;
      submit.firstElementChild.textContent = "Sign in";
    }
  });
});
