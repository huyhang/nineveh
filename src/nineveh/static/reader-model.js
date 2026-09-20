export const READING_MODES = Object.freeze(["single", "double", "scroll"]);

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

export function pageGroups(pages) {
  const ordered = [...pages].sort((first, second) => first.number - second.number);
  if (!ordered.length) return [];
  const groups = [[ordered[0]]]; // The cover always stands alone.
  for (let index = 1; index < ordered.length;) {
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

export function visiblePages(mode, pages, pageNumber, adaptiveSingle = false) {
  const page = pages.find((item) => item.number === pageNumber) || pages[0];
  if (!page || mode !== "double") return page ? [page] : [];
  const group = groupForPage(pageGroups(pages), page.number) || [page];
  return adaptiveSingle && group.length > 1 ? [page] : group;
}

export function adjacentPage(
  mode,
  pages,
  pageNumber,
  direction,
  adaptiveSingle = false,
  totalPages = pages.length,
) {
  if (!pages.length) return null;
  if (mode !== "double" || adaptiveSingle) {
    const target = pageNumber + direction;
    return target < 1 || target > totalPages ? null : target;
  }
  const groups = pageGroups(pages);
  const current = groups.findIndex((group) =>
    group.some((page) => page.number === pageNumber),
  );
  const target = current + direction;
  return target < 0 || target >= groups.length ? null : groups[target][0].number;
}
