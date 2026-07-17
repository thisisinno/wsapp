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
          const sheet = prompt(`Choose sheet: ${data.sheets.join(", ")}`, data.sheets[0]);
          if (!sheet) throw new Error("Workbook saved; worksheet selection is required.");
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
    }

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
          <td>${row.whatsapp}</td>
        </tr>`).join("") || '<tr><td colspan="6" class="empty">No matching recipients.</td></tr>';
    }

    $("#normalize").onclick = async () => {
      const column = $("#phoneColumn").value;
      if (!column) return toast("Choose a column.");
      badges(await request(`/uploads/${id}/select-phone-column/`, {
        method: "POST", body: JSON.stringify({ column }),
      }));
      await load();
      toast("Numbers normalized.");
    };
    $("#filter").onchange = (event) => { filter = event.target.value; page = 1; load(); };
    $("#more").onclick = () => { page += 1; load(); };
    $("#recipientRows").onclick = (event) => {
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
    $("#phoneForm").onsubmit = async (event) => {
      event.preventDefault();
      try {
        await request(`/recipients/${$("#recipientId").value}/edit-phone/`, {
          method: "POST", body: JSON.stringify({ phone: $("#phoneValue").value }),
        });
        modal.hide();
        await load();
        toast("Correction saved.");
      } catch (error) {
        $("#phoneError").textContent = error.message;
        $("#phoneValue").classList.add("is-invalid");
      }
    };
    columns();
    load();
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
    document.querySelector("#campaignForm").onsubmit = async (event) => {
      event.preventDefault();
      event.submitter.disabled = true;
      try {
        let media_id = "";
        const file = document.querySelector("#media").files[0];
        if (file) {
          const form = new FormData();
          form.append("file", file);
          media_id = (await request("/media/new/", { method: "POST", body: form })).id;
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
            opt_in_confirmed: document.querySelector("#optin").checked,
          }),
        });
        location.href = data.url;
      } catch (error) {
        toast(error.message);
        event.submitter.disabled = false;
      }
    };
  }

  function campaign(id) {
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
      $("#campaignStatus").textContent = heading;
      $("#progressText").textContent = `${data.progress_text} · ${data.percent}%`;
      const bar = $("#campaignProgressBar");
      bar.style.width = `${data.percent}%`;
      bar.setAttribute("aria-valuenow", processed);
      bar.setAttribute("aria-valuemax", data.sendable_total || 1);
      ["sendable_total", "processed", "accepted", "failed", "remaining", "skipped", "invalid"].forEach((key) => {
        $(`#${key}`).textContent = data[key];
      });
      $("#providerAlert").classList.toggle("d-none", data.provider_configured !== false);
      $("#recipientRows").innerHTML = data.recipients.map((row) => `
        <tr class="${row.state === "processing" || row.sequence === sendingSequence ? "table-primary" : ""}">
          <td>${row.sequence ?? "—"}</td>
          <td>${escapeHtml(row.phone_masked)}</td>
          <td class="preview-cell">${escapeHtml(row.preview)}</td>
          <td><span class="badge state-${escapeHtml(row.state)}">${escapeHtml(row.state_label)}</span></td>
          <td>${new Date(row.updated_at).toLocaleTimeString()}</td>
          <td class="text-danger">${escapeHtml(row.error)}</td>
          <td>${row.state === "failed" ? `<button class="btn btn-sm btn-outline-secondary" data-edit-recipient="${row.recipient_id}">Edit number</button>` : ""}</td>
        </tr>`).join("") || '<tr><td colspan="7" class="empty">No campaign recipients yet.</td></tr>';
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
      if (name === "cancel" && !confirm("Cancel all unsent recipients?")) return;
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
      if (edit) {
        const phone = prompt("Enter the corrected phone number");
        if (phone) {
          try {
            await request(`/recipients/${edit.dataset.editRecipient}/edit-phone/`, {
              method: "POST", body: JSON.stringify({ phone }),
            });
            toast("Number corrected. Use Resend failed to retry.");
            await progress();
          } catch (error) {
            toast(error.message);
          }
        }
        return;
      }
      const button = event.target.closest("[data-action]");
      if (button) action(button.dataset.action, button);
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) progress();
    });
    progress();
    pollTimer = setInterval(progress, 5000);
  }

  function escapeHtml(value) {
    const node = document.createElement("div");
    node.textContent = value ?? "";
    return node.innerHTML;
  }

  return { request, toast, uploadForm, dataset, campaignForm, campaign };
})();
