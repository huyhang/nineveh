# Nineveh

Nineveh is a self-hosted, API-first OPDS 2.0 service for CBZ comic and manga libraries. It provides authenticated OPDS feeds, full-archive downloads, individual page access, a responsive browser catalog, and local user administration.

The service is designed for a small Docker host such as a Synology NAS. Media remains read-only while users, the catalog index, and generated thumbnails live in a separate state volume.

## Features

- OPDS 2.0 catalog and publication feeds
- HTTP Basic authentication for OPDS clients
- Secure browser sessions and a responsive catalog
- Local users and hierarchical read access managed through an administrator page or JSON API
- Explicitly managed libraries with per-library scans and indexed-size reporting
- Series-first browser navigation through libraries and comics/manga categories
- Responsive browser reader with single-page, double-page, and continuous modes
- Automatic per-user reading progress and reading-mode synchronization
- Administrator-reviewed MangaBaka metadata with durable local edits and covers
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

Mount a directory with this exact structure at `/data`:

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

Symlinks and files outside this hierarchy are ignored. Pages are naturally sorted by archive member name. When present, Nineveh uses title, series, number, summary, creators, and cover information from `ComicInfo.xml`.

## Quick start with Docker Compose

1. Copy `docker/.env.example` to `docker/.env` and set the absolute media and state paths.
2. Create `docker/secrets/admin_password.txt` containing the initial administrator password. Use at least 12 characters and restrict access to the file.
3. Ensure the configured UID/GID can read the media directory and write to the state directory.
4. Start the service:

```sh
docker compose --env-file docker/.env -f docker/compose.yaml up --build -d
```

Open `http://NAS_ADDRESS:8080/`, or the HTTPS address configured through a reverse proxy. The OPDS catalog URL is:

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
| `/opds/v2/navigation.json?library=&category=` | Category and series navigation feeds |
| `/opds/v2/publications.json` | Paginated publication feed and search |
| `/api/v1/publications/{id}` | Single publication as an OPDS entry |
| `/api/v1/series/{id}` | Local series information and optional stored metadata |
| `/api/v1/series/{id}/cover` | Admin, MangaBaka, or local fallback series cover |
| `/api/v1/publications/{id}/file` | Original CBZ download |
| `/api/v1/publications/{id}/cover?width=320` | Generated WebP cover (160, 320, or 640) |
| `/api/v1/publications/{id}/pages?start=1&end=20` | Ordered page-range manifest |
| `/api/v1/publications/{id}/pages/{number}` | Original page image |
| `/api/v1/publications/{id}/range?start=1&end=20` | That page range as a standalone CBZ |
| `/api/v1/admin/libraries` | Managed libraries, indexed capacity, and available `/data` directories |
| `/api/v1/admin/users/{id}/access` | Library, content-type, and series read grants |
| `/api/v1/admin/settings`, `/api/v1/admin/restart` | Persisted application settings and restart control |
| `/api/v1/admin/metadata` | Manga metadata status and manually initiated matching operations |
| `/api/v1/health/live`, `/api/v1/health/ready` | Liveness and readiness with scan status |
| `/docs` | Interactive OpenAPI documentation |
| `/openapi.json` | The same contract this service serves, committed at [`docs/openapi.json`](docs/openapi.json) |

The committed specification is generated with `python scripts/export-openapi.py`. `tests/test_contract.py` compares it against the live route table, so a route added, removed, or renamed without regenerating the file fails the build rather than silently shipping a stale contract.

The page manifest and page-range download are Nineveh extensions, advertised from each OPDS publication under the `urn:nineveh:rel:page-manifest` and `urn:nineveh:rel:page-range` relations. Standard OPDS readers can use the full-CBZ acquisition link and ignore both; clients aware of the extensions can fetch individual pages or save an excerpt. A range may span at most `NINEVEH_PAGE_RANGE_LIMIT` pages and is generated on demand, never cached.

The browser catalog also links every publication to an immersive reader. Its double-page mode keeps the cover separate, preserves stitched spreads declared in `ComicInfo.xml` or detected from image dimensions, and places separate pages right-to-left for manga or left-to-right for comics. Everything that moves along that axis follows it: the arrow keys, swipes, the page-turn controls at the edges of the page, and the progress slider. A reader whose library disagrees with the category can override the direction from the toolbar, and that choice is remembered in their browser. On narrow portrait screens, paired pages temporarily adapt to a readable single-page view unless the reader explicitly requests the pair. Progress and mode are saved automatically per account, with a device-local fallback during transient connection failures.

## Configuration

| Variable | Default | Description |
|---|---:|---|
| `NINEVEH_DATA_DIR` | `/data` | Read-only library root inside the container |
| `NINEVEH_STATE_DIR` | `/state` | Writable database and cache directory |
| `NINEVEH_ADMIN_USERNAME` | `admin` | First administrator username |
| `NINEVEH_ADMIN_PASSWORD_FILE` | — | File containing the first administrator password |
| `NINEVEH_ADMIN_PASSWORD` | — | Less secure alternative to the password file |
| `NINEVEH_SECURE_COOKIES` | `true` | Require HTTPS for browser session cookies |
| `NINEVEH_PUBLIC_BASE_URL` | request URL | Origin stamped into OPDS links and accepted for browser sign-in |
| `NINEVEH_MEMORY_LIMIT` | `1g` | Container memory ceiling (Compose only) |
| `NINEVEH_RESTART_ENABLED` | `false` | Permit the admin UI to terminate gracefully for supervisor restart |
| `NINEVEH_HOST` | `0.0.0.0` | Listen address |
| `NINEVEH_PORT` | `8080` | Listen port |
| `NINEVEH_LOG_LEVEL` | `INFO` | Level for the JSON stdout log |
| `NINEVEH_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted |

Getting that last one wrong used to fail silently. Nineveh now warns at startup when its own container gateway is not in the trusted list, and raises a banner on **Admin → Overview** — naming the peer address and the exact variable to set — the first time it discards a real proxy's `X-Forwarded-Proto`. It reports; it never widens the trust list itself, because finding the address in front of the container does not establish that it is your proxy.

Archive safety limits can also be adjusted through the variables defined in [`config.py`](src/nineveh/config.py).

### Settings owned by the admin UI

Twelve settings are edited at **Admin → Settings** rather than in the environment: service title, session lifetime, scan interval, feed page size, page-range limit, archive cache size, thumbnail and page cache budgets, the two worker ceilings, the image-pixel ceiling, and the MangaBaka request limit. They are deliberately absent from `docker/.env.example` — a value with two owners is a value that eventually disagrees with itself.

The MangaBaka request limit defaults to 30 per rolling 60-second window and may only be lowered. Unlike the other settings, it takes effect immediately. Request reservations are persisted, so a restart cannot reset the limit. Browsing and catalog scans use stored metadata and never contact MangaBaka; an administrator must explicitly request suggestions, confirm a match, or refresh one. MangaBaka-derived data is attributed in the interface under its CC BY-NC-SA 4.0 license.

Metadata management lives alongside the collection: administrators can open a manga category to perform selected batch lookups, or use **Manage metadata** on an individual series. It is intentionally absent from the general administration console.

Reader-facing lists — alternative titles, creators, publishers, tags — are capped at twenty entries, because MangaBaka returns every tag it holds and a popular series carries hundreds. The untruncated response stays in the retained provider record. In the editor, list fields take **one entry per line**: titles and credits contain commas often enough ("Oh, My Sweet Alien!", "Smith, John") that splitting on them corrupted the value the moment the form was saved.

`NINEVEH_PUBLIC_BASE_URL` stays deployment-owned and is shown read-only on the settings page. It decides which origin may sign in, so an administrator who mistyped it in the UI would be locked out of the page needed to correct it. An override left in the database by an older release is discarded at startup rather than rejected, so retiring a setting never leaves an existing install unbootable.

Each has the same environment variable as before (`NINEVEH_FEED_PAGE_SIZE` and friends) and still reads from it if you set one. Precedence is narrow on purpose: a saved value is stored **only while it differs from the environment**, so editing one field in the UI never freezes the other eleven. Set a variable in `docker/.env` and it takes effect on the next `up -d` for every setting an administrator has not deliberately pinned; pin one in the UI and it wins until you clear it by saving the environment's value back.

Nineveh's resident set is roughly 80–120 MB and does not grow with library size; the archive, thumbnail, and page caches are bounded on disk under `/state`, not in memory. The two worker ceilings and `NINEVEH_MAX_IMAGE_PIXELS` are what actually bound peak RAM. See [the deployment guide](docker/synology-deployment.md#resource-tuning) for sizing.

`/state` holds the database and generated caches, including `thumbnails/`, `page-cache/`, `ranges/`, and `series-covers/`. Back up `nineveh.sqlite3` plus `series-covers/` if you use custom series artwork; the remaining caches are regenerated on demand. Inside `series-covers/`, only the top level is owned data — `series-covers/candidates/` holds throwaway thumbnails from suggestion lists and is bounded like every other cache under `/state`. Generated range archives are deleted as soon as their response completes, and any left behind by an unclean shutdown are cleared at startup.

The bootstrap password is used only when `/state` contains no users. Afterwards, users, access grants, libraries, scans, and application settings are managed at `/admin`. New readers have no catalog access until an administrator grants it. Existing readers are granted access to their currently indexed libraries when upgrading from the original schema.

On first startup, Nineveh registers each top-level directory under `/data`. Later directories must be added explicitly from the admin UI. Removing a library clears its index and grants but never changes the read-only media directory. Reported capacity is the sum of indexed CBZ file sizes.

Docker-level options such as `NINEVEH_MEMORY_LIMIT` remain deployment-managed. Compose enables the UI restart action; it gracefully exits the application and the `unless-stopped` policy restarts the same container with saved application settings. A Docker restart does not apply edits to Compose-level resource limits; recreate the service after changing those values. The settings page reports the ceiling it reads from the container's own cgroup, so it shows what is actually enforced rather than what was declared.

Upgrades run in place and are replayable: a v1 database gains the managed-library, series, grant, and settings tables on first start, and an upgrade interrupted partway through resumes on the next start rather than leaving the database unopenable.

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

Run `pytest` for the suite (it enforces 90% branch coverage), and `ruff check src tests && ruff format --check src tests` for style. [CI](.github/workflows/ci.yml) runs all three on Linux with Python 3.12 — the same platform the image ships — then builds the image and smoke-tests a live container.

`create_app(settings, container)` takes every I/O seam as an argument, so tests can substitute any of them; see [`tests/fakes.py`](tests/fakes.py) and [`tests/test_container.py`](tests/test_container.py) for the whole HTTP surface driven without a single CBZ on disk.

The application uses one process intentionally; blocking archive and password work runs through bounded framework worker threads while SQLite and in-process caches remain singular.

## MangaBaka attribution

Manga metadata in Nineveh is provided by [MangaBaka](https://mangabaka.org/) through its [public API](https://api.mangabaka.org/). MangaBaka data is available under the [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-nc-sa/4.0/).

Nineveh is not affiliated with or endorsed by MangaBaka. Cover images referenced by the metadata may remain subject to the rights of their respective owners.

## Security notes

Use HTTPS for any non-local deployment. The Docker Compose configuration mounts `/data` read-only, drops Linux capabilities, uses a read-only container filesystem, and persists only `/state`. Back up `/state/nineveh.sqlite3` to preserve accounts and stable publication identifiers.
