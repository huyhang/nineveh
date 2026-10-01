# Nineveh

Nineveh is a self-hosted, API-first OPDS 2.0 service for CBZ comic and manga libraries. It provides authenticated OPDS feeds, full-archive downloads, individual page access, a responsive browser catalog, and local user administration.

The service is designed for a small Docker host such as a Synology NAS. Media is written only by the librarian agent's guarded ingest path, which never replaces an existing file; users, the catalog index, agent activity, and generated thumbnails live in a separate state volume.

## Features

- OPDS 2.0 catalog and publication feeds
- HTTP Basic authentication for OPDS clients
- Secure browser sessions and a responsive catalog
- Local users and hierarchical read access managed through an administrator page or JSON API
- Explicitly managed libraries with per-library scans and indexed-size reporting
- Several data mounts, added and configured from the administrator page, with
  per-mount scans, health, and a reversible disconnect
- A dedicated ranked search page with typo tolerance, faceted filters, match
  explanations, and direct links to the matching volume
- Series-first browser navigation through libraries and comics/manga categories
- Default, dense, and list layouts remembered by each browser
- A grant-aware Private Collection with matching browser and OPDS navigation
- Responsive browser reader with single-page, double-page, and continuous modes
- Automatic per-user reading progress with per-browser, per-series reading modes
- Administrator-reviewed MangaBaka metadata with durable local edits and covers
- Scoped librarian-agent API with fuzzy series resolution and a two-step guarded ingest
- Auditable agent activity, with permission changes and writes in one feed
- Persisted application settings with a Docker-supervised restart action
- System, light, paper, and dark display themes with a persistent header toggle
- Original CBZ downloads with byte-range and cache support
- Ordered page manifests and individually streamed page images
- Contiguous page ranges downloadable as a standalone CBZ
- Incremental catalog scans with `ComicInfo.xml` support
- Bounded archive and thumbnail caches
- ZIP traversal, expansion, and size protections

PDF files are not supported in this initial release.

## Library layout

Every data mount uses this structure. `/data` is the first one; an
administrator may register more from **Admin → Libraries**:

```text
data root/
├── Library One/
│   ├── comics/
│   │   └── Series Name/
│   │       ├── Issue 01.cbz
│   │       └── Issue 02.cbz
│   └── manga/
│       └── Another Series/
│           └── Volume 01.cbz
└── Library Two/
    └── comics/
        └── Series Name/
            └── Collection.cbz
```

A library's directory name only has to be unique on its own mount. Its
*display* name is unique everywhere, because that is what readers, grants and
OPDS feeds see: adding a second "Manga" folder from another disk asks for a
display name to tell them apart.

Symlinks and files outside this hierarchy are ignored. Pages are naturally sorted by archive member name. When present, Nineveh uses title, series, number, summary, creators, and cover information from `ComicInfo.xml`.

## Quick start with Docker Compose

1. Copy `docker/.env.example` to `docker/.env` and set the absolute media and state paths.
2. Create `docker/secrets/admin_password.txt` containing the initial administrator password. Use at least 12 characters and restrict access to the file.
3. Ensure the configured UID/GID can read the media directory and write to the state directory. It needs write access to media only where the librarian places volumes; otherwise set `NINEVEH_DATA_MODE=ro`.
4. Start the service:

```sh
docker compose --env-file docker/.env -f docker/compose.yaml up --build -d
```

Compose publishes port 8080 on the NAS's loopback interface only, so readers reach Nineveh through an HTTPS reverse proxy and administrators through Tailscale Serve or a LAN proxy address — see [Split access](#split-access). The OPDS catalog URL is:

```text
https://nineveh.example.com/opds/v2/catalog.json
```

For detailed Synology instructions, see [docker/synology-deployment.md](docker/synology-deployment.md).

## API overview

All catalog and content endpoints require authentication.

| Endpoint | Purpose |
|---|---|
| `/opds/v2/authentication.json` | OPDS authentication discovery |
| `/opds/v2/catalog.json` | Root OPDS navigation feed |
| `/opds/v2/navigation.json?library=&category=` | Category and series navigation feeds; add `collection=private` to walk the Private Collection |
| `/opds/v2/private.json` | Private Collection navigation for series allowed by existing grants |
| `/opds/v2/publications.json` | Paginated publication feed and search; `collection=private` selects private series instead of the default public ones |
| `/api/v1/publications/{id}` | Single publication as an OPDS entry |
| `/api/v1/series/{id}` | Local series information and optional stored metadata |
| `/api/v1/series/{id}/cover` | The series cover: its artwork or its first volume's cover, as chosen below |
| `/api/v1/admin/series/{id}/cover-source` | Choose one series' cover source, or let it follow its library |
| `/api/v1/admin/libraries/{id}/series-cover` | Choose the cover source for every series in a library |
| `/api/v1/series/{id}/privacy` | `PUT` moves a series into or out of Private Collection (administrators only) |
| `/api/v1/publications/{id}/file` | Original CBZ download |
| `/api/v1/publications/{id}/cover?width=320` | Generated WebP cover (160, 320, or 640) |
| `/api/v1/publications/{id}/pages?start=1&end=20` | Ordered page-range manifest |
| `/api/v1/publications/{id}/pages/{number}` | Original page image, or a screen-sized copy with `?width=` (640, 960, or 1280) |
| `/api/v1/publications/{id}/range?start=1&end=20` | That page range as a standalone CBZ |
| `/api/v1/publications/{id}/progress` | Per-reader position and completion (`PUT` to save, `DELETE` to clear); `mode` is deprecated, see below |
| `/api/v1/search` | Ranked series search with facet counts; the same engine the search page uses |
| `/api/v1/search/suggestions` | Type-ahead series suggestions for the browser search box |
| `/api/v1/admin/libraries` | Managed libraries, indexed capacity, data mounts, and their unmanaged directories |
| `/api/v1/admin/mounts` | Register a data mount; `PUT`, `DELETE`, `/disconnect`, `/reconnect`, and `/scan` manage one |
| `/api/v1/admin/users` | List and create accounts; `PATCH /{id}` resets a password, disables or re-enables, or changes the role with `is_admin` |
| `/api/v1/admin/users/{id}/access` | Library, content-type, and series read grants |
| `/api/v1/admin/settings`, `/api/v1/admin/restart` | Persisted application settings and restart control |
| `/api/v1/admin/librarian-tokens` | Issue, re-scope, list, and revoke librarian credentials |
| `/api/v1/admin/librarian/activity` | Merged lifecycle and usage feed for the agent |
| `/api/v1/librarian/libraries` | Libraries the calling token may reach |
| `/api/v1/librarian/series` | Resolve a title, or filter series by stored metadata |
| `/api/v1/librarian/series/{id}` | Volumes on disk, latest volume, and provider totals |
| `/api/v1/librarian/ingest` | Stage, inspect, commit, or discard a proposed volume |
| `/api/v1/admin/metadata` | Manga metadata status and manually initiated matching operations |
| `/api/v1/admin/libraries/{id}/metadata/auto-match` | Start or inspect a resumable, confidence-gated library auto-match job |
| `/api/v1/admin/series/{id}/spread-detection` | Enable or disable automatic spread-start detection for a series |
| `/api/v1/admin/publications/{id}/spread-start` | Pin one volume's pairing start by hand, or restore detection |
| `/api/v1/admin/security/events` | Failed sign-ins, throttled accounts, and storage refusals |
| `/api/v1/health/live`, `/api/v1/health/ready` | Liveness and readiness; scan detail only on a private origin |
| `/docs` | Interactive OpenAPI documentation, for administrators on a private origin |
| `/openapi.json` | The same contract this service serves, committed at [`docs/openapi.json`](docs/openapi.json); administrators only |

The committed specification is generated with `python scripts/export-openapi.py`. `tests/test_contract.py` compares it against the live route table, so a route added, removed, or renamed without regenerating the file fails the build rather than silently shipping a stale contract.

The page manifest and page-range download are Nineveh extensions, advertised from each OPDS publication under the `urn:nineveh:rel:page-manifest` and `urn:nineveh:rel:page-range` relations. Standard OPDS readers can use the full-CBZ acquisition link and ignore both; clients aware of the extensions can fetch individual pages or save an excerpt. A range may span at most `NINEVEH_PAGE_RANGE_LIMIT` pages and is generated on demand, never cached.

The browser catalog also links every publication to an immersive reader. Its double-page mode keeps the cover separate, preserves stitched spreads declared in `ComicInfo.xml` or detected from image dimensions, and places separate pages right-to-left for manga or left-to-right for comics. A single stray page in the front matter would otherwise pair every later spread with the wrong half, so an administrator can enable spread-start detection for a series. Nineveh anchors on the first spread the scan left stitched into one wide image — a page that is a known spread boundary by construction, so a volume that has one needs nothing else. Only in a volume without one does it read the printed gutter between neighbouring pages to find where the real spreads begin. The cover always stands alone, however wide it is. It runs once per volume and again after each scan. The anchor only shifts the *parity* of pairing: pages before it still pair, aligned backwards from it, so one stray insert reads on its own instead of desynchronising every spread that follows. Detection abstains unless the evidence is clear, and any volume's start page can be pinned by hand. Everything that moves along that axis follows it: the arrow keys, swipes, the page-turn controls at the edges of the page, and the progress slider. A reader whose library disagrees with the category can override the direction from the toolbar, and that choice is remembered in their browser. On narrow portrait screens, paired pages temporarily adapt to a readable single-page view unless the reader explicitly requests the pair. Continuous mode keeps only a small window of pages loaded and serves them at the size the screen can actually show, upgrading to the original once the reader settles. Every page reserves its box from its known dimensions whether or not its image is loaded, so scrolling a high-resolution volume neither exhausts browser memory nor moves the document under the reader. Reading position and completion are synchronized per account. Reading mode is remembered separately for each series in the current browser, so changing it on one device does not alter another device.

Reading position is API state rather than a private detail of the browser reader: `/api/v1/publications/{id}/progress` reads, writes, and clears it under the same authentication and read grants as the rest of the catalog, so a third-party client can resume where the browser left off. Only the final page may be marked completed, which keeps "finished" meaning the same thing whoever wrote it. The `mode` field is deprecated but still compatible: responses always include it and a `PUT` may still send it, because apps built against the older contract require it. It is only ever what such an app last saved (`single` if it never did); a `PUT` without it leaves the stored value alone, and neither the browser reader nor current clients read it. New clients should omit it and keep reading mode on the device, per series. The two buttons on a volume card stay ordinary form posts on the browser surface, so marking something read still works without JavaScript.

`docs/app-openapi.json` is the slice of the contract for a native reading app: the OPDS feeds, series and publication detail, pages, downloads, reading progress, and `/api/v1/auth/me`, with the HTTP Basic scheme they require. An app in another repository vendors it the same way as the [librarian slice](#building-a-client); [`docs/contract-vendoring.md`](docs/contract-vendoring.md#vendoring-the-app-contract) covers the differences. Its JSON responses are described by schemas, so a renamed or removed response field changes the slice like any other contract change; only the OPDS feeds stay free-form, under their own media types. Covers, pages, and downloads declare the media types they are actually sent with, and a publication's volume number is the numeric `belongsTo.series[].position` that OPDS 2.0 specifies.

## Configuration

| Variable | Default | Description |
|---|---:|---|
| `NINEVEH_DATA_DIR` | `/data` | The first data mount; more are registered in Admin. The librarian's ingest path needs create access |
| `NINEVEH_DATA_MODE` | `rw` | Compose only: `ro` mounts the primary library read-only |
| `NINEVEH_PUBLISH_ADDRESS` | `127.0.0.1` | Compose only: host address the port is published on |
| `NINEVEH_STATE_DIR` | `/state` | Writable database and cache directory |
| `NINEVEH_ADMIN_USERNAME` | `admin` | First administrator username |
| `NINEVEH_ADMIN_PASSWORD_FILE` | — | File containing the first administrator password |
| `NINEVEH_ADMIN_PASSWORD` | — | Less secure alternative to the password file |
| `NINEVEH_SECURE_COOKIES` | `true` | Require HTTPS for browser session cookies |
| `NINEVEH_PUBLIC_BASE_URL` | request URL | Origin stamped into OPDS links and accepted for browser sign-in |
| `NINEVEH_PRIVATE_BASE_URLS` | — | Comma-separated private origins (Tailscale, LAN); setting any turns on [split access](#split-access) |
| `NINEVEH_PRIVATE_ALLOW_IPS` | Tailscale ranges | Client networks allowed on a private origin; add your LAN, e.g. `192.168.1.0/24` |
| `NINEVEH_MEMORY_LIMIT` | `1g` | Container memory ceiling (Compose only) |
| `NINEVEH_RESTART_ENABLED` | `false` | Permit the admin UI to terminate gracefully for supervisor restart |
| `NINEVEH_HOST` | `0.0.0.0` | Listen address |
| `NINEVEH_PORT` | `8080` | Listen port |
| `NINEVEH_LOG_LEVEL` | `INFO` | Level for the JSON stdout log |
| `NINEVEH_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted |
| `NINEVEH_MAX_UPLOAD_BYTES` | `4294967296` | Largest body accepted for one agent upload |
| `NINEVEH_MAX_REQUEST_BODY_BYTES` | `1048576` | Largest body for every other request, checked before parsing |
| `NINEVEH_MAX_RANGE_UNCOMPRESSED_BYTES` | `536870912` | Largest page range generated as one CBZ |
| `NINEVEH_STATE_FREE_RESERVE_BYTES` | `536870912` | Free space uploads and generated ranges may never use |
| `NINEVEH_STAGING_MAX_PER_TOKEN` | `10` | Uploads one librarian token may have staged at once |
| `NINEVEH_STAGING_MAX_BYTES_PER_TOKEN` | `21474836480` | Bytes one librarian token may have staged at once |
| `NINEVEH_DOWNLOAD_STREAMS` | `16` | Concurrent downloads, all accounts together |
| `NINEVEH_DOWNLOAD_STREAMS_PER_ACCOUNT` | `4` | Concurrent downloads per account; more wait their turn |
| `NINEVEH_RANGE_WORKERS` | `1` | Page-range archives generated at once |
| `NINEVEH_ACCOUNT_REQUESTS_PER_MINUTE` | `6000` | Per-account request rate; only a runaway client reaches it |
| `NINEVEH_ISOLATE_MEDIA_PROCESSING` | `false` | Also decode library pages in a limited worker process |
| `NINEVEH_CONNECTION_LIMIT` | `1024` | Concurrent connections uvicorn accepts before answering 503 |

Getting `NINEVEH_FORWARDED_ALLOW_IPS` wrong used to fail silently. Nineveh now warns at startup when its own container gateway is not in the trusted list, and raises a banner on **Admin → Overview** — naming the peer address and the exact variable to set — the first time it discards a real proxy's `X-Forwarded-Proto`. It reports; it never widens the trust list itself, because finding the address in front of the container does not establish that it is your proxy.

Archive safety limits can also be adjusted through the variables defined in [`config.py`](src/nineveh/config.py). Compose passes every deployment-owned variable through explicitly — `--env-file` fills in `compose.yaml` but does not reach the container on its own — and `tests/test_deployment.py` fails if a new one is left out.

### Split access

A public origin serves readers. Administration and the librarian agent belong somewhere the Internet cannot reach, so Nineveh can answer on private origins as well: a Tailscale Serve name, a LAN address behind the NAS's reverse proxy, or both.

```dotenv
NINEVEH_PUBLIC_BASE_URL=https://nineveh.example.com
NINEVEH_PRIVATE_BASE_URLS=https://nas.your-tailnet.ts.net,https://192.168.1.10:5443
NINEVEH_PRIVATE_ALLOW_IPS=100.64.0.0/10,fd7a:115c:a1e0::/48,192.168.1.0/24
```

A request is private only when it names a private origin **and** arrives from an allowed network, so forging a `Host` header gets nobody in. Once any private origin is set:

- administrator accounts sign in only on a private origin; on the public one their correct password is answered exactly like a wrong one;
- the admin pages and APIs, the API docs, and the whole librarian API answer 403 on the public origin before a request body is read;
- readiness detail (scan timing, library size, error text) is private; the public origin gets a bare status;
- requests naming any other origin are refused, and links are generated for the origin a request arrived on.

Every origin must be HTTPS, cookies must be secure, and `NINEVEH_FORWARDED_ALLOW_IPS` must name your proxies explicitly; Nineveh refuses to start otherwise. Without a private origin it behaves as before, and **Admin → Overview** warns that administration is reachable from anywhere. Keep a separate, non-administrator account for reading from the Internet. [The deployment guide](docker/synology-deployment.md#6-private-administration-over-tailscale-or-the-lan) walks through Tailscale Serve and a LAN address on a Synology.

### Abuse resistance

Nineveh does its blocking work — extraction, resizing, password hashing — on one shared thread pool, so the question is never only "how much" but "who waits behind whom". Expensive work waits for capacity on the event loop, not inside the pool, and a freed slot goes to the waiting account holding the fewest; a reader queued behind a script waits for one job, not the script's backlog. Work already done — a cached cover, page or rendition — never queues at all. Waiting is the normal answer: a grid of covers, a scroll window or a reader app's download queue simply takes its turn. Only a backlog far past anything a reader app produces is refused with `429` and `Retry-After`, and recorded.

Sign-ins get progressive delays rather than lockouts: each failure doubles the wait for the next attempt on that address and account, up to 30 seconds, and an address failing across many accounts is slowed too. Attempts from one address run one at a time, so extra connections buy a guesser nothing; identical credentials arriving together, such as an OPDS app opening its catalog, are verified once. Failures are forgotten after 15 quiet minutes, and no account is ever locked. Every password path — browser, OPDS, API, docs — goes through the same guard.

Failed sign-ins, throttled accounts and storage refusals are recorded on **Admin → Overview** and at `/api/v1/admin/security/events`. The table is bounded, so a spray can bury old signals but never grow the database.

### Settings owned by the admin UI

Thirteen settings are edited at **Admin → Settings** rather than in the environment: service title, session lifetime, scan interval, feed page size, page-range limit, archive cache size, the thumbnail, page, and scroll-rendition cache budgets, the two worker ceilings, the image-pixel ceiling, and the MangaBaka request limit. They are deliberately absent from `docker/.env.example` — a value with two owners is a value that eventually disagrees with itself.

The MangaBaka request limit defaults to 30 per rolling 60-second window and may only be lowered. Unlike the other settings, it takes effect immediately. Request reservations are persisted, so a restart cannot reset the limit. Browsing and catalog scans use stored metadata and never contact MangaBaka; an administrator must explicitly request suggestions, confirm a match, or refresh one. MangaBaka-derived data is attributed in the interface under its CC BY-NC-SA 4.0 license.

Metadata management lives alongside the collection: administrators can open a manga category to perform selected batch lookups, confidently auto-match every unmatched series in its library, or use **Manage metadata** on an individual series. Auto-match jobs preserve existing links and local edits, retain ambiguous suggestions for review, obey the configured request limit, and resume after an interrupted process. The same library-wide action is available from **Admin → Libraries**.

A matched series shows its MangaBaka cover, or an uploaded custom cover, in place of its first volume's. When your own scans make better covers, choose **First volume of each series** under a library in **Admin → Libraries**: every series in it switches, including series added or matched later, and a MangaBaka refresh does not bring the artwork back. A single series can override its library either way from its own page or from **Manage metadata**. Uploading a custom cover pins that series to its artwork, so the upload keeps showing whatever the library later chooses; removing the upload returns the series to its library's choice. "First volume" means the volume the series page lists first — numbered volumes in natural order (2 before 10), then unnumbered ones — not whichever file name sorts first. Comics have no series artwork, so their covers always come from the first volume.

Reader-facing lists — alternative titles, creators, publishers, tags — are capped at twenty entries, because MangaBaka returns every tag it holds and a popular series carries hundreds. The untruncated response stays in the retained provider record. In the editor, list fields take **one entry per line**: titles and credits contain commas often enough ("Oh, My Sweet Alien!", "Smith, John") that splitting on them corrupted the value the moment the form was saved.

`NINEVEH_PUBLIC_BASE_URL` stays deployment-owned and is shown read-only on the settings page. It decides which origin may sign in, so an administrator who mistyped it in the UI would be locked out of the page needed to correct it. An override left in the database by an older release is discarded at startup rather than rejected, so retiring a setting never leaves an existing install unbootable.

Each has the same environment variable as before (`NINEVEH_FEED_PAGE_SIZE` and friends) and still reads from it if you set one. Precedence is narrow on purpose: a saved value is stored **only while it differs from the environment**, so editing one field in the UI never freezes the other twelve. Set a variable in `docker/.env` and it takes effect on the next `up -d` for every setting an administrator has not deliberately pinned; pin one in the UI and it wins until you clear it by saving the environment's value back.

Nineveh's resident set is roughly 80–120 MB and does not grow with library size; the archive, thumbnail, and page caches are bounded on disk under `/state`, not in memory. The two worker ceilings and `NINEVEH_MAX_IMAGE_PIXELS` (80 megapixels by default) are what actually bound peak RAM. See [the deployment guide](docker/synology-deployment.md#resource-tuning) for sizing.

`/state` holds the database and generated caches, including `thumbnails/`, `page-cache/`, `renditions/`, `ranges/`, and `series-covers/`. Back up `nineveh.sqlite3` plus `series-covers/` if you use custom series artwork; the remaining caches are regenerated on demand. Inside `series-covers/`, only the top level is owned data — `series-covers/candidates/` holds throwaway thumbnails from suggestion lists and is bounded like every other cache under `/state`. Generated range archives are deleted as soon as their response completes, and any left behind by an unclean shutdown are cleared at startup.

The bootstrap password is used only when `/state` contains no users. Afterwards, users, access grants, libraries, scans, and application settings are managed at `/admin`. New readers have no catalog access until an administrator grants it. Existing readers are granted access to their currently indexed libraries when upgrading from the original schema.

On first startup, Nineveh registers each top-level directory under `/data`. Later directories must be added explicitly from the admin UI. Removing a library clears its index and grants but never deletes anything from the media directory. Reported capacity is the sum of indexed CBZ file sizes.

### Data mounts

A *data mount* is a directory Nineveh indexes libraries from. `/data` is the
first, and an administrator registers the rest under **Admin → Libraries** by
entering a path. That path is a location **inside the container**: Nineveh can
register a directory the deployment has already exposed, but it cannot create a
Docker bind mount, so add the volume first and recreate the service. Paths are
validated for existence, permissions, symlinks, overlap with another mount or
with `/state`, and against the system directories a library never lives in.

New mounts are read-only to the librarian agent until an administrator enables
ingest and the underlying volume is writable. Each mount can also be left out
of scheduled scans and scanned on demand instead.

Mounts have three states an administrator can see on the card:

| State | What it means |
|---|---|
| Healthy | Readable, scanned, and serving |
| Disconnected | Hidden from readers and skipped by scans, on purpose. The index, grants, reading progress and media are all kept, and reconnecting restores them |
| Missing | Nothing is mounted at that path. Libraries stay indexed and are never pruned; a rescan picks them up when the storage returns |

Unplugging a disk is therefore never mistaken for deleting a library. A
disconnected mount can be permanently forgotten, which clears its catalog rows,
grants and metadata — and still never touches a media file. The original `/data`
mount cannot be forgotten, because it is the directory Nineveh was configured
with.

### Search

**Search** is its own page. It ranks series rather than listing them: an exact
title beats a prefix, a creator beats a tag, and a title containing the whole
query beats one that merely contains each word. A query that matches nothing is
retried once against the index's own vocabulary, so `ninevh` still finds Nineveh.
Each result says which field matched and links directly to the matching volumes,
and the filter rail counts each value against every *other* active filter, so a
count always says how many results choosing it will give.

Search covers everything the reader may already reach, Private Collection
included, and marks those results. It runs entirely server-rendered; the `/`
shortcut and the type-ahead suggestions are enhancements that need JavaScript.

Docker-level options such as `NINEVEH_MEMORY_LIMIT` remain deployment-managed. Compose enables the UI restart action; it gracefully exits the application and the `unless-stopped` policy restarts the same container with saved application settings. A Docker restart does not apply edits to Compose-level resource limits; recreate the service after changing those values. The settings page reports the ceiling it reads from the container's own cgroup, so it shows what is actually enforced rather than what was declared.

Upgrades run in place and are replayable: a v1 database gains the managed-library, series, grant, and settings tables on first start, and an upgrade interrupted partway through resumes on the next start rather than leaving the database unopenable. There is no migration command to run — pull the image and restart.

Arriving at data mounts and search (schema v10) does three things to an existing
database, all on the first start: the configured `NINEVEH_DATA_DIR` becomes the
default mount and every library is attached to it; uniqueness moves from the bare
directory name to the mount it sits on, and from the bare stored path to the
library it belongs to, so two disks may repeat both; and the search index is built
from the rows already there, so search works without a rescan. Nothing is
re-indexed and nothing is pruned — the first scan after the upgrade reports every
publication unchanged.

A database is only ever read by the release that understands it: an older image
refuses a newer schema with a clear message rather than reading it half-right. The
upgrade is therefore one-way, so keep the usual copy of the state directory before
pulling a new image.

## Local development

Nineveh requires Python 3.12 or newer.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
export NINEVEH_DATA_DIR="$PWD/example-data"
export NINEVEH_STATE_DIR="$PWD/state"
export NINEVEH_ADMIN_PASSWORD='replace-with-a-long-password'
export NINEVEH_SECURE_COOKIES=false
nineveh
```

To try a change against a running server, see [Testing without Docker](#testing-without-docker) below.

Run `pytest` for the suite (it enforces 90% branch coverage), and `ruff check src tests && ruff format --check src tests` for style. [CI](.github/workflows/ci.yml) runs all three on Linux with Python 3.12 — the same platform the image ships — then builds the image and smoke-tests a live container.

`create_app(settings, container)` takes every I/O seam as an argument, so tests can substitute any of them; see [`tests/fakes.py`](tests/fakes.py) and [`tests/test_container.py`](tests/test_container.py) for the whole HTTP surface driven without a single CBZ on disk.

The application uses one process intentionally; blocking archive and password work runs through bounded framework worker threads while SQLite and in-process caches remain singular.

### Testing without Docker

`scripts/dev-server.sh` runs Nineveh straight from this checkout, so a change can be tried from a browser, the reading app or Cleo without rebuilding the image. It restarts on every edit under `src/`.

```sh
scripts/dev-server.sh            # serve on http://127.0.0.1:8081
scripts/dev-server.sh token      # in a second terminal: a read-only librarian token
scripts/dev-server.sh --reset    # throw everything away and start again
```

**What it creates.** Everything lives in `.dev/`, which is gitignored:

| Path | Contents |
|---|---|
| `.dev/data/` | A two-issue sample library, from `docker/create-sample-library.py` |
| `.dev/state/` | The database and caches |
| `.dev/admin-password` | A generated password for the `admin` account |

It never touches `example-data/`, `state/` or the Docker container, and it uses a different port, so the container can keep serving on 8080 at the same time.

**Signing in.** The first start creates an `admin` account with the generated password, and the startup banner prints both. Nineveh creates this account only when no users exist. If you change the password in the admin UI, `.dev/admin-password` goes stale, and the banner and `token` stop working until you run `--reset`.

**Pointing Cleo at it.** `token` issues a token with `catalog:read` and `metadata:read`, and prints it as two `export` lines. Environment variables take precedence over Cleo's `.env`, so this redirects Cleo in the current shell only:

```sh
eval "$(scripts/dev-server.sh token)"
cleo
```

For a token with ingest scopes, issue one under **Admin → Librarian** instead.

**Options.**

| Variable | Default | Effect |
|---|---|---|
| `NINEVEH_DEV_PORT` | `8081` | Port to serve on |
| `NINEVEH_DEV_DATA` | `.dev/data` | Library to serve, e.g. `"$PWD/example-data"`. State stays in `.dev/` |

To exercise several mounts locally, start the server, create another source
root anywhere the process can read, and register its absolute path under
**Admin → Libraries**. A local process sees the path directly; a container
needs the bind mount first.

Any other [configuration](#configuration) variable you export, such as `NINEVEH_SCAN_INTERVAL_SECONDS`, is passed through to Nineveh.

**How it differs from the container.** It listens on `127.0.0.1` only, so an iPad or phone can't reach it; test from a device against the container. Cookies aren't marked `Secure`, because it's served over plain HTTP. There is no memory limit and no read-only filesystem, and logs use uvicorn's plain-text format rather than JSON.

## MangaBaka attribution

Manga metadata in Nineveh is provided by [MangaBaka](https://mangabaka.org/) through its [public API](https://api.mangabaka.org/). MangaBaka data is available under the [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-nc-sa/4.0/).

Nineveh is not affiliated with or endorsed by MangaBaka. Cover images referenced by the metadata may remain subject to the rights of their respective owners.

## Librarian agent

Nineveh exposes a small API for an LLM-based librarian running on another device. Tokens are created under **Admin → Librarian**, limited to an explicit set of capabilities and to selected libraries. The secret is displayed once, at creation, and stored only as a hash.

Capabilities are deliberately finer than read and write:

| Capability | What it allows |
|---|---|
| `catalog:read` | Find series by title and list the volumes on disk |
| `metadata:read` | Answer author, status, and publication questions from stored details |
| `ingest:stage` | Validate and stage a new volume; writes nothing into the library |
| `ingest:commit` | Place a staged volume on disk |

Staging and placing are separate on purpose. A token held by a phone or a remote helper can be granted `ingest:stage` alone: it may propose a volume and see exactly where it would land, while only a token you keep locally can complete the write. `GET /api/v1/librarian/ingest` lists what is waiting.

Uploads are validated with the same archive rules the scanner uses, checked against the series for identical content, and offered a filename consistent with the volumes already there. Placement refuses a name that exists, and the file only appears once it is completely written.

Every read, proposal, placement, refusal, and permission change is recorded with the capabilities that were in force at the time, and shown together under **Admin → Librarian**. Revoking a token removes it from the list while leaving its history intact.

### Building a client

`docs/librarian-openapi.json` is the agent-facing slice of the contract: the eight librarian operations, the schemas they reference, and the bearer scheme they require, and nothing else. Every response is described by a schema, so a generated client gets typed objects and a renamed response field shows up as a contract change. A client in another repository should vendor that file rather than `docs/openapi.json`, so its copy moves only when the endpoints it calls move — not every time an unrelated part of Nineveh changes.

Regenerate every contract with `python scripts/export-openapi.py`. `tests/test_contract.py` fails if any of them drifts from the live route table, and additionally checks that every `$ref` and security scheme in a slice resolves inside it, so the file is safe to feed straight to a code generator.

Pin the slice by content hash rather than by `info.version`: the version tracks the package and bumps on releases that leave the agent surface untouched.

[`docs/contract-vendoring.md`](docs/contract-vendoring.md) walks through setting this up in a client repository, and lists what a client has to get right — capabilities, the staging/committing split, correlation headers, and the status codes worth handling.

## Security notes

Use HTTPS for any non-local deployment, and turn on [split access](#split-access) before exposing Nineveh to the Internet. The Compose configuration publishes the port on loopback only, drops Linux capabilities, forbids privilege escalation, limits memory, CPU, processes and open files, uses a read-only container filesystem with a `noexec` `/tmp`, and persists only `/state`. `/data` is writable only so the librarian can place volumes; set `NINEVEH_DATA_MODE=ro` if nothing ingests into it. Nothing in the agent surface can overwrite, move, rename, or delete an existing file. Metadata cover images, which arrive from uploads and a remote provider, are decoded in a worker process under memory and CPU limits; `NINEVEH_ISOLATE_MEDIA_PROCESSING=true` extends that to library pages at a latency cost on every cold render.

The image installs only hash-pinned dependencies (`requirements*.lock`), CI audits them with `pip-audit` and publishes an SPDX SBOM of each image build as the `sbom` workflow artifact, and Dependabot proposes updates for Python packages, the base image and the workflow actions. Back up the media tree and `/state/nineveh.sqlite3`; filesystem access is broader than the agent API's authorization, so NAS snapshots remain worthwhile.
