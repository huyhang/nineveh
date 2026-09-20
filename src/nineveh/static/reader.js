import {
  READING_MODES,
  adjacentPage,
  clampPage,
  groupForPage,
  isStitched,
  navigationDelta,
  pageGroups,
  visiblePages,
} from "/static/reader-model.js";

// "auto" follows the publication's category; the other two are the reader's
// explicit override, remembered per browser.
const DIRECTION_CYCLE = ["auto", "ltr", "rtl"];
const DIRECTION_LABELS = { auto: "Auto", ltr: "LTR", rtl: "RTL" };

class ReaderController {
  constructor(root, dependencies = {}) {
    this.root = root;
    this.fetch = dependencies.fetch || window.fetch.bind(window);
    this.storage = dependencies.storage || window.localStorage;
    this.history = dependencies.history || window.history;
    this.pages = new Map();
    this.allPagesPromise = null;
    this.observer = null;
    this.endObserver = null;
    this.saveTimer = null;
    this.retryTimer = null;
    this.chromeTimer = null;
    this.renderVersion = 0;
    this.pointerStart = null;
    this.chromeLocked = false;
    this.forcePair = false;
    this.finished = false;
    this.visiblePageNumbers = [];
    this.elements = this.findElements();
    this.portraitPhone = window.matchMedia(
      "(max-width: 640px) and (orientation: portrait)",
    );
    this.direction = this.readLocalDirection();
    this.state = this.initialState();
  }

  findElements() {
    const find = (selector) => this.root.querySelector(selector);
    return {
      pages: find("[data-reader-pages]"),
      loading: find("[data-reader-loading]"),
      error: find("[data-reader-error]"),
      errorMessage: find("[data-reader-error-message]"),
      retry: find("[data-reader-retry]"),
      previous: find("[data-previous]"),
      next: find("[data-next]"),
      endCard: find("[data-end-card]"),
      readAgain: find("[data-read-again]"),
      slider: find("[data-page-slider]"),
      output: find("[data-page-output]"),
      sync: find("[data-sync-status]"),
      adaptiveNote: find("[data-adaptive-note]"),
      adaptiveMessage: find("[data-adaptive-message]"),
      forcePair: find("[data-force-pair]"),
      savedMessage: find("[data-saved-message]"),
      picker: find("[data-publication-picker]"),
      direction: find("[data-direction-toggle]"),
      fullscreen: find("[data-fullscreen]"),
      stage: find("[data-reader-stage]"),
      modes: [...this.root.querySelectorAll("[data-mode]")],
    };
  }

  initialState() {
    const totalPages = Number(this.root.dataset.pageCount);
    const server = {
      page: clampPage(this.root.dataset.initialPage, totalPages),
      mode: this.validMode(this.root.dataset.initialMode),
      completed: this.root.dataset.completed === "true",
      pageUpdatedAt: Number(this.root.dataset.progressUpdatedAt),
      modeUpdatedAt: Number(this.root.dataset.modeUpdatedAt),
    };
    const local = this.readLocalProgress();
    const preference = this.readLocalMode();
    const state = { ...server };
    const localPageWins = (
      this.root.dataset.explicitPage !== "true"
      && local
      && local.updatedAt > server.pageUpdatedAt
    );
    if (localPageWins) {
      state.page = local.page;
      state.completed = Boolean(local.completed);
    }
    const localModeWins = (
      this.root.dataset.explicitMode !== "true"
      && preference
      && preference.updatedAt > server.modeUpdatedAt
    );
    if (localModeWins) {
      state.mode = this.validMode(preference.mode);
    }
    this.needsSync = Boolean(localPageWins || localModeWins);
    state.page = clampPage(state.page, totalPages);
    return state;
  }

  validMode(mode) {
    return READING_MODES.includes(mode) ? mode : "single";
  }

  get storageKey() {
    return `nineveh-reader:${this.root.dataset.userId}:${this.root.dataset.publicationId}`;
  }

  get modeStorageKey() {
    return `nineveh-reader-mode:${this.root.dataset.userId}`;
  }

  get directionStorageKey() {
    return `nineveh-reader-direction:${this.root.dataset.userId}`;
  }

  get defaultDirection() {
    return this.root.dataset.defaultDirection === "rtl" ? "rtl" : "ltr";
  }

  get readingDirection() {
    return this.direction === "auto" ? this.defaultDirection : this.direction;
  }

  readLocalDirection() {
    const saved = this.readStorage(this.directionStorageKey);
    return DIRECTION_CYCLE.includes(saved?.direction) ? saved.direction : "auto";
  }

  cycleDirection() {
    const next = DIRECTION_CYCLE.indexOf(this.direction) + 1;
    this.direction = DIRECTION_CYCLE[next % DIRECTION_CYCLE.length];
    try {
      this.storage.setItem(
        this.directionStorageKey,
        JSON.stringify({ direction: this.direction }),
      );
    } catch (_error) {
      // The override still applies to this tab when storage is unavailable.
    }
    this.applyDirection();
    return this.render();
  }

  applyDirection() {
    const effective = this.readingDirection;
    const explicit = this.direction !== "auto";
    const spelled = effective === "rtl" ? "right to left" : "left to right";
    this.root.dataset.readingDirection = effective;
    this.root.classList.toggle("direction-rtl", effective === "rtl");
    this.root.classList.toggle("direction-ltr", effective !== "rtl");
    this.elements.slider.dir = effective;
    this.elements.direction.textContent = DIRECTION_LABELS[this.direction];
    this.elements.direction.dataset.explicit = String(explicit);
    const label = explicit
      ? `Reading ${spelled}. Change reading direction.`
      : `Reading ${spelled}, matching this publication. Change reading direction.`;
    this.elements.direction.title = label;
    this.elements.direction.setAttribute("aria-label", label);
  }

  readStorage(key) {
    try {
      return JSON.parse(this.storage.getItem(key));
    } catch (_error) {
      return null;
    }
  }

  readLocalProgress() {
    return this.readStorage(this.storageKey);
  }

  readLocalMode() {
    return this.readStorage(this.modeStorageKey);
  }

  async start() {
    this.bindEvents();
    this.applyDirection();
    this.updateControls();
    try {
      await this.ensurePage(this.state.page);
      await this.render();
      if (this.state.mode === "single") this.prefetchAdjacent();
      this.scheduleSave(this.needsSync);
    } catch (error) {
      this.showError(error);
    }
  }

  bindEvents() {
    this.elements.previous.addEventListener("click", () =>
      this.perform(() => this.move(-1)),
    );
    this.elements.next.addEventListener("click", () =>
      this.perform(() => this.move(1)),
    );
    this.elements.retry.addEventListener("click", () =>
      this.perform(() => this.render()),
    );
    this.elements.readAgain.addEventListener("click", () =>
      this.perform(() => this.readAgain()),
    );
    this.elements.fullscreen.addEventListener("click", () =>
      this.perform(() => this.toggleFullscreen()),
    );
    this.elements.direction.addEventListener("click", () =>
      this.perform(() => this.cycleDirection()),
    );
    this.elements.forcePair.addEventListener("click", () => {
      this.forcePair = true;
      this.perform(() => this.render());
    });
    this.elements.picker.addEventListener("change", (event) => {
      window.location.assign(event.target.value);
    });
    for (const button of this.elements.modes) {
      button.addEventListener("click", () =>
        this.perform(() => this.setMode(button.dataset.mode)),
      );
    }
    this.elements.slider.addEventListener("input", (event) => {
      this.elements.output.value = `${event.target.value} / ${this.totalPages}`;
    });
    this.elements.slider.addEventListener("change", (event) => {
      this.perform(() => this.goToPage(Number(event.target.value), true));
    });
    document.addEventListener("keydown", (event) => this.onKeydown(event));
    this.elements.stage.addEventListener("pointerdown", (event) =>
      this.onPointerDown(event),
    );
    this.elements.stage.addEventListener("pointerup", (event) =>
      this.onPointerUp(event),
    );
    this.elements.stage.addEventListener("pointercancel", () => {
      this.pointerStart = null;
    });
    this.elements.stage.addEventListener("click", (event) => this.onStageClick(event));
    document.addEventListener("pointermove", (event) => {
      if (event.pointerType === "mouse") this.showChrome();
    });
    document.addEventListener("focusin", () => this.revealChrome());
    this.portraitPhone.addEventListener("change", () => {
      this.forcePair = false;
      if (this.state.mode === "double") this.perform(() => this.render());
    });
    window.addEventListener("online", () => this.saveRemote());
    window.addEventListener("pagehide", () => this.saveRemote(true));
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") this.saveRemote(true);
    });
  }

  perform(action) {
    Promise.resolve()
      .then(action)
      .catch((error) => this.showError(error));
  }

  get totalPages() {
    return Number(this.root.dataset.pageCount);
  }

  get manifestUrl() {
    return `/api/v1/publications/${encodeURIComponent(this.root.dataset.publicationId)}/pages`;
  }

  async fetchManifest(url) {
    const response = await this.fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("Your session has expired.");
    }
    if (!response.ok) {
      const message = response.status === 409
        ? "The archive changed. Reload the reader to continue."
        : `The server returned ${response.status}.`;
      throw new Error(message);
    }
    const manifest = await response.json();
    for (const page of manifest.pages) this.pages.set(page.number, page);
    return manifest;
  }

  async ensurePage(number) {
    if (this.pages.has(number)) return this.pages.get(number);
    const url = `${this.manifestUrl}?start=${number}&end=${number}`;
    await this.fetchManifest(url);
    return this.pages.get(number);
  }

  ensureAllPages() {
    if (!this.allPagesPromise) {
      this.allPagesPromise = this.loadAllPages().catch((error) => {
        this.allPagesPromise = null;
        throw error;
      });
    }
    return this.allPagesPromise;
  }

  async loadAllPages() {
    let next = `${this.manifestUrl}?start=1`;
    while (next) {
      const manifest = await this.fetchManifest(next);
      next = manifest.next || null;
    }
    return this.orderedPages();
  }

  orderedPages() {
    return [...this.pages.values()].sort(
      (first, second) => first.number - second.number,
    );
  }

  isAdaptiveSingle() {
    return (
      this.state.mode === "double"
      && this.portraitPhone.matches
      && !this.forcePair
    );
  }

  async render() {
    const version = ++this.renderVersion;
    this.hideError();
    this.elements.loading.hidden = false;
    if (this.state.mode !== "single") await this.ensureAllPages();
    else await this.ensurePage(this.state.page);
    if (version !== this.renderVersion) return;
    this.root.classList.toggle("mode-scroll", this.state.mode === "scroll");
    document.body.classList.toggle(
      "reader-scroll-active",
      this.state.mode === "scroll",
    );
    if (this.state.mode === "scroll") this.renderContinuous();
    else {
      window.scrollTo({ top: 0 });
      this.renderPaged();
    }
    this.elements.loading.hidden = true;
    this.updateControls();
    this.updateLocation();
    this.showChrome();
  }

  renderPaged() {
    this.disconnectObservers();
    const pages = this.orderedPages();
    const visible = visiblePages(
      this.state.mode,
      pages,
      this.state.page,
      this.isAdaptiveSingle(),
    );
    if (this.state.mode === "double" && !this.isAdaptiveSingle() && visible.length) {
      this.state.page = visible[0].number;
    }
    const spread = document.createElement("div");
    spread.className = "reader-spread";
    spread.classList.toggle("is-pair", visible.length === 2);
    spread.classList.toggle(
      "is-stitched",
      visible.length === 1 && isStitched(visible[0]),
    );
    for (const page of visible) spread.append(this.pageFigure(page, false));
    this.visiblePageNumbers = visible.map((page) => page.number);
    this.elements.pages.replaceChildren(spread);
    this.elements.pages.hidden = this.finished;
    this.elements.endCard.hidden = !this.finished;
    const group = groupForPage(pageGroups(pages), this.state.page);
    const adaptedPair = this.isAdaptiveSingle() && group && group.length === 2;
    const stitchedOnPortrait = this.portraitPhone.matches
      && visible.length === 1
      && isStitched(visible[0]);
    this.elements.adaptiveNote.hidden = !(adaptedPair || stitchedOnPortrait);
    this.elements.forcePair.hidden = !adaptedPair;
    this.elements.adaptiveMessage.textContent = stitchedOnPortrait
      ? "Wide stitched spread. Rotate or zoom for a closer view."
      : "Showing one page for readability. Rotate your device to restore the pair.";
    this.prefetchAdjacent();
  }

  renderContinuous() {
    this.disconnectObservers();
    this.finished = false;
    const fragment = document.createDocumentFragment();
    for (const page of this.orderedPages()) {
      const spread = document.createElement("div");
      spread.className = "reader-spread";
      spread.classList.toggle("is-stitched", isStitched(page));
      spread.dataset.page = String(page.number);
      spread.append(this.pageFigure(page, page.number !== this.state.page));
      fragment.append(spread);
    }
    this.elements.pages.replaceChildren(fragment);
    this.elements.pages.hidden = false;
    this.elements.endCard.hidden = false;
    this.elements.adaptiveNote.hidden = true;
    this.visiblePageNumbers = [this.state.page];
    this.observeContinuousPages();
    requestAnimationFrame(() => {
      this.elements.pages
        .querySelector(`[data-page="${this.state.page}"]`)
        ?.scrollIntoView({ block: "start" });
    });
  }

  pageFigure(page, lazy) {
    const figure = document.createElement("figure");
    figure.className = "reader-page";
    figure.dataset.pageNumber = String(page.number);
    const image = document.createElement("img");
    image.src = page.href;
    image.alt = `Page ${page.number} of ${this.totalPages}`;
    // Without this the browser starts its own image drag, which fires
    // `pointercancel` and swallows the swipe before it can turn the page.
    image.draggable = false;
    image.loading = lazy ? "lazy" : "eager";
    image.decoding = "async";
    if (page.width && page.height) {
      image.width = page.width;
      image.height = page.height;
      // An explicit ratio is what lets a width limit shrink the height, and a
      // height limit shrink the width, instead of one of them letterboxing.
      image.style.aspectRatio = `${page.width} / ${page.height}`;
    }
    image.addEventListener("error", () =>
      this.showError(new Error(`Page ${page.number} could not be loaded.`)),
    );
    figure.append(image);
    return figure;
  }

  observeContinuousPages() {
    this.observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.find((entry) => entry.isIntersecting);
        if (!visible) return;
        const page = Number(visible.target.dataset.page);
        if (page !== this.state.page) this.recordPage(page);
      },
      { rootMargin: "-46% 0px -46% 0px", threshold: 0 },
    );
    for (const element of this.elements.pages.querySelectorAll("[data-page]")) {
      this.observer.observe(element);
    }
    this.endObserver = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) this.completeReading();
      },
      { threshold: 0.55 },
    );
    this.endObserver.observe(this.elements.endCard);
  }

  disconnectObservers() {
    this.observer?.disconnect();
    this.endObserver?.disconnect();
    this.observer = null;
    this.endObserver = null;
  }

  async move(direction) {
    if (this.finished) {
      if (direction < 0) {
        this.finished = false;
        await this.render();
      }
      return;
    }
    if (this.state.mode !== "single") await this.ensureAllPages();
    const target = adjacentPage(
      this.state.mode,
      this.orderedPages(),
      this.state.page,
      direction,
      this.isAdaptiveSingle(),
      this.totalPages,
    );
    if (target === null) {
      if (direction > 0) this.showEndCard();
      return;
    }
    await this.goToPage(target, true);
  }

  setPage(page) {
    this.state.page = clampPage(page, this.totalPages);
    // Leaving the last page un-completes the volume. The server refuses a
    // completed flag on any other page, and that rejection can never clear
    // itself — every later save for this publication would fail too.
    if (this.state.page !== this.totalPages) this.state.completed = false;
  }

  async goToPage(page, smooth = false) {
    this.setPage(page);
    this.finished = false;
    if (this.state.mode === "scroll") {
      await this.ensureAllPages();
      const target = this.elements.pages.querySelector(
        `[data-page="${this.state.page}"]`,
      );
      target?.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
      this.recordPage(this.state.page);
      return;
    }
    await this.render();
    this.scheduleSave();
  }

  async setMode(mode) {
    const nextMode = this.validMode(mode);
    if (nextMode === this.state.mode) return;
    this.state.mode = nextMode;
    this.finished = false;
    await this.render();
    this.scheduleSave();
  }

  showEndCard() {
    this.finished = true;
    this.state.page = this.totalPages;
    this.elements.pages.hidden = true;
    this.elements.endCard.hidden = false;
    this.elements.adaptiveNote.hidden = true;
    this.elements.savedMessage.textContent = this.state.completed
      ? "Your progress has been saved."
      : "Saving your progress…";
    this.completeReading();
    this.updateControls();
    this.elements.endCard.focus();
  }

  completeReading() {
    if (this.state.completed) return;
    this.state.completed = true;
    this.state.page = this.totalPages;
    this.updateControls();
    this.scheduleSave(true);
  }

  async readAgain() {
    this.finished = false;
    this.state.completed = false;
    this.state.page = 1;
    await this.render();
    if (this.state.mode === "scroll") {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
    this.scheduleSave(true);
  }

  recordPage(page) {
    this.setPage(page);
    this.visiblePageNumbers = [this.state.page];
    this.updateControls();
    this.updateLocation();
    this.scheduleSave();
  }

  updateControls() {
    for (const button of this.elements.modes) {
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.mode === this.state.mode),
      );
    }
    const visible = this.visiblePageNumbers.length
      ? this.visiblePageNumbers
      : [this.state.page];
    const pageLabel = visible.length > 1
      ? `${visible[0]}–${visible[visible.length - 1]}`
      : String(visible[0]);
    this.elements.output.value = `${pageLabel} / ${this.totalPages}`;
    this.elements.slider.value = String(this.state.page);
    this.elements.previous.disabled = !this.finished && this.state.page <= 1;
    this.elements.next.disabled = this.finished;
  }

  updateLocation() {
    const url = new URL(window.location.href);
    url.searchParams.set("page", String(this.state.page));
    url.searchParams.set("mode", this.state.mode);
    this.history.replaceState(null, "", url);
  }

  persistLocal(updatedAt = Date.now()) {
    const saved = {
      page: this.state.page,
      mode: this.state.mode,
      completed: this.state.completed,
      updatedAt,
    };
    try {
      this.storage.setItem(this.storageKey, JSON.stringify(saved));
      this.storage.setItem(
        this.modeStorageKey,
        JSON.stringify({ mode: saved.mode, updatedAt: saved.updatedAt }),
      );
    } catch (_error) {
      // Private browsing or a full quota must never stop the reader.
    }
  }

  scheduleSave(immediate = false) {
    this.persistLocal();
    this.elements.sync.textContent = "Saving…";
    window.clearTimeout(this.saveTimer);
    this.saveTimer = window.setTimeout(
      () => this.saveRemote(),
      immediate ? 0 : 800,
    );
  }

  async saveRemote(keepalive = false) {
    window.clearTimeout(this.saveTimer);
    window.clearTimeout(this.retryTimer);
    let retryable = true;
    try {
      const response = await this.fetch(
        `/api/v1/publications/${encodeURIComponent(
          this.root.dataset.publicationId,
        )}/progress`,
        {
          method: "PUT",
          credentials: "same-origin",
          keepalive,
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.root.dataset.csrfToken,
          },
          body: JSON.stringify({
            page: this.state.page,
            mode: this.state.mode,
            completed: this.state.completed,
          }),
        },
      );
      if (!response.ok) {
        // A refused payload stays refused; only retry a server-side failure.
        retryable = response.status >= 500;
        throw new Error(`Progress sync returned ${response.status}`);
      }
      const saved = await response.json();
      this.persistLocal(Date.parse(saved.updatedAt));
      this.elements.sync.textContent = "Saved";
      this.elements.savedMessage.textContent = "Your progress has been saved.";
    } catch (_error) {
      this.elements.sync.textContent = "Saved locally";
      this.elements.savedMessage.textContent =
        "Progress is saved on this device and will sync when the connection returns.";
      if (retryable && !keepalive) {
        this.retryTimer = window.setTimeout(() => this.saveRemote(), 5000);
      }
    }
  }

  prefetchAdjacent() {
    for (const number of [this.state.page - 1, this.state.page + 1]) {
      if (number < 1 || number > this.totalPages) continue;
      this.ensurePage(number)
        .then((page) => {
          if (page) new Image().src = page.href;
        })
        // Prefetching is an optimisation. A neighbour that cannot be fetched
        // will report itself when the reader actually navigates to it, and
        // surfacing it here would fire an error over a page being read fine.
        .catch(() => {});
    }
  }

  onKeydown(event) {
    const isPageSlider = event.target === this.elements.slider;
    if (!isPageSlider && event.target.matches("input, select, textarea, button")) return;
    const movement = navigationDelta(
      event.key,
      this.root.dataset.readingDirection,
    );
    if (movement !== null) {
      event.preventDefault();
      this.perform(() => this.move(movement));
    } else if (!isPageSlider && event.key.toLowerCase() === "f") {
      this.perform(() => this.toggleFullscreen());
    }
  }

  onPointerDown(event) {
    if (!event.isPrimary) {
      this.pointerStart = null;
      return;
    }
    if (this.state.mode === "scroll" || event.target.closest("button, a, input")) return;
    this.pointerStart = { x: event.clientX, y: event.clientY };
  }

  onPointerUp(event) {
    if (!this.pointerStart || this.state.mode === "scroll") return;
    const horizontal = event.clientX - this.pointerStart.x;
    const vertical = event.clientY - this.pointerStart.y;
    this.pointerStart = null;
    if (Math.abs(horizontal) < 55 || Math.abs(horizontal) < Math.abs(vertical)) return;
    // Dragging the page the way it is bound: leftwards advances a comic,
    // rightwards advances manga, matching the arrow keys and the chevrons.
    const forward =
      this.readingDirection === "rtl" ? horizontal > 0 : horizontal < 0;
    this.perform(() => this.move(forward ? 1 : -1));
  }

  onStageClick(event) {
    if (event.target.closest("button, a, input") || this.state.mode === "scroll") return;
    this.chromeLocked = !this.chromeLocked;
    if (this.chromeLocked) this.hideChrome();
    else this.showChrome();
  }

  hideChrome() {
    window.clearTimeout(this.chromeTimer);
    this.root.classList.add("chrome-hidden");
  }

  showChrome(scheduleHide = true) {
    // Asking for the chrome to go away has to outlast the next mouse twitch,
    // or the page grows and shrinks again before the reader can read it.
    if (this.chromeLocked) return;
    this.root.classList.remove("chrome-hidden");
    window.clearTimeout(this.chromeTimer);
    if (scheduleHide && this.state.mode !== "scroll") {
      this.chromeTimer = window.setTimeout(
        () => this.root.classList.add("chrome-hidden"),
        3200,
      );
    }
  }

  revealChrome() {
    // Focus reaching a control the reader cannot see overrides their request.
    this.chromeLocked = false;
    this.showChrome(false);
  }

  async toggleFullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch (_error) {
      // Fullscreen can be disabled by the browser or an embedding policy.
    }
  }

  showError(error) {
    this.elements.loading.hidden = true;
    this.elements.error.hidden = false;
    this.elements.errorMessage.textContent = error.message || "Please try again.";
  }

  hideError() {
    this.elements.error.hidden = true;
  }
}

const root = document.querySelector("[data-reader]");
if (root) new ReaderController(root).start();

export { ReaderController };
