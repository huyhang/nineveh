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
        log_config=None,
    )


if __name__ == "__main__":
    main()
