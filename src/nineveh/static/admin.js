// Destructive actions confirm through an in-page dialog rather than
// `window.confirm`, which cannot be themed and is announced by the browser as
// "<host> says". `<dialog>` keeps the focus trap, Esc-to-cancel and backdrop
// behaviour we would otherwise have to rebuild. Browsers without it fall back
// to the native prompt so the confirmation never silently disappears.
const confirmForms = document.querySelectorAll("form[data-confirm]");

if (confirmForms.length) {
  const supported = typeof HTMLDialogElement === "function";
  const dialog = supported ? buildConfirmDialog() : null;
  let pending = null;

  if (dialog) {
    document.body.append(dialog);
    dialog.addEventListener("close", () => {
      const form = pending;
      pending = null;
      if (form && dialog.returnValue === "confirm") form.submit();
    });
  }

  for (const form of confirmForms) {
    form.addEventListener("submit", (event) => {
      if (!dialog) {
        if (!window.confirm(form.dataset.confirm)) event.preventDefault();
        return;
      }
      if (pending === form) return;
      event.preventDefault();
      pending = form;
      dialog.querySelector("[data-confirm-text]").textContent =
        form.dataset.confirm;
      dialog.querySelector("[data-confirm-accept]").textContent =
        form.querySelector("button")?.textContent?.trim() || "Confirm";
      dialog.returnValue = "cancel";
      dialog.showModal();
      dialog.querySelector("[data-confirm-cancel]").focus();
    });
  }
}

function buildConfirmDialog() {
  const dialog = document.createElement("dialog");
  dialog.className = "confirm-dialog";
  dialog.setAttribute("aria-label", "Confirm this action");

  const form = document.createElement("form");
  form.method = "dialog";

  const text = document.createElement("p");
  text.setAttribute("data-confirm-text", "");

  const actions = document.createElement("div");
  actions.className = "confirm-actions";

  const cancel = document.createElement("button");
  cancel.value = "cancel";
  cancel.className = "secondary";
  cancel.textContent = "Cancel";
  cancel.setAttribute("data-confirm-cancel", "");

  const accept = document.createElement("button");
  accept.value = "confirm";
  accept.className = "danger";
  accept.setAttribute("data-confirm-accept", "");

  actions.append(cancel, accept);
  form.append(text, actions);
  dialog.append(form);
  return dialog;
}

function refreshPermissions(fieldset) {
  const library = fieldset.querySelector('[data-permission="library"]');
  for (const branch of fieldset.querySelectorAll(".permission-branch")) {
    const category = branch.querySelector('[data-permission="category"]');
    category.disabled = library.checked;
    for (const series of branch.querySelectorAll('[data-permission="series"]')) {
      series.disabled = library.checked || category.checked;
    }
  }
}

for (const fieldset of document.querySelectorAll(".permission-library")) {
  fieldset.addEventListener("change", () => refreshPermissions(fieldset));
  refreshPermissions(fieldset);
}

if (document.querySelector("[data-metadata-progress]")) {
  window.setTimeout(() => window.location.reload(), 2000);
}

// A catalog scan runs in the background, so the page that started it is stale
// the moment the scan ends. Poll the readiness endpoint instead of reloading:
// an administrator may be part-way through a form on this page, and a scan of
// a large library can run for minutes.
const SCAN_POLL_MS = 5000;
const SCAN_POLL_FAILURES = 3;

function scanLocks() {
  return [...document.querySelectorAll("[data-scan-lock]")];
}

function applyScanLocks(running) {
  for (const control of scanLocks()) {
    control.disabled = running;
    const label = running
      ? control.dataset.scanBusyLabel
      : control.dataset.scanLabel;
    if (label) control.textContent = label;
  }
}

async function refreshScanStatus() {
  // Re-render from the server rather than rebuilding the sentence here: the
  // relative timestamp and the report already have one authoritative form.
  const status = document.querySelector("[data-scan-status]");
  if (!status) return;
  const response = await fetch(window.location.href, {
    credentials: "same-origin",
    headers: { "X-Requested-With": "fetch" },
  });
  if (!response.ok) return;
  const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
  const fresh = parsed.querySelector("[data-scan-status]");
  if (!fresh) return;
  status.replaceWith(fresh);
}

async function pollScan(failures = 0) {
  let running = true;
  try {
    const response = await fetch("/api/v1/health/ready", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`Readiness returned ${response.status}`);
    running = Boolean((await response.json()).catalog?.running);
  } catch (_error) {
    // A blip should not strand the controls disabled forever, but neither
    // should it clear them while a scan may still be holding the catalog.
    if (failures + 1 >= SCAN_POLL_FAILURES) return;
    window.setTimeout(() => pollScan(failures + 1), SCAN_POLL_MS);
    return;
  }
  applyScanLocks(running);
  if (running) {
    window.setTimeout(() => pollScan(), SCAN_POLL_MS);
    return;
  }
  await refreshScanStatus();
}

if (document.querySelector("[data-scan-lock][disabled]")) {
  window.setTimeout(() => pollScan(), SCAN_POLL_MS);
}
