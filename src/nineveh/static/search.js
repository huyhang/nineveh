// Progressive enhancement for the search page. Everything the page does
// works without this file: the form is an ordinary GET, the filters are
// ordinary checkboxes, and the results are rendered by the server. This adds
// the "/" shortcut everywhere and type-ahead suggestions on the search page.
(() => {
  const SUGGEST_DELAY_MS = 140;
  const MIN_QUERY = 2;

  const input = document.querySelector("[data-search-input]");
  const menu = document.querySelector("[data-search-suggestions]");
  const panel = document.querySelector("[data-search-filter-panel]");

  installShortcut(input);
  collapseFiltersOnNarrowScreens(panel);
  if (input && menu) installSuggestions(input, menu);

  function installShortcut(input) {
    document.addEventListener("keydown", (event) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTyping(document.activeElement)) return;
      event.preventDefault();
      if (input) input.focus();
      else window.location.assign("/search");
    });
  }

  function isTyping(element) {
    if (!element) return false;
    return (
      ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName) ||
      element.isContentEditable
    );
  }

  // The rail is a disclosure on phones and always-open on desktop, so the
  // filters never hide themselves on a screen with room for them.
  function collapseFiltersOnNarrowScreens(panel) {
    if (!panel) return;
    const narrow = window.matchMedia("(max-width: 640px)");
    const apply = (matches) => {
      panel.open = !matches;
    };
    apply(narrow.matches);
    narrow.addEventListener?.("change", (event) => apply(event.matches));
  }

  function installSuggestions(input, menu) {
    let timer = null;
    let controller = null;
    let active = -1;

    const close = () => {
      menu.hidden = true;
      menu.replaceChildren();
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    };

    const highlight = (items, index) => {
      active = Math.max(0, Math.min(index, items.length - 1));
      items.forEach((item, position) => {
        item.setAttribute("aria-selected", String(position === active));
      });
      const selected = items[active];
      if (!selected) return;
      input.setAttribute("aria-activedescendant", selected.id);
      selected.scrollIntoView({ block: "nearest" });
    };

    const render = (suggestions) => {
      menu.replaceChildren();
      suggestions.forEach((suggestion, index) => {
        const item = document.createElement("a");
        item.id = `search-suggestion-${index}`;
        item.href = suggestion.url;
        item.setAttribute("role", "option");
        // textContent, never innerHTML: these strings are library and series
        // names that came from someone's filesystem.
        const title = document.createElement("span");
        title.textContent = suggestion.title;
        const subtitle = document.createElement("small");
        subtitle.textContent = suggestion.subtitle;
        item.append(title, subtitle);
        menu.append(item);
      });
      menu.hidden = suggestions.length === 0;
      input.setAttribute("aria-expanded", String(suggestions.length > 0));
      active = -1;
    };

    input.addEventListener("keydown", (event) => {
      const items = [...menu.querySelectorAll('[role="option"]')];
      if (event.key === "Escape") return close();
      if (!items.length) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        highlight(items, active + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        highlight(items, active <= 0 ? items.length - 1 : active - 1);
      } else if (event.key === "Enter" && active >= 0) {
        event.preventDefault();
        window.location.assign(items[active].href);
      }
    });

    input.addEventListener("input", () => {
      window.clearTimeout(timer);
      controller?.abort();
      const query = input.value.trim();
      if (query.length < MIN_QUERY) return close();
      timer = window.setTimeout(() => fetchSuggestions(query), SUGGEST_DELAY_MS);
    });

    async function fetchSuggestions(query) {
      controller = new AbortController();
      try {
        const response = await fetch(
          `/api/v1/search/suggestions?q=${encodeURIComponent(query)}`,
          { credentials: "same-origin", signal: controller.signal },
        );
        if (!response.ok) return close();
        const { suggestions } = await response.json();
        render(suggestions);
      } catch (error) {
        // An aborted request was replaced by a newer one; leave the menu be.
        if (error.name !== "AbortError") close();
      }
    }

    document.addEventListener("click", (event) => {
      if (!menu.contains(event.target) && event.target !== input) close();
    });
  }
})();
