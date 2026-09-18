for (const form of document.querySelectorAll("form[data-confirm]")) {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
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
