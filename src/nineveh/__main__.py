from __future__ import annotations

import os

import uvicorn

from .observability import configure_logging


def main() -> None:
    configure_logging(os.getenv("NINEVEH_LOG_LEVEL", "INFO"))
    uvicorn.run(
        "nineveh.app:create_app",
        factory=True,
        host=os.getenv("NINEVEH_HOST", "0.0.0.0"),
        port=int(os.getenv("NINEVEH_PORT", "8080")),
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("NINEVEH_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        # A memory backstop, not a fairness control: requests queued for a
        # fair share hold a connection, so this sits well above any backlog
        # the admission gates allow. Per-client limits belong to the proxy.
        limit_concurrency=int(os.getenv("NINEVEH_CONNECTION_LIMIT", "1024")),
        timeout_graceful_shutdown=15,
        server_header=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
