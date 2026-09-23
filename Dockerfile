# Container image for the cloud-deployable shoebox pipelines.
#
# This is an *additional* way to run shoebox, not a replacement for a local
# checkout. Everything here is driven by the same `shoebox <command>` entry
# point a Mac or Windows install uses, so a pipeline behaves identically in
# both places.
#
# Deliberately excluded: Chrome. The price scraper, the Topps release scraper,
# and the TCDB browser all drive a real Chrome via undetected-chromedriver,
# which pins itself to the installed Chrome build and expects to survive a
# Cloudflare check. Those pipelines also read card scans and Excel workbooks
# from local paths, so they stay on a workstation -- see docs/deployment.md.
# Keeping Chrome out holds the image near 400MB instead of well over 1GB.
#
#   docker build -t shoebox .
#   docker run --rm shoebox --help
#
# Real config files are mounted at runtime (Secret Manager in Cloud Run; bind
# mounts locally) and located via SHOEBOX_CONFIG_PATH / EBAY_REST_CONFIG_PATH.
# See docs/deployment.md.

FROM python:3.11-slim

# - Unbuffered so logs reach the platform's collector as they happen rather
#   than in a lump when the process exits.
# - Console-only logging: the platform already captures stdout, and the
#   container filesystem is discarded when the execution ends.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SHOEBOX_LOG_DIR=-

# shoebox resolves configs/, its BigQuery schemas, and query_dir relative to the
# working directory, exactly as the Windows scheduled tasks do. Every entry
# point below therefore runs from the repo root.
WORKDIR /app

# Dependency install is its own layer, keyed on the files that declare them, so
# a code-only change does not reinstall the world.
COPY pyproject.toml README.md ./
COPY shoebox/__init__.py ./shoebox/
# undetected-chromedriver installs fine without Chrome (it only needs one at
# runtime), so the dependency set is exactly pyproject.toml's -- the image just
# never runs the pipelines that would launch a browser.
RUN pip install --upgrade pip \
    && pip install -e .

COPY shoebox/ ./shoebox/
COPY configs/bigquery/ ./configs/bigquery/
COPY configs/title_crosswalk.yaml ./configs/

# Real config files are mounted at runtime from a secret store; they are
# gitignored and must never be baked into an image.
#
# Run as a non-root user. The CLI calls ensure_runtime_dirs() before every
# command, which mkdirs each paths.*_dir from app.yaml, so the cloud copy of
# app.yaml must keep those relative (data, exports, scans, set_images, tools)
# and they must exist here, writable by the runtime user. exports/ is where the
# pipelines stage JSONL before shipping it to GCS.
RUN useradd --create-home --uid 1000 shoebox \
    && mkdir -p /app/exports/jsonl /app/data /app/scans /app/set_images /app/tools \
    && chown -R shoebox:shoebox /app
USER shoebox

# Every real invocation names a command, supplied by the job definition as
# container args. The default CMD just prints help, so an image run with no
# arguments is harmless rather than accidentally publishing something.
ENTRYPOINT ["shoebox"]
CMD ["--help"]
