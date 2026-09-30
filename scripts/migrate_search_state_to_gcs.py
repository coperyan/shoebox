"""One-off: upload the workstation's saved-search state to GCS.

Moves ``<exports_dir>/jsonl/searches/`` -- the seen-caches, ``search_state.json``
and any unflushed hits buffer -- to ``paths.searches_state_uri``, so the Cloud
Run job picks up exactly where this machine stopped: no re-seed, no gap, and
nothing already alerted alerts again.

    python scripts/migrate_search_state_to_gcs.py                 # report only
    python scripts/migrate_search_state_to_gcs.py --apply
    python scripts/migrate_search_state_to_gcs.py --apply --uri gs://bucket/searches/state

Run it after the Windows ``watch-searches`` task is disabled and before the
cloud schedule is enabled (docs/deployment.md, "Saved searches"). It holds both
locks while it copies -- the local ``.lock`` a Windows tick would take and the
GCS lock a cloud run would take -- so neither can run halfway through.

It refuses to overwrite a prefix that already holds state (the cloud job may
have seeded it) unless ``--force`` is passed, in which case GCS is made an
exact copy of the local directory. The local files are left untouched, so
falling back to the workstation is just re-enabling its task.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from shoebox.clients.search_state import LOCK_FILENAME, SearchStateStore
from shoebox.clients.search_state_gcs import GCSSearchStateStore
from shoebox.settings import get_settings


def _local_files(store: SearchStateStore) -> list[Path]:
    return sorted(
        p
        for p in store.base_dir.iterdir()
        if p.is_file()
        and p.name != LOCK_FILENAME
        and not p.name.endswith((".tmp", ".premigration"))
    )


def migrate(
    *,
    local: SearchStateStore,
    remote: GCSSearchStateStore,
    apply: bool,
    force: bool,
) -> int:
    files = _local_files(local)
    print(f"Local state: {local.base_dir} ({len(files)} file(s))")
    for path in files:
        print(f"  {path.name:<40} {path.stat().st_size:>10,} bytes")
    if not files:
        print("Nothing to migrate.")
        return 0

    if not apply:
        print(f"\nWould upload to {remote.uri}. Re-run with --apply.")
        return 0

    with local.lock() as got_local:
        if not got_local:
            print("A local watch-searches run holds the lock; try again when it finishes.")
            return 1
        with remote.lock() as got_remote:
            if not got_remote:
                print(f"A watch-searches run holds the lock on {remote.uri}; try again shortly.")
                return 1

            existing = sorted(p.name for p in remote.base_dir.iterdir() if p.is_file())
            if existing and not force:
                print(
                    f"{remote.uri} already holds {len(existing)} state file(s): "
                    f"{', '.join(existing)}\nRefusing to overwrite; pass --force to replace them."
                )
                return 1

            # Make the scratch copy an exact mirror of the local directory; the
            # lock's exit uploads it and deletes whatever is no longer there.
            for path in remote.base_dir.iterdir():
                if path.is_file():
                    path.unlink()
            for path in files:
                shutil.copy2(path, remote.base_dir / path.name)

    print(f"Uploaded {len(files)} file(s) to {remote.uri}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--uri", help="Destination (default: paths.searches_state_uri)")
    parser.add_argument("--apply", action="store_true", help="Upload; default is report only")
    parser.add_argument(
        "--force", action="store_true", help="Replace state already present at the destination"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    uri = args.uri or settings.paths.searches_state_uri
    if not uri:
        parser.error("no destination: pass --uri or set paths.searches_state_uri in app.yaml")

    # Explicitly the *local* directory, whatever searches_state_uri says.
    local = SearchStateStore(settings=settings)
    remote = GCSSearchStateStore(uri, settings=settings)
    return migrate(local=local, remote=remote, apply=args.apply, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
