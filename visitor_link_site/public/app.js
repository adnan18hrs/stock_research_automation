(function () {
  const statusEl = document.getElementById("trackingStatus");
  const consentPanel = document.getElementById("consentPanel");
  const allowButton = document.getElementById("allowTracking");
  const skipButton = document.getElementById("skipTracking");
  const preciseLocation = document.getElementById("preciseLocation");
  const visitorEmail = document.getElementById("visitorEmail");

  const state = {
    consent: localStorage.getItem("analyticsConsent") === "yes",
    preciseLocation: null,
    visitorEmail: localStorage.getItem("visitorEmail") || "",
  };

  visitorEmail.value = state.visitorEmail;

  function randomId(prefix) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return `${prefix}_${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
  }

  function getVisitorId() {
    const key = "visitorId";
    let id = localStorage.getItem(key);
    if (!id) {
      id = randomId("visitor");
      localStorage.setItem(key, id);
    }
    return id;
  }

  function getSessionId() {
    const key = "sessionId";
    let id = sessionStorage.getItem(key);
    if (!id) {
      id = randomId("session");
      sessionStorage.setItem(key, id);
    }
    return id;
  }

  function getUtm() {
    const params = new URLSearchParams(window.location.search);
    const keys = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"];
    return keys.reduce((result, key) => {
      if (params.has(key)) result[key] = params.get(key);
      return result;
    }, {});
  }

  function getClientDetails(extra) {
    return {
      visitor_id: getVisitorId(),
      session_id: getSessionId(),
      page_url: window.location.href,
      page_path: window.location.pathname,
      document_referrer: document.referrer || "",
      utm: getUtm(),
      language: navigator.language,
      languages: navigator.languages,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      platform: navigator.platform,
      screen: {
        width: window.screen.width,
        height: window.screen.height,
        color_depth: window.screen.colorDepth,
      },
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
      },
      device_memory: navigator.deviceMemory || null,
      hardware_concurrency: navigator.hardwareConcurrency || null,
      cookie_enabled: navigator.cookieEnabled,
      do_not_track: navigator.doNotTrack || window.doNotTrack || null,
      shared_email: state.visitorEmail || null,
      identity_source: state.visitorEmail ? "visitor_entered_email" : null,
      precise_location: state.preciseLocation,
      ...extra,
    };
  }

  function setStatus(enabled) {
    statusEl.textContent = enabled ? "Analytics on" : "Analytics off";
    statusEl.classList.toggle("on", enabled);
  }

  function requestPreciseLocation() {
    if (!preciseLocation.checked || !navigator.geolocation) {
      return Promise.resolve(null);
    }

    return new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition(
        (position) => {
          resolve({
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
            accuracy_meters: position.coords.accuracy,
            altitude: position.coords.altitude,
            heading: position.coords.heading,
            speed: position.coords.speed,
            captured_at: new Date(position.timestamp).toISOString(),
          });
        },
        (error) => {
          resolve({
            allowed: false,
            error: error.message,
          });
        },
        {
          enableHighAccuracy: true,
          timeout: 8000,
          maximumAge: 0,
        }
      );
    });
  }

  async function track(eventType, extra) {
    if (!state.consent) return { ok: false, skipped: true };

    try {
      const response = await fetch("/api/track", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          consent: true,
          event_type: eventType,
          client: getClientDetails(extra),
        }),
        keepalive: true,
      });
      return response.json();
    } catch (error) {
      return { ok: false, error: error.message };
    }
  }

  allowButton.addEventListener("click", async () => {
    allowButton.disabled = true;
    allowButton.textContent = "Enabling...";
    state.visitorEmail = visitorEmail.value.trim();
    if (state.visitorEmail) {
      localStorage.setItem("visitorEmail", state.visitorEmail);
    } else {
      localStorage.removeItem("visitorEmail");
    }
    state.preciseLocation = await requestPreciseLocation();
    state.consent = true;
    localStorage.setItem("analyticsConsent", "yes");
    consentPanel.hidden = true;
    setStatus(true);
    await track("page_view", { consent_action: "allowed" });
  });

  skipButton.addEventListener("click", () => {
    state.consent = false;
    localStorage.setItem("analyticsConsent", "no");
    consentPanel.hidden = true;
    setStatus(false);
  });

  document.querySelectorAll(".product button").forEach((button) => {
    button.addEventListener("click", async () => {
      const destinationUrl = button.dataset.url;
      const productTitle = button.dataset.title;
      button.disabled = true;
      button.textContent = "Opening...";
      await track("product_click", {
        product_title: productTitle,
        destination_url: destinationUrl,
      });
      window.location.href = destinationUrl;
    });
  });

  setStatus(state.consent);
  if (state.consent) {
    consentPanel.hidden = true;
    track("page_view", { consent_action: "previously_allowed" });
  }
})();
