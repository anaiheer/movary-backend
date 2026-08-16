Legacy Alembic revisions were squashed on 2026-03-21.

- Active migration history now starts at `e7f8a9b0c1d2_squashed_baseline_schema.py`.
- Older revision files are kept under `archive/legacy_versions/` for reference only.
- Databases already stamped at revision `e7f8a9b0c1d2` remain compatible because the squashed baseline reuses that final revision id.
- Databases still on an older intermediate revision should be upgraded with the archived chain before switching to this squashed history.
