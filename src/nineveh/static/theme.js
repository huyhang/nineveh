(() => {
  const themes = ["system", "light", "paper", "dark"];
  const labels = {
    system: ["◐", "System"],
    light: ["☀", "Light"],
    paper: ["▤", "Paper"],
    dark: ["☾", "Dark"],
  };

  function storedTheme() {
    try {
      const value = window.localStorage.getItem("nineveh-theme");
      return themes.includes(value) ? value : "system";
    } catch {
      return "system";
    }
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const [icon, label] = labels[theme];
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.querySelector(".theme-icon").textContent = icon;
      button.querySelector("[data-theme-label]").textContent = label;
      button.setAttribute("aria-label", `Current theme: ${label}. Switch color theme`);
    });
  }

  const initial = storedTheme();
  applyTheme(initial);
  document.addEventListener("DOMContentLoaded", () => {
    applyTheme(initial);
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        const current = document.documentElement.dataset.theme || "system";
        const next = themes[(themes.indexOf(current) + 1) % themes.length];
        try {
          window.localStorage.setItem("nineveh-theme", next);
        } catch {
          // The theme still applies for this page when storage is unavailable.
        }
        applyTheme(next);
      });
    });
  });
})();
