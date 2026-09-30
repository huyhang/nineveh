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

Keep the state directory outside the media tree. The media mount is writable so the
librarian agent can place validated volumes; scans never modify it, and placement
never replaces an existing file.

### More than one media root

Nineveh indexes several *data mounts*. Docker attaches the host directory; the
admin page then registers it. Add a line per root to the service's `volumes`,
at a stable container path:

```yaml
volumes:
  - "/volume1/media/nineveh:/data"
  - "/volume2/media/archive:/media/archive:ro"
  - "/volume3/comics:/media/comics"
```

Recreate the service, then open **Admin → Libraries → Add data mount** and enter
the *container* path — `/media/archive`, not `/volume2/media/archive`. Choose
which of its directories to index; a folder whose name is already taken by
another mount's library needs a display name of its own.

`:ro` is the right default. Leave it off only where librarian ingest is meant
to be enabled, and turn ingest on for that mount explicitly — mounts are
read-only to the agent until you do.

Disconnecting a mount in Nineveh hides its libraries and changes nothing in
Docker; the media, the index and the grants all survive. Remove the Compose
volume only after disconnecting, or the mount simply reports itself as missing.

## 2. Determine the service account IDs

Through SSH, run `id USERNAME` for the DSM account that should run Nineveh. Record its numeric UID and GID. That account needs:

- Read and directory-traversal permission for the media tree, plus write permission
  where the librarian places new volumes (nothing else writes to it). With no
  ingest, set `NINEVEH_DATA_MODE=ro` and grant read only.
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
6. Compose publishes port 8080 on loopback only, so nothing but the proxies on
   the NAS itself can reach it.
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

## 6. Private administration over Tailscale or the LAN

The public hostname should serve readers only. Give administration its own
front doors that the Internet cannot reach, and tell Nineveh about them. Use
either or both.

**Tailscale.** Install the Tailscale package on the NAS and publish the
loopback listener to your tailnet with Serve — never Funnel, which is public:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8080
tailscale serve status   # shows https://nas.your-tailnet.ts.net
```

**LAN.** In **Control Panel → Login Portal → Advanced → Reverse Proxy**, add a
second rule: HTTPS source on the NAS's LAN address and a free port (DSM itself
uses 5000/5001), for example `192.168.1.10:5443`, destination
`http://127.0.0.1:8080`, with the same headers preserved as the public rule.
The certificate can be DSM's own; your browser will ask you to accept it once.
Restrict that port to your LAN in the DSM firewall if you like — Nineveh checks
the client address regardless.

Then list both origins, and the networks their clients come from, in
`docker/.env`:

```dotenv
NINEVEH_PRIVATE_BASE_URLS=https://nas.your-tailnet.ts.net,https://192.168.1.10:5443
NINEVEH_PRIVATE_ALLOW_IPS=100.64.0.0/10,fd7a:115c:a1e0::/48,192.168.1.0/24
```

Both proxies run on the NAS, so both reach the container from the gateway
address found in step 5, and the `NINEVEH_FORWARDED_ALLOW_IPS` value set there
already covers them. Recreate the container with `up -d`.

A request is private only when it names one of those origins *and* its client
address falls in one of those networks. From then on:

- administrator accounts cannot sign in through the public hostname — a
  correct password there is refused exactly like a wrong one;
- the admin pages and APIs, the API docs, and the librarian API return 403 on
  the public hostname;
- the librarian agent must use a private origin, over the tailnet.

To confirm it, ask the readiness endpoint on each origin. Only a private one
includes scan detail:

```sh
curl -s https://nas.your-tailnet.ts.net/api/v1/health/ready   # {"status":"ok","catalog":{...}}
curl -s https://nineveh.example.com/api/v1/health/ready       # {"status":"ok"}
```

If a private origin answers with only a status, Nineveh does not see the
client's real address: check that the proxy is trusted (step 5) and that the
client's network is in `NINEVEH_PRIVATE_ALLOW_IPS`. A private origin that
answers *Unrecognized request origin* is missing from
`NINEVEH_PRIVATE_BASE_URLS`, or is spelled with a different port.

## 7. Sign in and connect clients

Open a private URL and sign in as the configured bootstrap administrator. Add reader accounts from **Admin**, and use one of those — not an administrator — to read through the public URL.

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

Coming from a release before the security hardening? Read the next section before you rebuild.

### What to watch for to migrate existing Nineveh to security-hardened version

An existing `docker/.env` keeps working: split access stays off until you name
a private origin, and the library mount stays writable. Four things can still
catch you on the first `up --build -d`, and a few behaviours change on purpose.

#### Before you rebuild

1. **The port is published on loopback only.** Compose now binds
   `127.0.0.1:${NINEVEH_PORT}` instead of every interface. A DSM reverse-proxy
   rule whose destination is `http://127.0.0.1:8080` (or `localhost`) keeps
   working. One pointed at the NAS's LAN address stops, and so does anything
   that talks to `http://NAS_ADDRESS:8080` directly: an OPDS app, a script, or
   the librarian agent using the tailnet address and port. Point those at a
   proxy address, or restore the old binding with
   `NINEVEH_PUBLISH_ADDRESS=0.0.0.0` — plain HTTP, and pointless once split
   access is on, since Nineveh then refuses origins it was not configured with.

2. **The proxy must be trusted.** `NINEVEH_FORWARDED_ALLOW_IPS` is not new,
   but more now depends on it: sign-in delays and per-account limits are kept
   per client address. If the proxy's gateway is not trusted, every request
   appears to come from that gateway, so one person's mistyped password slows
   everyone's sign-in — and under split access nobody counts as private, so no
   administrator can sign in anywhere. Set it as described in
   [Finding the address your proxy arrives from](#finding-the-address-your-proxy-arrives-from)
   and check it with [Confirming the proxy is trusted](#confirming-the-proxy-is-trusted).
   **Admin → Overview** raises a banner naming the right address when it is wrong.

3. **The CPU cap.** Compose now sets `cpus: ${NINEVEH_CPU_LIMIT:-2.0}`. Docker
   refuses to create the container if the NAS has fewer cores than that, or if
   its kernel cannot enforce CPU limits. If `up` fails with a CPU error, set
   `NINEVEH_CPU_LIMIT` to your core count, or to `0` to leave the cap out
   entirely. A kernel without the pids controller may print a warning that the
   PIDs limit was discarded; the container still starts.

4. **The rebuild downloads pinned packages.** The image installs only the
   hash-pinned versions in `requirements*.lock`, so the NAS needs Internet
   access to PyPI while building. A hash mismatch means a download is not the
   file that was pinned; do not work around it.

#### What behaves differently afterwards

None of these is a fault:

- `/docs`, `/redoc` and `/openapi.json` answer administrators only
  (anonymous callers get 401, readers 403); under split access, only on a
  private origin.
- Every failed sign-in makes the next one from that address and account wait
  longer — 1 second, doubling to 30 — and an address failing across many
  accounts is slowed too. Nothing is ever locked, and failures are forgotten
  after 15 quiet minutes. An OPDS client stuck on an old password will make
  sign-ins from its network slow until it is fixed; **Admin → Overview → Security**
  lists failed sign-ins with their address.
- Heavy use waits its turn instead of failing: image rendering, page-range
  generation, and more than four downloads at once per account queue fairly
  between accounts. Only a runaway backlog is refused, with `429` and
  `Retry-After`, and recorded under Security.
- Request bodies over 1 MiB are refused with `413`, except cover and librarian
  uploads, which keep their own ceilings. A page range that would expand past
  512 MiB is refused with `413`. Generated ranges and uploads stop with `507`
  when the state volume would drop below 512 MiB free.
- The default image-pixel ceiling is 80 megapixels, down from 200. A volume
  with a larger page cannot be opened in the browser reader and its covers
  cannot be generated; raise the ceiling in **Admin → Settings** if a real scan
  hits it. A value you had already saved there is kept.
- Each librarian token may have 10 uploads, and 20 GiB, staged at once; past
  that it gets `507` until something is committed or discarded. Its "last
  used" time updates at most once a minute.
- Downloaded and uploaded metadata covers are decoded in a separate worker
  process, so saving a match or a custom cover takes a moment longer.

#### Turning on split access

This is the change the hardening exists for, and the only one that needs new
lines in `docker/.env`:

```dotenv
NINEVEH_PRIVATE_BASE_URLS=https://your-nas.your-tailnet.ts.net,https://192.168.1.10:5443
NINEVEH_PRIVATE_ALLOW_IPS=100.64.0.0/10,fd7a:115c:a1e0::/48,192.168.1.0/24
```

Set up Tailscale Serve and the LAN proxy rule first, as described in
[section 6](#6-private-administration-over-tailscale-or-the-lan). Nineveh
refuses to start in this mode unless `NINEVEH_PUBLIC_BASE_URL` is set and uses
`https://`, every private origin uses `https://`, `NINEVEH_SECURE_COOKIES` is
`true`, and `NINEVEH_FORWARDED_ALLOW_IPS` names addresses rather than `*`. The
container log says which one is missing.

Once it is on:

- Administrator accounts sign in only on a private origin. On the public URL
  their correct password is answered exactly like a wrong one, so move any app
  or OPDS client signed in as an administrator to a reader account.
- The librarian agent must use a private origin; the public URL answers `403`
  for the whole librarian API.
- Links in feeds and pages name whichever origin the request arrived on.
- `/api/v1/health/ready` includes scan detail only on a private origin. Point
  any monitoring that reads it at one.

#### Optional

If the librarian never places volumes in the primary library, mount it
read-only with `NINEVEH_DATA_MODE=ro`. Leave it writable if it does, or ingest
fails with *Ingest destination is not writable*. The fair-use and storage
settings in `docker/.env.example` all have working defaults.

#### Checking the result

After `up --build -d`:

- **Admin → Overview** shows no *Forwarded headers are being ignored* banner,
  and — once split access is on — no *Administration is reachable from any
  address* banner either.
- Only a private origin returns scan detail:

  ```sh
  curl -s https://your-nas.your-tailnet.ts.net/api/v1/health/ready   # {"status":"ok","catalog":{...}}
  curl -s https://nineveh.example.com/api/v1/health/ready            # {"status":"ok"}
  ```

  A private origin that returns only a status means Nineveh does not see the
  client's real address (item 2 above, or a network missing from
  `NINEVEH_PRIVATE_ALLOW_IPS`). One that answers *Unrecognized request origin*
  is missing from `NINEVEH_PRIVATE_BASE_URLS`, or is listed with another port.

- Signing in as an administrator on the public URL fails as if the password
  were wrong; the same account signs in on a private origin.

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
NINEVEH_SCAN_INTERVAL_SECONDS=3600
NINEVEH_MEMORY_LIMIT=2g
NINEVEH_CPU_LIMIT=4.0
NINEVEH_DOWNLOAD_STREAMS=32
NINEVEH_RANGE_WORKERS=2
```

`NINEVEH_MAX_IMAGE_PIXELS` deserves a note: the default of 80 megapixels still
admits any real comic scan while bounding one decode to roughly 230 MB. The
earlier default of 200 megapixels allowed about 575 MB, enough for two
concurrent renders to threaten a 1 GB container.

The extract workers also set how many images are decoded at once for all
readers together. Extra requests wait their turn without holding a thread, and
the next free worker goes to whichever account is waiting with the fewest in
progress, so raising the count buys throughput, not fairness.

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
