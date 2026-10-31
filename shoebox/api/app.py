"""FastAPI application for the listing queue (see docs/content/guides/api.mdx).

The phone talks only to this: it reads the dropdown metadata, previews and
submits queue rows (enriched with the same ``build_enriched_json`` the
Streamlit UI and Excel loader use), saves drafts, uploads photos, and asks for
the queue to be listed. The enriched queue lands at ``paths.queue_uri`` in
GCS, where ``create-listings`` picks it up: on the workstation by hand, or as
the Cloud Run job this API starts (``POST /queue/listings``), which fetches
scans from OneDrive and reports progress to a run record the phone polls.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from shoebox.api.deps import (
    ApiContext,
    Clock,
    Loader,
    MetadataBundle,
    MetadataCache,
    get_ctx,
    require_api_key,
    utc_now,
)
from shoebox.api.queue_store import QueueStore
from shoebox.api.schemas import (
    Counts,
    DraftRequest,
    DraftRowIn,
    HealthResponse,
    ImageUploadResponse,
    ListingRunsResponse,
    MetadataRefreshResponse,
    MetadataResponse,
    PreviewResponse,
    QueueGetResponse,
    QueueRowIn,
    QueueRowsRequest,
    QueueWriteResponse,
    RowResult,
    StartListingRunRequest,
)
from shoebox.services.listing_runs import ListingRun, ListingRunStore, new_run_id
from shoebox.settings import Settings, get_settings
from shoebox.transforms.queue_enrichment import build_enriched_json

logger = logging.getLogger(__name__)

API_KEY_ENV = "SHOEBOX_API_KEY"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
UPLOADS_PREFIX = "uploads"
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
INCOMPLETE_ROW = "set, subset and card # are required"


def _shoebox_version() -> str:
    try:
        return package_version("shoebox")
    except PackageNotFoundError:
        return "unknown"


def _sniff_image(data: bytes) -> tuple[str, str] | None:
    """(content_type, extension) from magic bytes; the declared type is not trusted."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    return None


def enrich_rows(rows: list[QueueRowIn], bundle: MetadataBundle) -> list[RowResult]:
    """Run ``build_enriched_json`` per row and attach the UI's soft warnings."""
    results: list[RowResult] = []
    seen: dict[tuple[str, str, str, str], int] = {}
    for i, row in enumerate(rows):
        warnings: list[str] = []
        key = (row.set_name, row.subset_name, row.parallel_variety or "", row.card_number)
        if key in seen:
            warnings.append(f"duplicate of row {seen[key]}")
        else:
            seen[key] = i
        if not row.image_front:
            warnings.append("image_front missing")
        if not row.image_back:
            warnings.append("image_back missing")

        rec = build_enriched_json(row.to_queue_row(), bundle.cdf, bundle.pdf)
        if rec is None:
            results.append(RowResult(index=i, ok=False, error=INCOMPLETE_ROW, warnings=warnings))
            continue
        if "error" in rec:
            results.append(RowResult(index=i, ok=False, error=str(rec["error"]), warnings=warnings))
            continue
        if row.parallel_variety and not rec.get("parallel_variety"):
            warnings.append(
                f"parallel {row.parallel_variety!r} not in parallels view (listed as base)"
            )
        results.append(RowResult(index=i, ok=True, enriched=rec, warnings=warnings))
    return results


def _counts(results: list[RowResult]) -> Counts:
    ok = sum(1 for r in results if r.ok)
    return Counts(rows=len(results), ok=ok, errors=len(results) - ok)


def _draft_doc(
    kind: str, rows: list[Any], counts: Counts, *, submitted_at, metadata_version: str | None
) -> dict[str, Any]:
    return {
        "kind": kind,
        "submitted_at": submitted_at.isoformat(),
        "metadata_version": metadata_version,
        "counts": counts.model_dump(),
        "rows": [r.model_dump() for r in rows],
    }


def create_app(
    *,
    settings: Settings | None = None,
    api_key: str | None = None,
    metadata_loader: Loader | None = None,
    gcs: Any = None,
    metadata_ttl_s: int = 3600,
    clock: Clock | None = None,
    job_runner: Any = None,
    run_store: Any = None,
) -> FastAPI:
    """Build the application. Every collaborator can be injected for tests; the
    defaults are the real settings, ``SHOEBOX_API_KEY`` from the environment,
    the BigQuery metadata loader, a lazily constructed ``GCSClient``, the
    Cloud Run job client and the run-record store built on that GCS client."""
    settings = settings or get_settings()
    api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")
    # A secret created with `openssl rand -hex 32 | gcloud secrets create --data-file=-`
    # carries a trailing newline, and Cloud Run hands it to the env var verbatim
    # while a shell's $(...) strips it from the client side. Keys never contain
    # whitespace, so drop it here rather than make every operator remember `tr -d`.
    api_key = api_key.strip()
    if not api_key:
        raise RuntimeError(f"{API_KEY_ENV} is not set; the API refuses to start without a key")
    if not settings.paths.queue_uri:
        raise RuntimeError("paths.queue_uri is not set; the API needs a gs:// queue object")

    clock = clock or utc_now

    if metadata_loader is None:
        from shoebox.transforms.queue_enrichment import load_checklist_and_parallels

        metadata_loader = load_checklist_and_parallels

    def gcs_factory() -> Any:
        if gcs is not None:
            return gcs
        from shoebox.clients.gcs import GCSClient

        return GCSClient(settings=settings)

    class _LazyStore:
        """QueueStore whose GCS client is built on first use."""

        def __init__(self) -> None:
            self._store: QueueStore | None = None
            self.queue_uri = settings.paths.queue_uri
            from shoebox.api.queue_store import draft_uri_for

            self.draft_uri = draft_uri_for(self.queue_uri)

        def _get(self) -> QueueStore:
            if self._store is None:
                self._store = QueueStore(ctx.gcs, self.queue_uri)
            return self._store

        def write_queue(self, enriched):
            return self._get().write_queue(enriched)

        def write_draft(self, doc):
            return self._get().write_draft(doc)

        def read_draft(self):
            return self._get().read_draft()

    def job_runner_factory() -> Any:
        if job_runner is not None:
            return job_runner
        from shoebox.clients.cloud_run import CloudRunJobs

        return CloudRunJobs(settings.gcp.project_id, settings.cloud_run.region)

    def run_store_factory() -> Any:
        if run_store is not None:
            return run_store
        return ListingRunStore.from_settings(settings, gcs=ctx.gcs)

    ctx = ApiContext(
        settings=settings,
        api_key=api_key,
        metadata=MetadataCache(metadata_loader, ttl_s=metadata_ttl_s, clock=clock),
        queue_store=_LazyStore(),
        gcs_factory=gcs_factory,
        clock=clock,
        job_runner_factory=job_runner_factory,
        run_store_factory=run_store_factory,
    )

    app = FastAPI(title="shoebox listing-queue API", version=_shoebox_version())
    app.state.ctx = ctx
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # /health is the one to use from outside: on a run.app URL the Google front
    # end answers /healthz itself with its own 404 before the container sees it.
    # /healthz stays for local runs and anything already pointed at it.
    @app.get("/health", response_model=HealthResponse)
    @app.get("/healthz", response_model=HealthResponse, include_in_schema=False)
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok", version=_shoebox_version())

    router = APIRouter(dependencies=[Depends(require_api_key)])

    def _bundle(ctx: ApiContext) -> MetadataBundle:
        try:
            return ctx.metadata.get()
        except Exception as exc:  # BigQuery down on first load
            logger.exception("metadata load failed")
            raise HTTPException(status_code=503, detail=f"metadata unavailable: {exc}") from exc

    @router.get("/metadata", response_model=MetadataResponse)
    def get_metadata(
        request: Request,
        response: Response,
        ctx: Annotated[ApiContext, Depends(get_ctx)],
    ):
        bundle = _bundle(ctx)
        etag = f'W/"{bundle.version}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, max-age=0"
        return MetadataResponse(
            version=bundle.version,
            generated_at=bundle.loaded_at,
            ttl_s=ctx.metadata.ttl_s,
            counts=bundle.payload["counts"],
            sets=bundle.payload["sets"],
        )

    @router.post("/metadata/refresh", response_model=MetadataRefreshResponse)
    def refresh_metadata(ctx: Annotated[ApiContext, Depends(get_ctx)]):
        try:
            bundle = ctx.metadata.refresh()
        except Exception as exc:
            logger.exception("metadata refresh failed")
            raise HTTPException(status_code=503, detail=f"metadata unavailable: {exc}") from exc
        return MetadataRefreshResponse(
            version=bundle.version,
            generated_at=bundle.loaded_at,
            counts=bundle.payload["counts"],
        )

    @router.post("/queue/preview", response_model=PreviewResponse)
    def preview_queue(body: QueueRowsRequest, ctx: Annotated[ApiContext, Depends(get_ctx)]):
        bundle = _bundle(ctx)
        results = enrich_rows(body.rows, bundle)
        return PreviewResponse(
            rows=results, counts=_counts(results), metadata_version=bundle.version
        )

    @router.put("/queue", response_model=QueueWriteResponse)
    def put_queue(body: QueueRowsRequest, ctx: Annotated[ApiContext, Depends(get_ctx)]):
        bundle = _bundle(ctx)
        results = enrich_rows(body.rows, bundle)
        counts = _counts(results)
        if counts.errors:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "message": f"{counts.errors} rows failed enrichment",
                        "rows": [r.model_dump() for r in results if not r.ok],
                    }
                },
            )
        now = ctx.clock()
        store = ctx.queue_store
        # Enriched JSONL first: it is what create-listings reads. If the draft
        # write then fails, the queue is still current and GET /queue is stale.
        store.write_queue([r.enriched for r in results])
        store.write_draft(
            _draft_doc(
                "queue", body.rows, counts, submitted_at=now, metadata_version=bundle.version
            )
        )
        return QueueWriteResponse(
            kind="queue",
            submitted_at=now,
            counts=counts,
            queue_uri=store.queue_uri,
            draft_uri=store.draft_uri,
            metadata_version=bundle.version,
        )

    @router.put("/queue/draft", response_model=QueueWriteResponse)
    def put_draft(body: DraftRequest, ctx: Annotated[ApiContext, Depends(get_ctx)]):
        now = ctx.clock()
        store = ctx.queue_store
        counts = Counts(rows=len(body.rows), ok=len(body.rows), errors=0)
        store.write_draft(
            _draft_doc("draft", body.rows, counts, submitted_at=now, metadata_version=None)
        )
        return QueueWriteResponse(
            kind="draft",
            submitted_at=now,
            counts=counts,
            queue_uri=None,
            draft_uri=store.draft_uri,
            metadata_version=None,
        )

    @router.get("/queue", response_model=QueueGetResponse)
    def get_queue(ctx: Annotated[ApiContext, Depends(get_ctx)]):
        store = ctx.queue_store
        doc = store.read_draft()
        if not doc:
            return QueueGetResponse(
                kind=None,
                submitted_at=None,
                rows=[],
                counts=Counts(rows=0, ok=0, errors=0),
                queue_uri=store.queue_uri,
                draft_uri=store.draft_uri,
            )
        rows = [DraftRowIn(**r) for r in doc.get("rows", [])]
        counts = doc.get("counts") or {"rows": len(rows), "ok": len(rows), "errors": 0}
        return QueueGetResponse(
            kind=doc.get("kind"),
            submitted_at=doc.get("submitted_at"),
            rows=rows,
            counts=Counts(**counts),
            queue_uri=store.queue_uri,
            draft_uri=store.draft_uri,
        )

    @router.post("/queue/listings", response_model=ListingRun, status_code=202)
    def start_listing_run(
        body: StartListingRunRequest, ctx: Annotated[ApiContext, Depends(get_ctx)]
    ):
        """Ask the Cloud Run ``create-listings`` job to list the submitted queue."""
        if body.scans_folder and not ctx.settings.onedrive.enabled:
            raise HTTPException(
                status_code=422,
                detail={"message": "a scans folder needs OneDrive (onedrive in app.yaml)"},
            )
        doc = ctx.queue_store.read_draft()
        if not doc or doc.get("kind") != "queue":
            raise HTTPException(
                status_code=409,
                detail={"message": "submit the queue first (the latest save is not a queue)"},
            )
        store = ctx.run_store
        active = store.active(now=ctx.clock())
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail={"message": "a run is in progress", "run_id": active.run_id},
            )
        now = ctx.clock()
        counts = doc.get("counts") or {}
        run = ListingRun(
            run_id=new_run_id(now),
            state="requested",
            requested_at=now,
            publish=body.publish,
            schedule=body.schedule,
            queue_uri=ctx.queue_store.queue_uri,
            scans_folder=body.scans_folder,
            metadata_version=doc.get("metadata_version"),
            rows_total=int(counts.get("rows") or len(doc.get("rows") or [])),
        )
        store.write(run)
        try:
            run.execution = ctx.job_runner.run_job(ctx.settings.cloud_run.listings_job)
        except Exception as exc:
            logger.exception("could not start the create-listings job")
            run.state = "failed"
            run.error = f"could not start the Cloud Run job: {exc}"
            run.finished_at = ctx.clock()
            store.write(run)
            raise HTTPException(status_code=502, detail=run.error) from exc
        if run.execution:
            store.write(run)
        return run

    @router.get("/queue/listings", response_model=ListingRunsResponse)
    def list_listing_runs(ctx: Annotated[ApiContext, Depends(get_ctx)]):
        return ListingRunsResponse(runs=[r.summary() for r in ctx.run_store.list()])

    @router.get("/queue/listings/{run_id}", response_model=ListingRun)
    def get_listing_run(run_id: str, ctx: Annotated[ApiContext, Depends(get_ctx)]):
        found = ctx.run_store.read(run_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        return found[0]

    @router.post("/images", response_model=ImageUploadResponse, status_code=201)
    async def upload_image(
        ctx: Annotated[ApiContext, Depends(get_ctx)],
        file: Annotated[bytes, File()],
        side: Annotated[str, Form()],
        client_id: Annotated[str | None, Form()] = None,
    ):
        if side not in ("front", "back"):
            raise HTTPException(status_code=422, detail="side must be 'front' or 'back'")
        if client_id is not None and not CLIENT_ID_RE.fullmatch(client_id):
            raise HTTPException(
                status_code=422, detail="client_id must match ^[A-Za-z0-9_-]{8,64}$"
            )
        if len(file) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="image larger than 10 MiB")
        sniffed = _sniff_image(file)
        if sniffed is None:
            raise HTTPException(status_code=415, detail="only JPEG or PNG images are accepted")
        content_type, ext = sniffed
        stem = client_id or uuid.uuid4().hex
        month = ctx.clock().strftime("%Y-%m")
        object_name = f"{UPLOADS_PREFIX}/{month}/{stem}.{ext}"
        metadata = {"side": side}
        if client_id:
            metadata["client_id"] = client_id
        uri = ctx.gcs.upload_bytes(
            bucket=ctx.settings.gcs.image_bucket,
            object_name=object_name,
            data=file,
            content_type=content_type,
            metadata=metadata,
        )
        return ImageUploadResponse(
            uri=uri, side=side, client_id=client_id, bytes=len(file), content_type=content_type
        )

    app.include_router(router)
    return app
