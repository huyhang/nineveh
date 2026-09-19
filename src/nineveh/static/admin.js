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
