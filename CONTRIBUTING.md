# Contributing

Thanks for your interest! This started as a personal automation project for a
sports-card store and is shared as a reference/portfolio piece — issues and PRs
are welcome, but there's no guarantee of ongoing maintenance.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -U pip
pip install -e ".[dev]"
```

Copy the config templates and fill in your own values:

```bash
cp configs/app.example.yml configs/app.yaml
cp configs/gcp.example.json configs/gcp.json
cp configs/ebay_rest.example.json configs/ebay_rest.json
cp configs/ebay_legacy.example.json configs/ebay_legacy.json
cp tools/checklist_parallel_metadata.sample.xlsx tools/checklist_parallel_metadata.xlsm
```

See [`docs/setup`](docs/content/guides/setup.mdx) and [`docs/configuration`](docs/content/guides/configuration.mdx)
for the full walkthrough.

## Running tests

```bash
pytest -q
```

Tests point the settings loader at `configs/app.example.yml` (via
`tests/conftest.py`), so they run without any real credentials.

## Style / linting

The project uses [ruff](https://docs.astral.sh/ruff/) (config in
`pyproject.toml`) and CI enforces both:

```bash
ruff format .
ruff check --fix .
```

The tree is ruff-clean; please keep it that way. A handful of pandas boolean
masks are intentionally marked `# noqa: E712` (`== False` on a Series is the
correct idiom — you can't use `not` on a Series).

To run these automatically before each commit, install the pre-commit hook
(config in `.pre-commit-config.yaml`):

```bash
pre-commit install
```

## Guidelines

- Keep secrets out of the repo. Real `configs/*.json`, `configs/app.yaml`, and
  `tools/*.xlsm` are gitignored — never commit them.
- Store-specific values (business name, eBay policy/campaign IDs, category)
  belong in `configs/app.yaml` under `store:`, not hardcoded in source.
- Add or update tests for pure logic you touch (`utils/`, `transforms/`).

## Documentation

The docs are a [Nextra](https://nextra.site) site under `docs/`. The guides are
hand-written MDX in `docs/content/guides/`; the API reference in
`docs/content/api/` is generated from the Python docstrings and should not be
edited by hand.

```bash
cd docs
npm install
npm run api      # regenerate docs/content/api/ from docstrings (python scripts/generate_api_docs.py)
npm run dev      # http://localhost:3000
npm run build    # static export to docs/out/
```

Re-run `npm run api` after changing a docstring and commit the regenerated
pages. The guides are MDX, so a bare `<name>` or `{...}` outside a code span
is parsed as JSX — keep placeholders in backticks, and inside a table cell
escape `|` as `\|` even within backticks.
