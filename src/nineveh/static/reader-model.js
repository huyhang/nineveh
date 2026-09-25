export const READING_MODES = Object.freeze(["single", "double", "scroll"]);

export function readerModeStorageKey(userId, seriesId) {
  return `nineveh-reader-mode:${userId}:${seriesId}`;
}

export function navigationDelta(key, readingDirection = "ltr") {
  const forwardArrow = readingDirection === "rtl" ? "ArrowLeft" : "ArrowRight";
  const backwardArrow = readingDirection === "rtl" ? "ArrowRight" : "ArrowLeft";
  if ([forwardArrow, "PageDown", " "].includes(key)) return 1;
  if ([backwardArrow, "PageUp"].includes(key)) return -1;
  return null;
}

export function clampPage(page, totalPages) {
  const value = Number.isFinite(Number(page)) ? Number(page) : 1;
  return Math.min(totalPages, Math.max(1, Math.trunc(value)));
}

export function isStitched(page) {
  if (page.spread) return true;
  if (!page.width || !page.height) return false;
  return page.width / page.height >= 1.25;
}

// Anchors arrive from a manifest, so anything unusable has to fall back to
// pairing straight after the cover rather than shifting the whole volume.
function pairingStart(pairingAnchor) {
  const anchor = Math.trunc(Number(pairingAnchor));
  return Number.isFinite(anchor) && anchor > 2 ? anchor : 2;
}

// Pages between the cover and the anchor, paired from the anchor backwards so
// the run finishes flush against it. Any page left over lands at the front,
// which is where a colour insert or a frontispiece actually sits.
function leadingGroups(pages) {
  const groups = [];
  let index = pages.length - 1;
  while (index >= 0) {
    const page = pages[index];
    const previous = pages[index - 1];
    if (isStitched(page) || !previous || isStitched(previous)) {
      groups.push([page]);
      index -= 1;
    } else {
      groups.push([previous, page]);
      index -= 2;
    }
  }
  return groups.reverse();
}

export function pageGroups(pages, pairingAnchor = null) {
  const ordered = [...pages].sort((first, second) => first.number - second.number);
  if (!ordered.length) return [];
  const anchor = pairingStart(pairingAnchor);
  let index = 1;
  while (index < ordered.length && ordered[index].number < anchor) index += 1;
  // The cover always stands alone. The anchor only sets the *parity* of what
  // follows: pages before it still pair, they just align backwards from it, so
  // one stray page early on does not desynchronise every later spread.
  const groups = [[ordered[0]], ...leadingGroups(ordered.slice(1, index))];
  while (index < ordered.length) {
    const page = ordered[index];
    const next = ordered[index + 1];
    if (isStitched(page) || !next || isStitched(next)) {
      groups.push([page]);
      index += 1;
    } else {
      groups.push([page, next]);
      index += 2;
    }
  }
  return groups;
}

export function groupForPage(groups, pageNumber) {
  return groups.find((group) => group.some((page) => page.number === pageNumber));
}

export function visiblePages(
  mode,
  pages,
  pageNumber,
  adaptiveSingle = false,
  pairingAnchor = null,
) {
  const page = pages.find((item) => item.number === pageNumber) || pages[0];
  if (!page || mode !== "double") return page ? [page] : [];
  const group = groupForPage(pageGroups(pages, pairingAnchor), page.number) || [page];
  return adaptiveSingle && group.length > 1 ? [page] : group;
}

export function adjacentPage(
  mode,
  pages,
  pageNumber,
  direction,
  adaptiveSingle = false,
  totalPages = pages.length,
  pairingAnchor = null,
) {
  if (!pages.length) return null;
  if (mode !== "double" || adaptiveSingle) {
    const target = pageNumber + direction;
    return target < 1 || target > totalPages ? null : target;
  }
  const groups = pageGroups(pages, pairingAnchor);
  const current = groups.findIndex((group) =>
    group.some((page) => page.number === pageNumber),
  );
  const target = current + direction;
  return target < 0 || target >= groups.length ? null : groups[target][0].number;
}

export function activeImageNumbers(pageNumber, totalPages, before = 2, after = 3) {
  const current = clampPage(pageNumber, totalPages);
  const first = Math.max(1, current - before);
  const last = Math.min(totalPages, current + after);
  return new Set(Array.from({ length: last - first + 1 }, (_, index) => first + index));
}
