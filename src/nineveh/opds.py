from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlencode

from .domain import Publication

OPDS_MEDIA_TYPE = "application/opds+json"
CBZ_MEDIA_TYPE = "application/vnd.comicbook+zip"
ACQUISITION_REL = "http://opds-spec.org/acquisition"
AUTHENTICATION_REL = "http://opds-spec.org/auth/document"

# Nineveh extensions. URNs rather than URLs: these name a relation, they are not
# meant to resolve.
PAGE_MANIFEST_REL = "urn:nineveh:rel:page-manifest"
PAGE_RANGE_REL = "urn:nineveh:rel:page-range"

CATALOG_PATH = "/opds/v2/catalog.json"
NAVIGATION_PATH = "/opds/v2/navigation.json"
PUBLICATIONS_PATH = "/opds/v2/publications.json"
AUTHENTICATION_PATH = "/opds/v2/authentication.json"


class OpdsBuilder:
    def __init__(self, title: str) -> None:
        self._title = title

    def authentication_document(self, base_url: str) -> dict[str, object]:
        return {
            "id": f"{base_url}{AUTHENTICATION_PATH}",
            "title": f"{self._title} authentication",
            "description": "Sign in with a Nineveh username and password.",
            "authentication": [
                {
                    "type": "http://opds-spec.org/auth/basic",
                    "labels": {"login": "Username", "password": "Password"},
                }
            ],
            "links": [
                {
                    "rel": "logo",
                    "href": f"{base_url}/static/icon.svg",
                    "type": "image/svg+xml",
                }
            ],
        }

    def root_feed(
        self, base_url: str, libraries: list[tuple[str, int]], modified: str
    ) -> dict[str, object]:
        return {
            "metadata": {
                "title": self._title,
                "modified": modified,
                "numberOfItems": len(libraries),
            },
            "links": [
                _link("self", f"{base_url}{CATALOG_PATH}"),
                {
                    "rel": AUTHENTICATION_REL,
                    "href": f"{base_url}{AUTHENTICATION_PATH}",
                    "type": "application/opds-authentication+json",
                },
                {
                    "rel": "search",
                    "href": f"{base_url}{PUBLICATIONS_PATH}?q={{searchTerms}}",
                    "type": OPDS_MEDIA_TYPE,
                    "templated": True,
                },
            ],
            "navigation": [
                _navigation_entry(
                    library, count, url(base_url, NAVIGATION_PATH, {"library": library})
                )
                for library, count in libraries
            ],
        }

    def navigation_feed(
        self,
        base_url: str,
        *,
        title: str,
        parameters: dict[str, str],
        entries: list[tuple[str, int, str]],
        modified: str,
    ) -> dict[str, object]:
        return {
            "metadata": {
                "title": f"{self._title} — {title}",
                "modified": modified,
                "numberOfItems": len(entries),
            },
            "links": [
                _link("self", url(base_url, NAVIGATION_PATH, parameters)),
                _link("start", f"{base_url}{CATALOG_PATH}"),
            ],
            "navigation": [
                _navigation_entry(name, count, href) for name, count, href in entries
            ],
        }

    def publication_feed(
        self,
        base_url: str,
        publications: list[Publication],
        *,
        total: int,
        page: int,
        page_size: int,
        library: str | None,
        category: str | None,
        series: str | None,
        query: str | None,
        modified: str,
    ) -> dict[str, object]:
        filters = _filters(library=library, category=category, series=series, q=query)
        title = " — ".join(
            part for part in (self._title, library, category, series) if part
        )
        return {
            "metadata": {
                "title": title,
                "modified": modified,
                "numberOfItems": total,
                "itemsPerPage": page_size,
                "currentPage": page,
            },
            "links": _feed_links(base_url, filters, page, page_size, total),
            "publications": [self.publication(base_url, item) for item in publications],
        }

    @staticmethod
    def publication(base_url: str, item: Publication) -> dict[str, object]:
        return {
            "metadata": _publication_metadata(item),
            "links": _publication_links(base_url, item),
            "images": [
                {
                    "href": (
                        f"{base_url}/api/v1/publications/{item.id}/cover"
                        f"?width=640&revision={item.revision}"
                    ),
                    "type": "image/webp",
                    "width": 640,
                }
            ],
        }


def _publication_metadata(item: Publication) -> dict[str, object]:
    metadata: dict[str, object] = {
        "@type": "http://schema.org/ComicStory",
        "identifier": f"urn:uuid:{item.id}",
        "title": item.title,
        "modified": datetime.fromtimestamp(
            item.modified_ns / 1_000_000_000, UTC
        ).isoformat(),
        "numberOfPages": item.page_count,
        "belongsTo": {"series": [{"name": item.series}]},
    }
    if item.number:
        metadata["position"] = item.number
    if item.description:
        metadata["description"] = item.description
    if item.authors:
        metadata["author"] = [{"name": author} for author in item.authors]
    return metadata


def _publication_links(base_url: str, item: Publication) -> list[dict[str, object]]:
    root = f"{base_url}/api/v1/publications/{item.id}"
    return [
        {
            "rel": ACQUISITION_REL,
            "href": f"{root}/file",
            "type": CBZ_MEDIA_TYPE,
            "title": "Download CBZ",
            "properties": {"length": item.size},
        },
        {
            "rel": PAGE_MANIFEST_REL,
            "href": f"{root}/pages",
            "type": "application/json",
            "title": "Page manifest",
        },
        {
            "rel": PAGE_RANGE_REL,
            "href": f"{root}/range{{?start,end}}",
            "type": CBZ_MEDIA_TYPE,
            "title": "Download a page range as a CBZ",
            "templated": True,
        },
    ]


def _feed_links(
    base_url: str, filters: dict[str, str], page: int, page_size: int, total: int
) -> list[dict[str, object]]:
    def page_link(rel: str, target: int) -> dict[str, object]:
        return _link(rel, url(base_url, PUBLICATIONS_PATH, {**filters, "page": target}))

    links = [page_link("self", page), _link("start", f"{base_url}{CATALOG_PATH}")]
    if page > 1:
        links.append(page_link("previous", page - 1))
    if page * page_size < total:
        links.append(page_link("next", page + 1))
    return links


def _navigation_entry(title: str, count: int, href: str) -> dict[str, object]:
    return {
        "title": title,
        "href": href,
        "type": OPDS_MEDIA_TYPE,
        "properties": {"numberOfItems": count},
    }


def _link(rel: str, href: str) -> dict[str, object]:
    return {"rel": rel, "href": href, "type": OPDS_MEDIA_TYPE}


def _filters(**values: str | None) -> dict[str, str]:
    return {key: value for key, value in values.items() if value}


def url(base_url: str, path: str, parameters: dict[str, object]) -> str:
    return (
        f"{base_url}{path}?{urlencode(parameters)}"
        if parameters
        else f"{base_url}{path}"
    )
