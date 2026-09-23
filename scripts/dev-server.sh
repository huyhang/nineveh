#!/usr/bin/env bash
# Run Nineveh from this checkout, without Docker, reloading on every edit.
#
#     scripts/dev-server.sh            # serve on http://127.0.0.1:8081
#     scripts/dev-server.sh --reset    # start again from a fresh sample library
#     scripts/dev-server.sh token      # issue a read-only librarian token
#
# Everything it creates lives in .dev/ (gitignored): a generated sample library,
# a state directory, and the admin password. The Docker container, example-data/
# and state/ are never touched, so both can run side by side.
#
#     NINEVEH_DEV_PORT=8082 scripts/dev-server.sh
#     NINEVEH_DEV_DATA="$PWD/example-data" scripts/dev-server.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV="$ROOT/.dev"
PORT="${NINEVEH_DEV_PORT:-8081}"
DATA="${NINEVEH_DEV_DATA:-$DEV/data}"
PASSWORD_FILE="$DEV/admin-password"
# Pinned rather than read from NINEVEH_ADMIN_USERNAME: a value left in the shell
# would name the account one thing and this script's banner and token another.
ADMIN=admin
PYTHON="$ROOT/.venv/bin/python"

[ -x "$PYTHON" ] || { echo "No virtualenv at $ROOT/.venv — see README, Local development" >&2; exit 1; }

if [ "${1:-}" = "token" ]; then
  [ -f "$PASSWORD_FILE" ] || { echo "Start the dev server first" >&2; exit 1; }
  # Basic auth rather than a session, so no CSRF token is needed. The secret
  # exists only in this response; Nineveh stores its hash.
  curl --fail-with-body -sS -u "$ADMIN:$(cat "$PASSWORD_FILE")" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"dev $(date +%Y%m%d-%H%M%S)\", \"scopes\": [\"catalog:read\", \"metadata:read\"]}" \
    "http://127.0.0.1:$PORT/api/v1/admin/librarian-tokens" |
    "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["secret"])' |
    { read -r secret; printf 'export NINEVEH_URL=http://127.0.0.1:%s\nexport NINEVEH_TOKEN=%s\n' "$PORT" "$secret"; }
  exit 0
fi

if [ "${1:-}" = "--reset" ]; then
  rm -rf "$DEV"
elif [ -n "${1:-}" ]; then
  echo "usage: scripts/dev-server.sh [--reset | token]" >&2
  exit 2
fi

mkdir -p "$DEV/state"
if [ -z "${NINEVEH_DEV_DATA:-}" ] && [ ! -d "$DATA" ]; then
  "$PYTHON" "$ROOT/docker/create-sample-library.py" "$DATA" >/dev/null
fi
if [ ! -f "$PASSWORD_FILE" ]; then
  (umask 077 && "$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(18))' >"$PASSWORD_FILE")
fi

echo "Nineveh (dev) on http://127.0.0.1:$PORT — $ADMIN / $(cat "$PASSWORD_FILE")"
echo "Library: $DATA"

export NINEVEH_DATA_DIR="$DATA"
export NINEVEH_STATE_DIR="$DEV/state"
export NINEVEH_ADMIN_USERNAME="$ADMIN"
export NINEVEH_ADMIN_PASSWORD_FILE="$PASSWORD_FILE"
# Plain HTTP on localhost: a Secure cookie would never come back from Safari.
export NINEVEH_SECURE_COOKIES=false

cd "$ROOT"
# Bound to loopback, unlike the container: this is for testing from this Mac.
exec "$ROOT/.venv/bin/uvicorn" nineveh.app:create_app --factory \
  --host 127.0.0.1 --port "$PORT" --reload --reload-dir src
