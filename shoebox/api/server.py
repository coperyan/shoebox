"""Run the listing-queue API under uvicorn (``shoebox api``)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def serve(*, host: str = "0.0.0.0", port: int = 8080) -> None:
    import uvicorn

    from shoebox.api.app import create_app

    # Built eagerly so a missing SHOEBOX_API_KEY or paths.queue_uri fails at
    # startup, in the Cloud Run logs, rather than on the first request.
    app = create_app()
    logger.info("Serving listing-queue API on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True)
