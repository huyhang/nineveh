# Deploying Nineveh on Synology DSM

This guide uses Container Manager and Docker Compose. It assumes DSM 7.2 or newer, Container Manager is installed, and the NAS can build container images.

## 1. Prepare directories

Create separate directories for the source, persistent state, initial password, and media. For example:

```text
/volume1/docker/nineveh/
├── source/        # this repository
├── state/         # SQLite database and thumbnail cache
└── secrets/
    └── admin_password.txt

/volume1/media/nineveh/
└── My Library/
    ├── comics/
    │   └── Series Name/
    │       └── Issue 01.cbz
    └── manga/
        └── Series Name/
            └── Volume 01.cbz
```

Keep the state directory outside the media tree so the library can be mounted read-only.

## 2. Determine the service account IDs

Through SSH, run `id USERNAME` for the DSM account that should run Nineveh. Record its numeric UID and GID. That account needs:

- Read and directory-traversal permission for the media tree
- Read/write permission for `/volume1/docker/nineveh/state`
- Read permission for the initial password file

Using DSM shared-folder permissions is preferable to making these directories world-writable.

## 3. Configure Nineveh

Place this repository in `/volume1/docker/nineveh/source`, then copy `docker/.env.example` to `docker/.env`. Set at least:

```dotenv
NINEVEH_DATA_PATH=/volume1/media/nineveh
NINEVEH_STATE_PATH=/volume1/docker/nineveh/state
NINEVEH_SECRETS_PATH=/volume1/docker/nineveh/secrets
NINEVEH_PUID=1026
NINEVEH_PGID=100
NINEVEH_PORT=8080
NINEVEH_PUBLIC_BASE_URL=https://nineveh.example.com
NINEVEH_SECURE_COOKIES=true
```

Replace the example UID, GID, hostname, and paths. Put a unique password of at least 12 characters in `admin_password.txt`; the file must contain only the password and a trailing newline is allowed.

The password bootstraps the `admin` account only when the state database has no users. It does not reset an existing account.

## 4. Build and start the container

From the source directory over SSH:

```sh
docker compose --env-file docker/.env -f docker/compose.yaml config
docker compose --env-file docker/.env -f docker/compose.yaml up --build -d
docker compose --env-file docker/.env -f docker/compose.yaml ps
```

Alternatively, create a Container Manager **Project** using `docker/compose.yaml`. Confirm that Container Manager loads `docker/.env` before starting the project.

Follow startup and scan progress with:

```sh
docker compose --env-file docker/.env -f docker/compose.yaml logs -f nineveh
```

The first catalog scan runs in the background. Large libraries can appear gradually while it completes. Its status is available at `/api/v1/health/ready` and on the admin page.

## 5. Configure HTTPS reverse proxying

Browser sessions use secure cookies by default, so configure HTTPS before normal use:

1. In DSM, open **Control Panel → Login Portal → Advanced → Reverse Proxy**.
2. Create an HTTPS source such as `nineveh.example.com:443`.
3. Set the destination to `http://127.0.0.1:8080`.
4. Assign a trusted certificate to the hostname.
5. Preserve the `Host`, `X-Forwarded-For`, and `X-Forwarded-Proto` headers.
6. Restrict direct access to port 8080 with the DSM firewall when possible.
7. Set `NINEVEH_PUBLIC_BASE_URL=https://nineveh.example.com` in `docker/.env`.
8. Set `NINEVEH_FORWARDED_ALLOW_IPS` to the address the proxy arrives from — see [Finding the address your proxy arrives from](#finding-the-address-your-proxy-arrives-from).

Step 7 is not optional behind a proxy. The proxy terminates TLS, so Nineveh only learns the public origin if you tell it: without the variable every OPDS link is published as `http://`, and browser sign-in fails with *Invalid request origin* because the `Origin` header the browser sends will not match the origin Nineveh infers.

Step 8 decides whether Nineveh believes the proxy at all. Docker's port mapping rewrites the source address, so a proxy pointed at `127.0.0.1:8080` does **not** reach the container from `127.0.0.1` — the loopback default therefore discards its `X-Forwarded-Proto` silently. The visible symptom is a missing `Strict-Transport-Security` header and access logs that record the gateway rather than the client.

For temporary HTTP-only testing, set `NINEVEH_SECURE_COOKIES=false`. Do not use that setting for an Internet-accessible deployment.

### Finding the address your proxy arrives from

Ask the running container, rather than guessing a network name:

```sh
docker inspect nineveh --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
# 172.19.0.1
```

To trust the whole subnet instead of the single gateway, look up the network that container is attached to and read its CIDR:

```sh
docker inspect nineveh --format '{{range $name, $conf := .NetworkSettings.Networks}}{{$name}}{{end}}'
# nineveh_default

docker network inspect nineveh_default --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
# 172.19.0.0/16
```

**Do not inspect the `bridge` network.** Compose puts the service on a network of its own, named after the project — `nineveh_default`, from the `name:` at the top of the compose file. The default `bridge` network is a different subnet (commonly `172.17.0.0/16`), so trusting it would trust nothing that ever connects.

`NINEVEH_FORWARDED_ALLOW_IPS` accepts a single address, a comma-separated list, or CIDR notation. Prefer the narrowest value that works: trusting a subnet means any container on it can forge `X-Forwarded-For`. Avoid `*` unless the published port is reachable only by the trusted proxy.

#### When the value still does not work

Easiest route: send one request through the proxy and read the banner on **Admin → Overview**, which names the peer address verbatim. To measure it without the app, start a throwaway listener on a spare port, connect to it exactly as the proxy would, and read the peer it reports:

```sh
docker run -d --name peer-probe -p 8099:8099 alpine \
  sh -c 'while true; do nc -lv -p 8099 </dev/null; done'
curl -s --max-time 2 http://127.0.0.1:8099 > /dev/null
docker logs peer-probe | grep -m1 'connect to'
docker rm -f peer-probe
```

```text
connect to [::ffff:172.17.0.2]:8099 from [::ffff:192.168.65.1]:47158
```

The address after `from` is the one to trust. Note that it is not always a bridge gateway: on Docker Desktop for macOS or Windows, published ports arrive from the Desktop VM (`192.168.65.1` above) rather than from `172.x`. DSM runs Linux Docker, where the bridge gateway is the usual answer — but measuring costs a few seconds and removes the guess.

### Confirming the proxy is trusted

Nineveh checks this for you. At startup it compares its own container gateway against the trusted list and logs a warning if the gateway would be ignored. At runtime, the first time a request arrives carrying `X-Forwarded-Proto` that gets discarded, it logs the peer address and raises a banner on **Admin → Overview** naming the exact value to set:

```text
Forwarded headers are being ignored. A request arrived from 192.168.65.1 carrying
X-Forwarded-Proto, but only 127.0.0.1 is trusted, so Nineveh is treating it as
plain HTTP. Set NINEVEH_FORWARDED_ALLOW_IPS=192.168.65.1 in docker/.env and
recreate the container.
```

Nineveh does not widen the trust list on its own. It can discover the address in front of it, but not whether the thing at that address is *your* proxy — that stays your decision.

To check by hand, send the header the proxy sends and see whether Nineveh acted on it:

```sh
curl -sI -H 'X-Forwarded-Proto: https' \
  http://127.0.0.1:8080/api/v1/health/live | grep -i strict-transport
# strict-transport-security: max-age=31536000; includeSubDomains
```

The header appears only when the request scheme reads `https`, which only happens once the peer is trusted. No output means the value is still wrong — widen it and recreate the container with `up -d`, since environment is fixed when a container is created.

## 6. Sign in and connect clients

Open the public URL and sign in as the configured bootstrap administrator. Add reader accounts from **Admin**.

Configure an OPDS 2.0 client with:

```text
https://nineveh.example.com/opds/v2/catalog.json
```

Select HTTP Basic authentication and use a Nineveh username and password. New readers see only the libraries, content types, or series granted to them in **Admin**. Administrators retain full access and are the only users who can change accounts, libraries, settings, or scans.

## Maintenance

### Applying a configuration change

Three operations, three costs. Only the last one rebuilds anything:

| You changed | Command | What happens |
| --- | --- | --- |
| Nothing (just want a clean start) | `docker compose ... restart` | Same container, process restarts. A second or two. |
| An application setting saved in **Admin** | Use **Save and restart** | Nineveh exits gracefully and Compose restarts the same container with the persisted setting. |
| A value in `docker/.env` | `docker compose ... up -d` | Container is **recreated** from the existing image. Environment is fixed when a container is created, so a restart alone will not pick it up. No rebuild. Applies to every setting an administrator has not pinned in **Admin**. |
| The source code | `docker compose ... up --build -d` | Image is rebuilt, then the container is recreated. |

The memory ceiling remains a Docker-level setting and is read-only in Nineveh; changing it requires the Compose recreation described above.

`/state` is a bind mount, so accounts, grants, settings, the catalog, and the caches survive all
three. The catalog scan re-runs at startup but is incremental — unchanged
archives are skipped on a file size and timestamp comparison.

### Updating

Back up the state directory, update the source, and rebuild:

```sh
docker compose --env-file docker/.env -f docker/compose.yaml down
git pull --ff-only
docker compose --env-file docker/.env -f docker/compose.yaml up --build -d
```

The media directory is never modified. The persistent state directory preserves accounts, catalog identifiers, and cached thumbnails.

### Backups

Back up `/volume1/docker/nineveh/state`. Stop Nineveh before copying the SQLite files, or use a SQLite-aware backup tool while the service is running.

### Resource tuning

Nineveh's own memory use is small and flat: roughly 80 MB after startup and 120 MB
under active browsing, independent of library size. Scanning streams, so indexing
a large library does not grow the process. Three things determine peak RAM:

| Setting | Cost |
| --- | --- |
| `NINEVEH_ARCHIVE_CACHE_SIZE` | ~170 KB per cached archive (a 300-page CBZ) |
| `NINEVEH_HASH_WORKERS` | ~19 MiB per concurrent password verification |
| `NINEVEH_MAX_IMAGE_PIXELS` | ~3 bytes per pixel while rendering one cover |

Everything else — the thumbnail and page caches — is bounded **on disk** under
`/state`, so size those against free space, not RAM. On a NAS with plenty of
memory the operating system keeps them in its own page cache anyway.

The defaults target a modest NAS. On a constrained model:

```dotenv
NINEVEH_ARCHIVE_CACHE_SIZE=2
NINEVEH_THUMBNAIL_CACHE_MB=256
NINEVEH_PAGE_CACHE_MB=512
NINEVEH_MAX_IMAGE_PIXELS=40000000
NINEVEH_SCAN_INTERVAL_SECONDS=3600
NINEVEH_MEMORY_LIMIT=512m
```

On a well-provisioned model (8 GB or more):

```dotenv
NINEVEH_ARCHIVE_CACHE_SIZE=64
NINEVEH_THUMBNAIL_CACHE_MB=2048
NINEVEH_PAGE_CACHE_MB=8192
NINEVEH_HASH_WORKERS=6
NINEVEH_EXTRACT_WORKERS=8
NINEVEH_FEED_PAGE_SIZE=48
NINEVEH_PAGE_RANGE_LIMIT=200
NINEVEH_MAX_IMAGE_PIXELS=80000000
NINEVEH_SCAN_INTERVAL_SECONDS=3600
NINEVEH_MEMORY_LIMIT=2g
```

`NINEVEH_MAX_IMAGE_PIXELS` deserves a note: the default of 200 megapixels permits
a single cover to occupy roughly 575 MB while it is being resized. No real comic
scan approaches that, so lowering it is a cheap safety margin rather than a
restriction. Cover rendering is serialised, so only one such decode runs at a time.

A longer `NINEVEH_SCAN_INTERVAL_SECONDS` also lets the drives hibernate: each pass
stats every CBZ in the library. Set it to `0` and scan on demand from **Admin**
if you add books rarely.

`NINEVEH_MEMORY_LIMIT` (default `1g`) caps the container. It is a ceiling, not a
target — it exists so a runaway decode fails inside the container rather than
pressuring DSM. Container Manager can also apply CPU limits. Avoid multiple
application workers because each worker would duplicate caches and scheduled scans.

## Troubleshooting

- **The container exits immediately:** ensure the data directory exists, the state directory is writable, and an initial administrator password is configured.
- **No books appear:** confirm the exact `library/comics-or-manga/series/file.cbz` hierarchy, then run a scan from the admin page.
- **Permission denied:** verify `NINEVEH_PUID` and `NINEVEH_PGID` and the DSM ACLs on all mounted directories.
- **Login succeeds but returns to the login page:** HTTPS is required while `NINEVEH_SECURE_COOKIES=true`.
- **Feed links use the wrong hostname:** set `NINEVEH_PUBLIC_BASE_URL` to the external HTTPS origin.
- **An archive is skipped:** review container logs for malformed ZIP entries, unsupported page formats, encryption, or configured safety limits.
