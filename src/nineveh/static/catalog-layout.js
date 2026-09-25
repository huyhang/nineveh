// Loaded in <head>, like theme.js, so the remembered layout is on <html>
// before the series grid is parsed and the page never paints the default first.
(() => {
  const layouts = ["default", "dense", "list"];
  const storageKey = "nineveh-catalog-layout";

  function storedLayout() {
    try {
      const value = window.localStorage.getItem(storageKey);
      return layouts.includes(value) ? value : "default";
    } catch {
      return "default";
    }
  }

  function applyLayout(layout) {
    document.documentElement.dataset.catalogLayout = layout;
    document.querySelectorAll("[data-layout-option]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.layoutOption === layout));
    });
  }

  applyLayout(storedLayout());
  document.addEventListener("DOMContentLoaded", () => {
    applyLayout(storedLayout());
    document.querySelectorAll("[data-layout-option]").forEach((button) => {
      button.addEventListener("click", () => {
        const layout = layouts.includes(button.dataset.layoutOption)
          ? button.dataset.layoutOption
          : "default";
        try {
          window.localStorage.setItem(storageKey, layout);
        } catch {
          // The selection still applies for this page when storage is unavailable.
        }
        applyLayout(layout);
      });
    });
  });
})();
