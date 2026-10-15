"""Shared state and dependencies for the listing-queue API."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

import pandas as pd
from fastapi import Header, HTTPException, Request

from shoebox.settings import Settings
from shoebox.transforms.queue_indexes import build_metadata_payload, metadata_version

Loader = Callable[[], tuple[pd.DataFrame, pd.DataFrame]]
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class MetadataBundle:
    cdf: pd.DataFrame
    pdf: pd.DataFrame
    payload: dict[str, Any]
    version: str
    loaded_at: datetime


class MetadataCache:
    """The checklist/parallels views, loaded lazily and kept for ``ttl_s``.

    Mirrors the Streamlit UI's hour-long ``st.cache_data``. A lock makes
    concurrent first requests run one BigQuery load; a failed reload keeps the
    previous bundle so a BigQuery hiccup never blanks the phone's pickers.
    """

    def __init__(self, loader: Loader, *, ttl_s: int = 3600, clock: Clock = utc_now):
        self._loader = loader
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._bundle: MetadataBundle | None = None

    def _load(self) -> MetadataBundle:
        cdf, pdf = self._loader()
        payload = build_metadata_payload(cdf, pdf)
        return MetadataBundle(
            cdf=cdf,
            pdf=pdf,
            payload=payload,
            version=metadata_version(payload),
            loaded_at=self._clock(),
        )

    def _expired(self) -> bool:
        if self._bundle is None:
            return True
        age = (self._clock() - self._bundle.loaded_at).total_seconds()
        return age >= self.ttl_s

    def get(self) -> MetadataBundle:
        if not self._expired():
            return self._bundle  # type: ignore[return-value]
        with self._lock:
            if not self._expired():
                return self._bundle  # type: ignore[return-value]
            try:
                self._bundle = self._load()
            except Exception:
                if self._bundle is None:
                    raise
                # Serve the stale bundle; the next request after the TTL tries again.
                self._bundle.loaded_at = self._clock()
            return self._bundle

    def refresh(self) -> MetadataBundle:
        with self._lock:
            self._bundle = self._load()
            return self._bundle


@dataclass
class ApiContext:
    settings: Settings
    api_key: str
    metadata: MetadataCache
    queue_store: Any
    gcs_factory: Callable[[], Any]
    clock: Clock = utc_now
    job_runner_factory: Callable[[], Any] | None = None
    run_store_factory: Callable[[], Any] | None = None
    _gcs: Any = field(default=None, repr=False)
    _job_runner: Any = field(default=None, repr=False)
    _run_store: Any = field(default=None, repr=False)

    @property
    def gcs(self) -> Any:
        if self._gcs is None:
            self._gcs = self.gcs_factory()
        return self._gcs

    @property
    def job_runner(self) -> Any:
        if self._job_runner is None:
            if self.job_runner_factory is None:
                raise RuntimeError("no Cloud Run job runner configured")
            self._job_runner = self.job_runner_factory()
        return self._job_runner

    @property
    def run_store(self) -> Any:
        if self._run_store is None:
            if self.run_store_factory is None:
                raise RuntimeError("no listing-run store configured")
            self._run_store = self.run_store_factory()
        return self._run_store


def get_ctx(request: Request) -> ApiContext:
    return request.app.state.ctx


def require_api_key(
    request: Request,
    x_api_key: Annotated[str, Header()] = "",
) -> None:
    ctx: ApiContext = request.app.state.ctx
    if not x_api_key or not secrets.compare_digest(
        x_api_key.encode("utf-8"), ctx.api_key.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
