const Waya = (() => {
  function csrf() {
    const cookieToken = document.cookie
      .split("; ")
      .find((item) => item.startsWith("csrftoken="))
      ?.split("=")[1];
    return cookieToken ? decodeURIComponent(cookieToken) : document.querySelector("[name=csrfmiddlewaretoken]")?.value;
  }

  async function request(url, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = new Headers(options.headers || {});
    headers.set("X-Requested-With", "XMLHttpRequest");
    if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method)) {
      const token = csrf();
      if (token) headers.set("X-CSRFToken", token);
    }
    if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    let response;
    try {
      response = await fetch(url, { ...options, method, headers, credentials: "same-origin" });
    } catch (networkError) {
      throw new Error("Unable to reach the server. Check the connection and retry.");
    }
    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    if (contentType.includes("application/json")) {
      payload = await response.json().catch(() => null);
    } else {
      const text = await response.text();
      payload = { ok: false, message: text && response.status !== 403 ? text.slice(0, 300) : "" };
    }
    if (!response.ok || !payload?.ok) {
      let message = payload?.message || `Request failed (${response.status})`;
      if (response.status === 403) {
        message = "Security check failed. Refresh the page and retry.";
      } else if (response.status === 401) {
        message = "Your login session has expired. Sign in again.";
      }
      const exception = new Error(message);
      exception.status = response.status;
      exception.payload = payload;
      throw exception;
    }
    return payload.data || {};
  }

  function toast(message) {
    const node = document.querySelector("#appToast");
    node.querySelector(".toast-body").textContent = message;
    bootstrap.Toast.getOrCreateInstance(node).show();
  }

  function parseIntervalInput(input, { fallback = 5, allowBlank = true } = {}) {
    const raw = String(input?.value ?? "").trim();

    if (raw === "") {
      return { valid: allowBlank, value: allowBlank ? Number(fallback) : null, blank: true, message: allowBlank ? "" : "Send interval must be between 5 and 3600 seconds." };
    }
    if (!/^(0|[1-9]\d*)$/.test(raw)) {
      return { valid: false, value: null, blank: false, message: "Send interval must be between 5 and 3600 seconds." };
    }
    const value = Number(raw);
    if (!Number.isSafeInteger(value) || value < 5 || value > 3600) {
      return { valid: false, value: null, blank: false, message: "Send interval must be between 5 and 3600 seconds." };
    }
    return { valid: true, value, blank: false, message: "" };
  }

  function setIntervalValidity(input, feedback, result) {
    input.classList.toggle("is-invalid", !result.valid);
    input.setCustomValidity(result.valid ? "" : result.message);
    feedback.hidden = result.valid;
    feedback.textContent = result.valid ? "" : result.message;
  }

  function uploadForm() {
    document.querySelector("#uploadForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter;
      const status = document.querySelector("#uploadStatus");
      button.disabled = true;
      status.textContent = "Uploading…";
      try {
        const data = await request("/uploads/new/", {
          method: "POST",
          body: new FormData(event.target),
        });
        if (data.requires_sheet) {
          const sheet = data.sheets[0];
          status.textContent = "Processing workbook…";
          const result = await request(`/uploads/${data.id}/sheet/`, {
            method: "POST",
            body: JSON.stringify({ sheet_name: sheet, header_row: 1 }),
          });
          location.href = result.url;
          return;
        }
        status.textContent = "Processing workbook…";
        location.href = data.url;
      } catch (error) {
        status.textContent = error.message;
        button.disabled = false;
      }
    });
  }

  function dataset(id) {
    let page = 1;
    let filter = "all";
    let rows = [];
    const modal = bootstrap.Modal.getOrCreateInstance("#phoneModal");
    const $ = (selector) => document.querySelector(selector);

    function badges(counts) {
      $("#counts").innerHTML = Object.entries(counts)
        .map(([key, value]) => `<span>${key}: <strong>${value}</strong></span>`)
        .join("");
      $("#selectedCount").textContent = `${counts.selected || 0} selected`;
      ["#composeCampaignButton", "#composeCampaignSticky"].forEach((selector) => {
        const button = $(selector); if (!button) return;
        const enabled = Boolean(counts.selected) && Boolean($("#phoneColumn").value || button.id === "composeCampaignButton" && !button.classList.contains("disabled"));
        button.classList.toggle("disabled", !enabled); button.setAttribute("aria-disabled", String(!enabled));
      });
    }

    function whatsappBadge(row) {
      const states = { exists: ["success", "Registered"], not_exists: ["danger", "Not on WhatsApp"], checking: ["primary", "Checking…"], error: [row.whatsapp_retry_seconds ? "secondary" : "warning", row.whatsapp_retry_seconds ? "Waiting to retry" : "Check failed"], unknown: ["secondary", "Unchecked"] };
      const value = states[row.whatsapp] || states.unknown;
      return `<button type="button" class="badge text-bg-${value[0]} border-0" data-check-detail="${row.id}" title="${escapeHtml(row.whatsapp_error || "")}">${value[1]}</button>`;
    }

    function updateHeader() { const checks = [...document.querySelectorAll(".row-check")]; $("#visibleAll").checked = checks.length && checks.every((node) => node.checked); $("#visibleAll").indeterminate = checks.some((node) => node.checked) && !$("#visibleAll").checked; }

    async function whatsappProgress() { const data = await request(`/uploads/${id}/whatsapp-check/progress/`); $("#whatsappProgress").textContent = `Confirmed ${data.confirmed}/${data.total} · ${data.pending} pending${data.waiting_retry ? ` · ${data.waiting_retry} waiting` : ""}${data.paused ? " · Paused" : ""}`; $("#whatsappProgressBar").style.width = `${data.percent}%`; return data; }
    let checkTimer = null;
    let checkInFlight = false;
    let checksPausedByError = false;
    function scheduleCheck(seconds = 0) { clearTimeout(checkTimer); if (document.hidden || !navigator.onLine || checksPausedByError) return; checkTimer = setTimeout(runNextCheck, Math.max(0, seconds) * 1000); }
    function patchRecipient(updated) { const index = rows.findIndex((row) => row.id === updated.id); if (index >= 0) { rows[index] = {...rows[index], ...updated}; const badge = document.querySelector(`[data-check-detail="${updated.id}"]`); if (badge) badge.outerHTML = whatsappBadge(rows[index]); } }
    async function runNextCheck() { if (checkInFlight || document.hidden || !navigator.onLine || checksPausedByError) return; checkInFlight = true; try { const progress = await whatsappProgress(); if (progress.paused || !progress.pending) return; const data = await request(`/uploads/${id}/whatsapp-check/next/`, {method:"POST", body:"{}"}); if (data.recipient) patchRecipient({id:data.recipient.id, whatsapp:data.recipient.whatsapp, whatsapp_error:data.recipient.error, whatsapp_error_code:data.recipient.whatsapp_error_code, whatsapp_http_status:data.recipient.http_status}); await whatsappProgress(); if (data.authentication_error) { checksPausedByError = true; toast("WhatsApp provider authentication failed. Checks are paused."); return; } if (!data.finished && !data.paused) scheduleCheck(data.wait_seconds || 0); } catch (error) { toast(error.message); } finally { checkInFlight = false; } }

    async function columns() {
      const data = await request(`/uploads/${id}/columns/`);
      $("#status").textContent = data.status;
      const select = $("#phoneColumn");
      if (select.options.length === 1) {
        data.columns.forEach((column) => {
          select.insertAdjacentHTML(
            "beforeend",
            `<option value="${column.key}">${column.label} — ${data.samples[column.key].slice(0, 3).join(", ")}</option>`,
          );
        });
      }
    }

    async function load() {
      const data = await request(`/datasets/${id}/recipients/?filter=${filter}&page=${page}`);
      rows = data.rows;
      badges(data.counts);
      $("#pageInfo").textContent = `Page ${page} · ${data.total} matching`;
      $("#recipientRows").innerHTML = rows.map((row) => `
        <tr class="${["invalid", "blank"].includes(row.validation) ? "invalid-row" : ""}">
          <td><input class="row-check" data-id="${row.id}" type="checkbox" ${row.selected ? "checked" : ""}></td>
          <td>${row.row_number}</td>
          <td><span class="phone-link" data-edit="${row.id}">${escapeHtml(row.phone_original || "blank")}</span></td>
          <td>${escapeHtml(row.phone_normalized)}</td>
          <td><span class="badge text-bg-${row.validation === "valid" ? "success" : row.validation === "warning" ? "warning" : "danger"}">${row.validation}${row.duplicate ? " · duplicate" : ""}${row.suppressed ? " · suppressed" : ""}</span></td>
          <td>${whatsappBadge(row)}</td>
        </tr>`).join("") || '<tr><td colspan="6" class="empty">No matching recipients.</td></tr>';
      updateHeader();
    }

    $("#normalize").onclick = async () => {
      const column = $("#phoneColumn").value;
      if (!column) return toast("Choose a column.");
      const result = await request(`/uploads/${id}/select-phone-column/`, {
        method: "POST", body: JSON.stringify({ column }),
      });
      badges(result);
      $("#composeCampaignButton").classList.remove("disabled"); $("#composeCampaignButton").removeAttribute("aria-disabled"); $("#phoneColumnHelp").textContent = "Phone column selected";
      await load();
      if (result.auto_check) { await request(`/uploads/${id}/whatsapp-check/start/`, {method:"POST", body:"{}"}); scheduleCheck(0); }
      toast("Numbers normalized.");
    };
    $("#filter").onchange = (event) => { filter = event.target.value; page = 1; load(); };
    $("#more").onclick = () => { page += 1; load(); };
    $("#recipientRows").onclick = (event) => {
      const detailId = event.target.dataset.checkDetail;
      if (detailId) { const row = rows.find((item) => item.id === detailId); if (row && row.whatsapp === "error") { if (confirm(`${row.whatsapp_error || "Registration check failed."}\nRetry this number now?`)) { event.target.disabled = true; request(`/recipients/${detailId}/check-whatsapp/`, {method:"POST", body:JSON.stringify({retry_now:true})}).then((data) => { patchRecipient(data.recipient); whatsappProgress(); }).catch((error) => toast(error.message)).finally(() => { event.target.disabled = false; }); } } return; }
      const recipientId = event.target.dataset.edit;
      if (!recipientId) return;
      const row = rows.find((item) => item.id === recipientId);
      $("#recipientId").value = recipientId;
      $("#phoneValue").value = row.phone_original;
      modal.show();
    };
    $("#recipientRows").onchange = (event) => {
      if (!event.target.classList.contains("row-check")) return;
      request(`/datasets/${id}/selection/`, {
        method: "POST",
        body: JSON.stringify({ action: "set", ids: [event.target.dataset.id], selected: event.target.checked }),
      }).then(load);
    };
    $("#visibleAll").onchange = (event) => request(`/datasets/${id}/selection/`, {
      method: "POST",
      body: JSON.stringify({ action: "set", ids: rows.map((row) => row.id), selected: event.target.checked }),
    }).then(load);
    $("#selectMatching").onclick = () => request(`/datasets/${id}/selection/`, {
      method: "POST", body: JSON.stringify({ action: "matching", filter }),
    }).then(load);
    $("#clearSelection").onclick = () => request(`/datasets/${id}/selection/`, {
      method: "POST", body: JSON.stringify({ action: "clear" }),
    }).then(load);
    $("#selectVisible").onclick = () => request(`/datasets/${id}/selection/`, {method:"POST", body:JSON.stringify({action:"visible", ids:rows.map(row => row.id), selected:true})}).then(load);
    $("#selectAll").onclick = () => request(`/datasets/${id}/selection/`, {method:"POST", body:JSON.stringify({action:"all"})}).then(load);
    $("#selectValid").onclick = () => request(`/datasets/${id}/selection/`, {method:"POST", body:JSON.stringify({action:"valid"})}).then(load);
    $("#selectWhatsApp").onclick = () => request(`/datasets/${id}/selection/`, {method:"POST", body:JSON.stringify({action:"whatsapp_exists"})}).then(load);
    $("#pauseWhatsAppChecks").onclick = () => request(`/uploads/${id}/whatsapp-check/pause/`, {method:"POST", body:"{}"}).then(whatsappProgress);
    $("#resumeWhatsAppChecks").onclick = async () => { checksPausedByError = false; await request(`/uploads/${id}/whatsapp-check/start/`, {method:"POST", body:"{}"}); scheduleCheck(0); };
    $("#phoneForm").onsubmit = async (event) => {
      event.preventDefault();
      try {
        const data = await request(`/recipients/${$("#recipientId").value}/edit-phone/`, {
          method: "POST", body: JSON.stringify({ phone: $("#phoneValue").value }),
        });
        modal.hide();
        await load();
        if (data.should_check) { await request(`/uploads/${id}/whatsapp-check/start/`, {method:"POST", body:"{}"}); scheduleCheck(0); }
        toast("Correction saved.");
      } catch (error) {
        $("#phoneError").textContent = error.message;
        $("#phoneValue").classList.add("is-invalid");
      }
    };
    columns();
    load();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) scheduleCheck(0); });
    window.addEventListener("online", () => scheduleCheck(0));
    window.addEventListener("pagehide", () => clearTimeout(checkTimer), {once:true});
    whatsappProgress().then((data) => { if (data.pending && !data.paused) scheduleCheck(0); }).catch(() => {});
  }

  function campaignForm(id) {
    const body = document.querySelector("#body");
    body.oninput = () => {
      document.querySelector("#chars").textContent = `${body.value.length} characters`;
    };
    document.querySelector("#placeholder").onchange = (event) => {
      if (!event.target.value) return;
      body.setRangeText(`{${event.target.value}}`, body.selectionStart, body.selectionEnd, "end");
      body.dispatchEvent(new Event("input"));
    };
    const interval = document.querySelector("#sendInterval");
    const feedback = document.querySelector("#sendIntervalFeedback");
    const initial = parseIntervalInput(interval, { allowBlank: true });
    let selectedInterval = initial.valid ? initial.value : 0;
    const updateInterval = () => {
      const parsed = parseIntervalInput(interval, { fallback: selectedInterval, allowBlank: true });
      setIntervalValidity(interval, feedback, parsed);
      document.querySelectorAll(".campaign-interval").forEach((button) => button.classList.toggle("active", parsed.valid && !parsed.blank && Number(button.dataset.value) === parsed.value));
      return parsed;
    };
    document.querySelectorAll(".campaign-interval").forEach((button) => button.onclick = () => {
      const value = Number(button.dataset.value);
      interval.value = String(value);
      selectedInterval = value;
      updateInterval();
    });
    interval.addEventListener("input", updateInterval);
    updateInterval();
    const mediaInput = document.querySelector("#media");
    const mediaFeedback = document.querySelector("#mediaFeedback");
    const mediaActions = document.querySelector("#mediaActions");
    const extensionLabels = { xlsx: "Excel workbook", xls: "Excel workbook", csv: "CSV document", pdf: "PDF document", doc: "Word document", docx: "Word document", txt: "Text document" };
    let uploadedMediaId = "";
    let uploadInFlight = false;
    const describeFile = (file) => {
      const extension = (file.name.split(".").pop() || "").toLowerCase();
      return `${file.name} · ${extensionLabels[extension] || file.type || "Media file"} · ${Math.max(1, Math.ceil(file.size / 1024))} KB`;
    };
    const showMedia = (text, state = "") => {
      mediaFeedback.textContent = text;
      mediaFeedback.className = `media-feedback small mt-1 ${state}`;
    };
    const uploadSelectedMedia = async () => {
      const file = mediaInput.files[0];
      if (!file || uploadInFlight) return "";
      uploadInFlight = true;
      uploadedMediaId = "";
      mediaActions.hidden = true;
      showMedia(`Validating ${extensionLabels[(file.name.split(".").pop() || "").toLowerCase()] || "media"}…`, "is-pending");
      try {
        const form = new FormData(); form.append("file", file);
        showMedia("Uploading to WhatsApp provider…", "is-pending");
        const data = await request("/media/new/", { method: "POST", body: form });
        uploadedMediaId = data.id;
        showMedia(`Ready: ${file.name}`, "is-ready");
        return uploadedMediaId;
      } catch (error) {
        const fieldError = error.payload?.errors?.file?.[0] || error.message;
        showMedia(`${extensionLabels[(file.name.split(".").pop() || "").toLowerCase()] || "Media"} upload failed: ${fieldError}`, "is-error");
        mediaActions.hidden = false;
        throw error;
      } finally { uploadInFlight = false; }
    };
    mediaInput.onchange = () => {
      uploadedMediaId = "";
      const file = mediaInput.files[0];
      mediaActions.hidden = !file;
      showMedia(file ? `Selected: ${describeFile(file)}` : "");
    };
    document.querySelector("#removeMedia").onclick = () => {
      mediaInput.value = ""; uploadedMediaId = ""; mediaActions.hidden = true; showMedia("");
    };
    document.querySelector("#retryMediaUpload").onclick = async () => {
      try { await uploadSelectedMedia(); } catch (_) { /* Inline feedback already explains the failure. */ }
    };
    document.querySelector("#campaignForm").onsubmit = async (event) => {
      event.preventDefault();
      const parsed = updateInterval();
      if (!parsed.valid) return;
      if (!document.querySelector("#name").value.trim() || !body.value.trim() || !document.querySelector("#optin").checked) {
        toast("Enter a campaign name and message, and confirm recipient opt-in.");
        return;
      }
      const submitButton = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
      if (!submitButton || submitButton.disabled) return;
      submitButton.disabled = true;
      try {
        let media_id = uploadedMediaId;
        const file = mediaInput.files[0];
        if (file) {
          media_id = uploadedMediaId || await uploadSelectedMedia();
        }
        const data = await request(`/campaigns/create/${id}/`, {
          method: "POST",
          body: JSON.stringify({
            name: document.querySelector("#name").value,
            body: body.value,
            media_id,
            missing_value_policy: document.querySelector("#policy").value,
            missing_value_fallback: document.querySelector("#fallback").value,
            allow_unknown: document.querySelector("#unknown").checked,
            allow_duplicates: document.querySelector("#duplicates").checked,
            send_interval_seconds: parsed.value,
            opt_in_confirmed: document.querySelector("#optin").checked,
          }),
        });
        location.href = data.url;
      } catch (error) {
        const fields = error.payload?.fields || error.payload?.errors || {};
        const intervalErrors = fields.send_interval_seconds;
        if (intervalErrors?.length) {
          setIntervalValidity(interval, feedback, { valid: false, message: intervalErrors[0] });
        }
        toast(error.message);
        submitButton.disabled = false;
      }
    };
  }

  function setTextIfChanged(node, value) { const next = String(value ?? ""); if (node && node.textContent !== next) node.textContent = next; }
  function createCampaignRecipientRow(data) {
    const template = document.createElement("template");
    template.innerHTML = `<tr id="message-${data.id}" data-message-id="${data.id}" data-serial="${data.sequence}" data-provider-message-id="${escapeHtml(data.provider_message_id || "")}" data-state="${escapeHtml(data.state)}" data-status-sync-eligible="${["accepted", "pending", "sent", "delivered"].includes(data.state) && data.provider_message_id && !data.is_deleted}"><td><input type="checkbox" class="message-check" aria-label="Select message"></td><td data-field="sequence">${data.sequence ?? "—"}</td><td data-field="phone">${escapeHtml(data.phone_masked)}</td><td><button class="btn btn-link p-0 text-start preview-cell" data-field="message-preview" data-message-action="detail">${escapeHtml(data.preview)}</button></td><td class="message-status-cell"><span data-field="status" class="app-status app-status--${escapeHtml(data.state)}">● ${escapeHtml(data.state_label)}</span></td><td data-field="reason" class="text-danger">${escapeHtml(data.error)}</td><td class="text-end message-actions"><button class="btn btn-sm btn-outline-secondary" data-message-action="detail">View</button><button class="btn btn-sm btn-outline-primary" data-message-action="refresh" ${!data.provider_message_id || data.is_deleted ? "disabled" : ""}>↻</button><button class="btn btn-sm btn-outline-secondary" data-message-action="update" ${data.is_deleted || (!data.provider_message_id && data.state !== "failed") ? "disabled" : ""}>Update</button><button class="btn btn-sm btn-outline-danger" data-message-action="delete" ${!data.provider_message_id || data.is_deleted ? "disabled" : ""}>Delete</button>${data.state === "failed" && !data.is_deleted ? '<button class="btn btn-sm btn-warning" data-message-action="resend">Resend</button>' : ""}</td></tr>`;
    return template.content.firstElementChild;
  }
  function patchCampaignRecipientRow(row, data) {
    if (!row) return;
    row.dataset.state = data.state; row.dataset.providerMessageId = data.provider_message_id || "";
    row.dataset.statusSyncEligible = String(["accepted", "pending", "sent", "delivered"].includes(data.state) && data.provider_message_id && !data.is_deleted);
    setTextIfChanged(row.querySelector('[data-field="sequence"]'), data.sequence ?? "—");
    setTextIfChanged(row.querySelector('[data-field="phone"]'), data.phone_masked);
    setTextIfChanged(row.querySelector('[data-field="message-preview"]'), data.preview);
    setTextIfChanged(row.querySelector('[data-field="reason"]'), data.error);
    const badge = row.querySelector('[data-field="status"]'); if (badge) { const text = `● ${data.is_deleted ? "Deleted" : data.state_label}`; if (badge.textContent !== text) { badge.textContent = text; badge.className = `app-status app-status--${data.is_deleted ? "deleted" : data.state} status-badge-updated`; setTimeout(() => badge.classList.remove("status-badge-updated"), 200); } }
  }
  function campaign(id, options = {}) {
    const $ = (selector) => document.querySelector(selector);
    const tokenKey = `waya-campaign-run-${id}`;
    let runToken = sessionStorage.getItem(tokenKey) || "";
    let sendingLoopActive = false;
    let pendingSendRequest = false;
    let loopTimer = null;
    let pollTimer = null;
    let countdownTimer = null;
    let lastProgress = null;

    function stopLoop() {
      sendingLoopActive = false;
      clearTimeout(loopTimer);
      clearInterval(countdownTimer);
      $("#nextSend").textContent = "";
    }

    function formatWait(seconds) {
      return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
    }

    function countdown(seconds, callback) {
      clearInterval(countdownTimer);
      let remaining = Math.max(0, Math.ceil(seconds));
      const draw = () => { $("#nextSend").textContent = remaining ? `Next message in ${formatWait(remaining)}` : ""; };
      draw();
      countdownTimer = setInterval(() => {
        remaining -= 1;
        draw();
        if (remaining <= 0) {
          clearInterval(countdownTimer);
          callback();
        }
      }, 1000);
    }

    function render(data, sendingSequence = null) {
      lastProgress = data;
      const processed = data.processed ?? data.completed ?? 0;
      const activeSequence = sendingSequence || (data.processing ? data.current_number : null);
      let heading = `Ready: ${data.progress_text}`;
      if (activeSequence) {
        heading = `${data.media_needs_upload ? "Preparing media… · " : ""}Sending ${activeSequence}/${data.sendable_total}`;
      } else if (["completed", "completed_with_errors"].includes(data.status)) {
        heading = `Complete: ${data.progress_text}`;
      } else if (data.latest_result && processed) {
        heading = `Processed ${data.progress_text} — ${data.latest_result.state}`;
      }
      setTextIfChanged($("#campaignStatus"), heading);
      setTextIfChanged($("#progressText"), `${data.progress_text} · ${data.percent}%`);
      const bar = $("#campaignProgressBar");
      if (bar.style.width !== `${data.percent}%`) bar.style.width = `${data.percent}%`;
      bar.setAttribute("aria-valuenow", processed);
      bar.setAttribute("aria-valuemax", data.sendable_total || 1);
      ["sendable_total", "processed", "accepted", "sent", "delivered", "read", "failed", "remaining", "skipped", "invalid"].forEach((key) => {
        setTextIfChanged($(`#${key}`), data[key]);
      });
      $("#providerAlert").classList.toggle("d-none", data.provider_configured !== false);
      const body = $("#recipientRows"), seen = new Set();
      data.recipients.forEach((row) => { seen.add(row.id); let node = body.querySelector(`#message-${CSS.escape(row.id)}`); if (!node) { node = createCampaignRecipientRow(row); body.appendChild(node); } patchCampaignRecipientRow(node, row); node.classList.toggle("table-primary", row.state === "processing" || row.sequence === sendingSequence); });
      [...body.querySelectorAll("[data-message-id]")].forEach((node) => { if (!seen.has(node.dataset.messageId)) node.remove(); });
      $('[data-action="start"]').disabled = !data.can_start;
      $('[data-action="resume"]').disabled = !data.can_resume;
      $('[data-action="pause"]').disabled = !data.can_pause;
      $('[data-action="cancel"]').disabled = !data.can_cancel;
      $("#completionSummary").classList.toggle(
        "d-none",
        !["completed", "completed_with_errors", "cancelled"].includes(data.status),
      );
    }

    async function progress() {
      try {
        const data = await request(`/campaigns/${id}/progress/`);
        render(data);
        const protection = $("#providerProtection");
        if (protection) protection.classList.toggle("d-none", !data.rate_limited);
        if (data.rate_limited && protection) { protection.textContent = data.message || `The provider requested a ${data.wait_seconds}-second pause. Sending will resume automatically.`; }
        $("#liveDisconnect").classList.add("d-none");
        return data;
      } catch (error) {
        $("#liveDisconnect").textContent = `Live updates disconnected — ${error.message}`;
        $("#liveDisconnect").classList.remove("d-none");
        return null;
      }
    }

    function scheduleLoop(waitSeconds) {
      clearTimeout(loopTimer);
      if (!sendingLoopActive) return;
      if (waitSeconds > 0) {
        countdown(waitSeconds, () => {
          loopTimer = setTimeout(sendNext, 0);
        });
      } else {
        loopTimer = setTimeout(sendNext, 0);
      }
    }

    async function sendNext() {
      if (!sendingLoopActive || pendingSendRequest || !runToken) return;
      pendingSendRequest = true;
      const sequence = lastProgress?.next_sequence;
      if (lastProgress && sequence) render(lastProgress, sequence);
      try {
        const data = await request(`/campaigns/${id}/send-next/`, {
          method: "POST",
          body: JSON.stringify({ run_token: runToken }),
        });
        render(data);
        if (data.finished || !["sending", "queued"].includes(data.status)) {
          stopLoop();
          if (data.finished) sessionStorage.removeItem(tokenKey);
          return;
        }
        scheduleLoop(data.wait_seconds || data.retry_after || 0);
      } catch (error) {
        stopLoop();
        const data = await progress();
        const resume = $('[data-action="resume"]');
        if (data?.remaining > 0) {
          resume.disabled = false;
          resume.textContent = "Resume safely";
          toast("Connection changed during sending. Review progress, then Resume safely.");
        } else {
          toast(error.message);
        }
      } finally {
        pendingSendRequest = false;
      }
    }

    async function action(name, button) {
      // Destructive campaign cancellation is confirmed by the server-side state
      // and can be resumed only through a deliberate new run; keep this AJAX-only.
      const original = button.innerHTML;
      button.disabled = true;
      button.innerHTML = name === "start"
        ? '<span class="spinner-border spinner-border-sm"></span> Starting…'
        : "Working…";
      try {
        const url = name === "preflight"
          ? `/campaigns/${id}/preflight/`
          : name === "resend-failed"
            ? `/campaigns/${id}/resend-failed/`
            : `/campaigns/${id}/${name}/`;
        const data = await request(url, { method: "POST", body: "{}" });
        if (data.run_token) {
          runToken = data.run_token;
          sessionStorage.setItem(tokenKey, runToken);
        }
        if (name === "start" || name === "resume" || name === "resend-failed") {
          render(data);
          if (!sendingLoopActive) {
            sendingLoopActive = true;
            scheduleLoop(data.wait_seconds || 0);
          }
        } else if (name === "preflight") {
          await runPreflight(data.run_token, data.total);
        } else {
          stopLoop();
          render(data);
        }
      } catch (error) {
        toast(error.message);
      } finally {
        button.innerHTML = original;
        if (!sendingLoopActive) button.disabled = false;
      }
    }

    async function runPreflight(token, total) {
      let checked = 0;
      while (token) {
        const data = await request(`/campaigns/${id}/preflight-next/`, {
          method: "POST",
          body: JSON.stringify({ run_token: token }),
        });
        if (data.finished) break;
        checked += 1;
        $("#campaignStatus").textContent = `Checked ${checked}/${total}`;
        await new Promise((resolve) => setTimeout(resolve, (data.wait_seconds || 0) * 1000));
      }
      toast(`Preflight complete: checked ${checked}.`);
      await progress();
    }

    document.addEventListener("click", async (event) => {
      const edit = event.target.closest("[data-edit-recipient]");
      if (edit) { toast("Use the message Update action to correct and resend this number."); return; }
      const button = event.target.closest("[data-action]");
      if (button) action(button.dataset.action, button);
    });
    const scheduleProgress = () => {
      clearTimeout(pollTimer);
      if (document.hidden) return;
      const active = lastProgress?.status === "sending";
      const deliveryPending = (lastProgress?.accepted || 0) + (lastProgress?.sent || 0) + (lastProgress?.delivered || 0) > 0;
      const delay = active ? 2500 : deliveryPending ? 5000 : 30000;
      pollTimer = setTimeout(async () => { await progress(); scheduleProgress(); }, delay);
    };
    let progressInFlight = false;
    const originalProgress = progress;
    progress = async function () { if (progressInFlight) return lastProgress; progressInFlight = true; try { return await originalProgress(); } finally { progressInFlight = false; } };
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { progress().then(scheduleProgress); } else clearTimeout(pollTimer); });
    window.addEventListener("pagehide", () => clearTimeout(pollTimer), { once: true });
    progress().then(() => { if (options.enableMessageLogActions) messageLogs("#recipientRows", {campaignId: id}); scheduleProgress(); });
  }

  function escapeHtml(value) {
    const node = document.createElement("div");
    node.textContent = value ?? "";
    return node.innerHTML;
  }

  function messageLogs(selector = "#messageRows", options = {}) {
    const rows = document.querySelector(selector);
    if (!rows) return;
    const detailModal = bootstrap.Modal.getOrCreateInstance("#messageDetailModal");
    const updateModal = bootstrap.Modal.getOrCreateInstance("#messageUpdateModal");
    const deleteModal = bootstrap.Modal.getOrCreateInstance("#messageDeleteModal");
    const resendModal = bootstrap.Modal.getOrCreateInstance("#messageResendModal");
    let activeRow = null;
    let activeData = null;
    let syncTimer = null;
    let syncInFlight = false;
    let syncCursor = 0;
    let authWarned = false;
    const liveIndicator = document.querySelector("#liveStatusSync");
    const selectionToolbar = document.querySelector("#messageBulkToolbar");
    function selectedRows() { return [...rows.querySelectorAll(".message-check:checked")].map((box) => box.closest("[data-message-id]")); }
    function redrawBulk() { if (!selectionToolbar) return; const selected = selectedRows(); selectionToolbar.classList.toggle("d-none", !selected.length); document.querySelector("#messageSelectedCount").textContent = `${selected.length} selected`; const all = [...rows.querySelectorAll(".message-check")]; const head = document.querySelector("#messageVisibleAll"); if (head) { head.checked = all.length && all.length === selected.length; head.indeterminate = selected.length > 0 && selected.length < all.length; } }

    const formatDate = (value) => value ? new Date(value).toLocaleString() : "—";
    const rowFor = (target) => target.closest("[data-message-id]");

    function statusIcon(state) {
      return { pending: "◷", sent: "✓", delivered: "✓✓", read: "◉", played: "▶", failed: "!", deleted: "⌫", unknown: "?" }[state] || "?";
    }

    function actionButton(action, label, style, enabled = true) {
      return `<button class="btn btn-sm ${style}" data-message-action="${action}" ${enabled ? "" : "disabled"}>${label}</button>`;
    }

    function renderRow(row, data) {
      if (!row || !data) return;
      const serial = data.serial_number || row.dataset.serial;
      const wasChecked = row.querySelector(".message-check")?.checked;
      row.dataset.serial = serial;
      row.dataset.providerMessageId = data.provider_message_id || "";
      row.dataset.state = data.state;
      row.dataset.statusSyncEligible = String(Boolean(data.status_sync_eligible));
      row.classList.add("message-row-updated");
      row.innerHTML = `
        <td><input class="message-check" type="checkbox" value="${escapeHtml(data.id)}" aria-label="Select message" ${wasChecked ? "checked" : ""}></td>
        <td class="message-serial">${escapeHtml(serial)}</td>
        <td><span class="message-phone">${escapeHtml(data.phone)}</span><button type="button" class="btn btn-sm btn-link copy-phone" data-phone="${escapeHtml(data.phone)}" aria-label="Copy phone">⧉</button></td>
        <td><button type="button" class="btn btn-link p-0 text-start message-preview" data-message-action="detail">${escapeHtml(data.message.length > 90 ? `${data.message.slice(0, 90)}…` : data.message)}</button></td>
        <td class="message-campaign">${escapeHtml(data.campaign_name)}</td>
        <td><span class="app-status app-status--${escapeHtml(data.state)}">● ${escapeHtml(data.state_label)}</span></td>
        <td class="message-time">${escapeHtml(formatDate(data.sent_at || data.updated_at))}</td>
        <td class="text-end message-actions">
          ${actionButton("detail", "View", "btn-outline-secondary")}
          ${actionButton("refresh", "↻", "btn-outline-primary", data.can_refresh_status)}
          <details class="action-more"><summary class="btn btn-sm btn-outline-secondary">More</summary><div>${actionButton("update", "Update", "btn-outline-secondary", data.can_update)} ${actionButton("delete", "Delete", "btn-outline-danger", data.can_delete)} ${data.can_resend ? actionButton("resend", "Resend", "btn-warning") : ""}</div></details>
        </td>`;
      setTimeout(() => row.classList.remove("message-row-updated"), 1400);
    }

    function patchMessageStatus(row, patch) {
      if (!row || !patch) return;
      row.dataset.state = patch.state;
      row.dataset.statusSyncEligible = String(Boolean(patch.status_sync_eligible));
      const badge = row.querySelector(".app-status, [data-field=\"status\"]");
      if (badge) { const text = `● ${patch.state_label}`; if (badge.textContent !== text) { badge.textContent = text; badge.className = `app-status app-status--${patch.state} status-badge-updated`; setTimeout(() => badge.classList.remove("status-badge-updated"), 200); } }
      if (activeRow === row && document.querySelector("#messageDetailModal")?.classList.contains("show")) patchDetailStatus(patch);
    }
    function patchCampaignCounts(counts) {
      if (!counts) return;
      setTextIfChanged(document.querySelector("#accepted"), counts.pending);
      setTextIfChanged(document.querySelector("#sent"), counts.sent);
      setTextIfChanged(document.querySelector("#delivered"), counts.delivered);
      setTextIfChanged(document.querySelector("#read"), counts.read);
      setTextIfChanged(document.querySelector("#failed"), counts.failed);
    }

    function syncStatus(text, checking = false) {
      if (!liveIndicator) return;
      const stamp = liveIndicator.querySelector("[data-live-time]");
      if (stamp) setTextIfChanged(stamp, text ? ` · Last checked ${text}` : "");
    }
    function eligibleRows() {
      return [...rows.querySelectorAll("[data-message-id]")].filter((row) =>
        ["accepted", "pending", "sent", "delivered"].includes(row.dataset.state) && Boolean(row.dataset.providerMessageId)
          && row.dataset.statusSyncEligible !== "false"
      );
    }
    function scheduleSync(delay = 5000) {
      clearTimeout(syncTimer);
      if (!document.hidden && navigator.onLine) syncTimer = setTimeout(sync, delay);
    }
    async function sync() {
      if (syncInFlight || document.hidden || !navigator.onLine) return;
      const eligible = eligibleRows();
      if (!eligible.length) { scheduleSync(); return; }
      const selected = [];
      for (let index = 0; index < Math.min(5, eligible.length); index += 1) selected.push(eligible[(syncCursor + index) % eligible.length]);
      syncCursor = (syncCursor + selected.length) % eligible.length;
      syncInFlight = true;
      try {
        const data = await request("/messages/auto-sync-statuses/", { method: "POST", body: JSON.stringify({ ids: selected.map((row) => row.dataset.messageId), campaign_id: options.campaignId || undefined, limit: 5, serial_numbers: Object.fromEntries(selected.map((row) => [row.dataset.messageId, Number(row.dataset.serial)])) }) });
        const patches = data.results.filter((result) => result.changed && result.patch);
        requestAnimationFrame(() => patches.forEach((result) => { patchMessageStatus(rows.querySelector(`#message-${CSS.escape(result.id)}`), result.patch); patchCampaignCounts(result.campaign_counts); }));
        if (data.auth_failed) {
          if (!authWarned) { toast("Live delivery status is temporarily unavailable because provider authentication failed."); authWarned = true; }
          return;
        }
        syncStatus(new Date().toLocaleTimeString());
      } catch (_) { /* transient polling failures remain quiet */ }
      finally { syncInFlight = false; scheduleSync(); }
    }

    async function loadDetail(row, show = true) {
      if (show) {
        document.querySelector("#messageDetailBody").innerHTML = '<div class="message-skeleton"></div>';
        detailModal.show();
      }
      const data = await request(`/messages/${row.dataset.messageId}/detail/?serial=${encodeURIComponent(row.dataset.serial)}`);
      activeData = data;
      if (!show) return data;
      const attempts = data.attempts.map((attempt) => `
        <article class="attempt-card">
          <div class="d-flex justify-content-between"><strong>Attempt ${attempt.attempt_number}</strong><span>${escapeHtml(formatDate(attempt.attempted_at))}</span></div>
          ${attempt.error_message ? `<div class="alert alert-danger mt-2 mb-2"><strong>${escapeHtml(attempt.error_category || "Failure")}</strong><br>${escapeHtml(attempt.error_message)}</div>` : ""}
          <dl class="row small mb-1"><dt class="col-4">HTTP status</dt><dd class="col-8">${escapeHtml(attempt.http_status ?? "—")}</dd><dt class="col-4">Duration</dt><dd class="col-8">${escapeHtml(attempt.duration_ms)} ms</dd></dl>
          <details><summary>Technical provider response</summary><pre class="safe-json">${escapeHtml(JSON.stringify(attempt.provider_response, null, 2))}</pre></details>
        </article>`).join("") || '<p class="text-muted">No send attempts recorded.</p>';
      document.querySelector("#messageDetailBody").innerHTML = `
        <dl class="row message-detail-list">
          <dt class="col-sm-4">S/N</dt><dd class="col-sm-8">${escapeHtml(data.serial_number || row.dataset.serial)}</dd>
          <dt class="col-sm-4">Campaign</dt><dd class="col-sm-8">${escapeHtml(data.campaign_name)}</dd>
          <dt class="col-sm-4">Original Excel row</dt><dd class="col-sm-8">${escapeHtml(data.original_row_number)}</dd>
          <dt class="col-sm-4">Phone</dt><dd class="col-sm-8">${escapeHtml(data.phone)} <button class="btn btn-sm btn-link copy-phone" data-phone="${escapeHtml(data.phone)}">Copy</button></dd>
          <dt class="col-sm-4">Status</dt><dd class="col-sm-8"><span data-detail-field="state" class="badge state-${escapeHtml(data.state)}">${statusIcon(data.state)} ${escapeHtml(data.state_label)}</span> · <span data-detail-field="explanation">${escapeHtml(data.delivery_explanation)}</span></dd>
          <dt class="col-sm-4">Provider message ID</dt><dd class="col-sm-8 text-break">${escapeHtml(data.provider_message_id || "Not assigned")}</dd>
          <dt class="col-sm-4">Message</dt><dd class="col-sm-8 message-full">${escapeHtml(data.message)}</dd>
          <dt class="col-sm-4">Sent / delivered / read</dt><dd class="col-sm-8"><span data-detail-field="timestamps">${escapeHtml(formatDate(data.sent_at))} / ${escapeHtml(formatDate(data.delivered_at))} / ${escapeHtml(formatDate(data.read_at))}</span></dd>
          <dt class="col-sm-4">Edited / deleted</dt><dd class="col-sm-8">${escapeHtml(formatDate(data.edited_at))} / ${escapeHtml(formatDate(data.deleted_at))}</dd>
          <dt class="col-sm-4">Last checked</dt><dd class="col-sm-8">${escapeHtml(formatDate(data.provider_status_checked_at))}</dd>
          <dt class="col-sm-4">Retries</dt><dd class="col-sm-8">${escapeHtml(data.retry_count)}</dd>
        </dl>
        ${data.failure_reason ? `<div class="alert alert-danger"><strong>Failure reason</strong><br>${escapeHtml(data.failure_reason)}</div>` : ""}
        <h3 class="h6 mt-4">Attempts</h3>${attempts}`;
      return data;
    }

    function patchDetailStatus(patch) {
      const root = document.querySelector("#messageDetailBody"); if (!root) return;
      const badge = root.querySelector('[data-detail-field="state"]'); if (badge) { badge.className = `badge state-${patch.state}`; setTextIfChanged(badge, `${statusIcon(patch.state)} ${patch.state_label}`); }
      setTextIfChanged(root.querySelector('[data-detail-field="explanation"]'), patch.delivery_explanation);
      setTextIfChanged(root.querySelector('[data-detail-field="timestamps"]'), `${formatDate(patch.sent_at)} / ${formatDate(patch.delivered_at)} / ${formatDate(patch.read_at)}`);
    }

    async function postAction(row, action, body = {}) {
      const data = await request(`/messages/${row.dataset.messageId}/${action}/`, {
        method: "POST",
        body: JSON.stringify({ ...body, serial_number: Number(row.dataset.serial) }),
      });
      renderRow(row, data.row);
      toast(data.message || "Message updated.");
      return data;
    }

    rows.addEventListener("click", async (event) => {
      const copy = event.target.closest(".copy-phone");
      if (copy) {
        await navigator.clipboard.writeText(copy.dataset.phone);
        toast("Phone number copied.");
        return;
      }
      const button = event.target.closest("[data-message-action]");
      if (!button || button.disabled) return;
      const row = rowFor(button);
      activeRow = row;
      button.disabled = true;
      try {
        if (button.dataset.messageAction === "detail") await loadDetail(row);
        if (button.dataset.messageAction === "refresh") await postAction(row, "refresh-status");
        if (button.dataset.messageAction === "delete") deleteModal.show();
        if (["update", "resend"].includes(button.dataset.messageAction)) {
          const data = await loadDetail(row, false);
          const form = document.querySelector(button.dataset.messageAction === "update" ? "#messageUpdateForm" : "#messageResendForm");
          form.elements.id.value = data.id;
          form.elements.phone.value = data.phone;
          form.elements.text.value = data.message;
          form.querySelector(".form-error").textContent = "";
          if (button.dataset.messageAction === "update") {
            form.querySelectorAll(".local-phone-field").forEach((field) => field.classList.toggle("d-none", Boolean(data.provider_message_id)));
            updateModal.show();
          } else {
            form.querySelector(".previous-failure").textContent = data.failure_reason || "Previous send failed.";
            resendModal.show();
          }
        }
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
      }
    });

    document.querySelector("#messageDetailBody").addEventListener("click", async (event) => {
      const copy = event.target.closest(".copy-phone");
      if (copy) {
        await navigator.clipboard.writeText(copy.dataset.phone);
        toast("Phone number copied.");
      }
    });

    document.querySelector("#messageUpdateForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter;
      button.disabled = true;
      try {
        await postAction(activeRow, "update", {
          phone: event.target.elements.phone.value,
          text: event.target.elements.text.value,
          update_imported_recipient: event.target.elements.update_imported_recipient.checked,
        });
        updateModal.hide();
      } catch (error) {
        event.target.querySelector(".form-error").textContent = error.message;
      } finally { button.disabled = false; }
    });

    document.querySelector("#confirmMessageDelete").addEventListener("click", async (event) => {
      event.target.disabled = true;
      try {
        await postAction(activeRow, "delete");
        deleteModal.hide();
      } catch (error) { toast(error.message); }
      finally { event.target.disabled = false; }
    });

    document.querySelector("#messageResendForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter;
      button.disabled = true;
      try {
        await postAction(activeRow, "resend", {
          phone: event.target.elements.phone.value,
          text: event.target.elements.text.value,
        });
        resendModal.hide();
      } catch (error) {
        event.target.querySelector(".form-error").textContent = error.message;
        const wait = error.payload?.data?.wait_seconds;
        if (wait) {
          let remaining = wait;
          const node = event.target.querySelector(".resend-countdown");
          const timer = setInterval(() => {
            node.textContent = remaining > 0 ? `Resend available in ${remaining--} seconds.` : "";
            if (remaining < 0) { clearInterval(timer); button.disabled = false; }
          }, 1000);
        }
      } finally {
        if (!event.target.querySelector(".resend-countdown").textContent) button.disabled = false;
      }
    });

    document.querySelector("#refreshVisible")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const visibleRows = [...rows.querySelectorAll("[data-message-id]")];
      const eligible = visibleRows.filter((row) => !row.querySelector('[data-message-action="refresh"]')?.disabled).slice(0, 25);
      const progress = document.querySelector("#refreshProgress");
      button.disabled = true;
      progress.classList.remove("d-none");
      progress.textContent = `0/${eligible.length} refreshed`;
      try {
        const serial_numbers = Object.fromEntries(eligible.map((row) => [row.dataset.messageId, Number(row.dataset.serial)]));
        const data = await request("/messages/refresh-visible-statuses/", {
          method: "POST",
          body: JSON.stringify({ ids: eligible.map((row) => row.dataset.messageId), serial_numbers }),
        });
        data.results.forEach((result, index) => {
          if (result.ok) renderRow(document.querySelector(`#message-${CSS.escape(result.id)}`), result.row);
          progress.textContent = `${index + 1}/${eligible.length} refreshed`;
        });
      } catch (error) { toast(error.message); }
      finally { button.disabled = false; }
    });
    rows.addEventListener("change", (event) => { if (event.target.classList.contains("message-check")) redrawBulk(); });
    document.querySelector("#messageVisibleAll")?.addEventListener("change", (event) => { rows.querySelectorAll(".message-check").forEach((box) => { box.checked = event.target.checked; }); redrawBulk(); });
    document.querySelector("#bulkClear")?.addEventListener("click", () => { rows.querySelectorAll(".message-check").forEach((box) => { box.checked = false; }); redrawBulk(); });
    document.querySelector("#messageSelectPage")?.addEventListener("click", () => { rows.querySelectorAll(".message-check").forEach((box) => { box.checked = true; }); redrawBulk(); });
    async function sequential(action) { const selected = selectedRows(); for (let i = 0; i < selected.length; i += 1) { const row = selected[i]; const actionButton = row.querySelector(`[data-message-action="${action}"]`); if (!actionButton || actionButton.disabled) continue; try { await postAction(row, action === "refresh" ? "refresh-status" : action); row.querySelector(".message-check").checked = false; } catch (error) { toast(`${i + 1}/${selected.length}: ${error.message}`); } } redrawBulk(); }
    document.querySelector("#bulkRefresh")?.addEventListener("click", () => sequential("refresh"));
    document.querySelector("#bulkDelete")?.addEventListener("click", () => sequential("delete"));
    document.querySelector("#bulkResend")?.addEventListener("click", async () => { for (const row of selectedRows()) { const button = row.querySelector('[data-message-action="resend"]'); if (!button) continue; const data = await loadDetail(row, false); try { await postAction(row, "resend", {phone:data.phone, text:data.message}); row.querySelector(".message-check").checked = false; } catch (error) { toast(error.message); } } redrawBulk(); });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { clearTimeout(syncTimer); syncStatus("Paused while tab is hidden"); }
      else { sync(); }
    });
    window.addEventListener("online", () => { syncStatus("Checking…", true); sync(); });
    window.addEventListener("offline", () => { clearTimeout(syncTimer); syncStatus("Offline"); });
    window.addEventListener("pagehide", () => clearTimeout(syncTimer), { once: true });
    sync();
  }

  function messagingSettings() {
    const input = document.querySelector("#defaultInterval"), feedback = document.querySelector("#settingsFeedback");
    const initial = parseIntervalInput(input, { allowBlank: true });
    let selectedInterval = initial.valid ? initial.value : 0;
    const validate = () => {
      const parsed = parseIntervalInput(input, { fallback: selectedInterval, allowBlank: true });
      setIntervalValidity(input, feedback, parsed);
      document.querySelectorAll(".interval-preset").forEach((button) => button.classList.toggle("active", parsed.valid && !parsed.blank && Number(button.dataset.value) === parsed.value));
      return parsed;
    };
    document.querySelectorAll(".interval-preset").forEach((button) => button.onclick = () => { const value = Number(button.dataset.value); input.value = String(value); selectedInterval = value; validate(); });
    input.addEventListener("input", validate); validate();
    document.querySelector("#messagingSettingsForm").onsubmit = async (event) => { event.preventDefault(); const parsed = validate(); if (!parsed.valid) return; const button = event.submitter || event.currentTarget.querySelector('button[type="submit"]'); if (!button || button.disabled) return; button.disabled = true; try { const data = await request("/settings/messaging/save/", {method:"POST", body:JSON.stringify({default_send_interval_seconds:parsed.value, auto_check_whatsapp_after_normalization:document.querySelector("#autoCheck").checked})}); input.value = String(data.default_send_interval_seconds); selectedInterval = data.default_send_interval_seconds; feedback.className="small mt-2 text-success"; feedback.hidden=false; feedback.textContent=`Saved: ${data.default_send_interval_seconds} seconds.`; } catch (error) { const fields = error.payload?.fields || error.payload?.errors || {}; const errors = fields.default_send_interval_seconds; if (errors?.length) setIntervalValidity(input, feedback, {valid:false, message:errors[0]}); else { feedback.className="small mt-2 text-danger"; feedback.hidden=false; feedback.textContent=error.message; } } finally { button.disabled = false; } };
  }
  return { request, toast, uploadForm, dataset, campaignForm, campaign, messageLogs, messagingSettings };
})();
