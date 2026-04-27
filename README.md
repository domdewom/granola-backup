# Granola Backup Exporter

Daily incremental backup for Granola meetings. The exporter writes generated
artifacts locally under `backups/`; those generated artifacts are ignored on
`main` by default.

This is an unofficial exporter that relies on Granola API behavior observed
from the desktop app. It is not affiliated with or endorsed by Granola.

For each meeting, it exports:

- `notes.md` and `notes.json`
- `enhanced.md` and `enhanced.json`
- `transcript.md` and `transcript.json`
- `meeting.json` (metadata snapshot)

Meeting folder naming:

- `YYYYMMDD_Title` (example: `20260303_Allie`)
- if two meetings collide on same date/title, exporter appends `--<id8>`

## Use This Template

This repository is designed to be copied into your own private GitHub repo.
When used as a GitHub template, the workflow and exporter code are copied, but
your GitHub Secrets and backup data are not.

Recommended setup:

1. Create a new private repo from this template.
2. Add the `GRANOLA_SUPABASE_JSON` repository secret.
3. Run the `Granola Backup` workflow manually once from the Actions tab.
4. Keep `main` for code and config.
5. Let the workflow publish generated backups to the `granola-backups` branch.

## Privacy and Security

Exported backups contain private meeting content, transcripts, notes, metadata,
and AI summaries. Treat the backup branch and any local `backups/` folder as
sensitive data.

- Keep repos containing real backups private.
- Do not commit `.env`, `supabase.json`, access tokens, or refresh tokens.
- Do not make a repo public if its Git history ever contained real backups.
- For public sharing, create a fresh repo with clean history and no generated
  backup files.

## Layout

- `scripts/export_granola.py`: main exporter
- `.github/workflows/granola-backup.yml`: daily workflow
- `backup.config.yaml`: runtime config
- `backups/`: generated output artifacts and manifests, ignored by Git
- `analysis/`: local analysis workspace, ignored by Git

Export paths:

- `backups/granola-md/<meeting-folder>/...`
- `backups/granola-json/<meeting-folder>/...`
- `backups/manifests/...`

## Generated Artifacts and Git

- `backups/` and `analysis/` are ignored in `.gitignore`.
- Existing backup files have been removed from Git tracking with `git rm --cached`; the files can still exist locally.
- Moving `backups/` or `analysis/` out of this repo will not remove tracked files from Git, because neither folder is currently tracked.
- The exporter always recreates `backups/` inside the repo on the next run. If `backups/manifests/sync_state.json` is moved away, the next run has no incremental marker and behaves like a first export.

## GitHub Actions Backup Branch

The scheduled workflow runs from `main`, using the latest exporter code from
`main`, but publishes generated backup files to a separate `granola-backups`
branch.

- `main` contains code, config, docs, and workflow files.
- `granola-backups` contains generated `backups/` output only.
- Before each export, the workflow restores `backups/` from `granola-backups` if the branch exists, so incremental state is preserved in `backups/manifests/sync_state.json`.
- After each export, the workflow replaces the backup branch contents with the newly generated `backups/` tree.
- Code changes only need to be made on `main`; the next scheduled run automatically uses them.

## Required GitHub Secret

- `GRANOLA_SUPABASE_JSON`: full contents of your Granola `supabase.json`

On macOS, this file is typically at:

- `~/Library/Application Support/Granola/supabase.json`

## Optional Secrets

- `GRANOLA_CLIENT_ID`: override client id (default: `client_GranolaMac`)
- `GRANOLA_API_BASE`: override API base (default: `https://api.granola.ai`)
- `BACKUP_WORKSPACE_ID`: restrict backup to a single workspace
- `ALLOW_LARGE_DROP`: set to `true` to bypass health guard if meeting count drops unexpectedly
- `ALLOW_EMPTY_CONTENT_OVERWRITE`: set to `true` to allow empty/null API responses to overwrite existing non-empty export files

## Local Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# one-time setup
cp .env.example .env
# then paste your real supabase.json content into GRANOLA_SUPABASE_JSON in .env

# run (script auto-loads .env)
python scripts/export_granola.py
```

## GitHub Actions Setup

The workflow can run on a schedule or manually from the Actions tab. It expects
the `GRANOLA_SUPABASE_JSON` repository secret to be present.

To add the secret:

1. Open your GitHub repo.
2. Go to **Settings** -> **Secrets and variables** -> **Actions**.
3. Add a new repository secret named `GRANOLA_SUPABASE_JSON`.
4. Paste the full contents of your local Granola `supabase.json` file.

The workflow is scheduled with:

```yaml
cron: "0 2 * * *"
```

You can change that cron expression in `.github/workflows/granola-backup.yml`.

## Progress Output

- Local terminal (`PROGRESS_MODE=auto`, default): shows spinner/progress and phase updates.
- GitHub Actions (`GITHUB_ACTIONS=true`): progress UI is suppressed by default.

Optional controls:

- `PROGRESS_MODE=auto|rich|plain|off`
- `PROGRESS_LOG_EVERY=25` (for plain mode update cadence)

## Incremental State

State is stored in `backups/manifests/sync_state.json`.

- First run exports all meetings.
- Later runs export only meetings with `updated_at` newer than the last successful sync marker.
- The run manifest records per-file statuses, content counts, missing-content warnings, and skipped empty overwrites.

Use `FULL_EXPORT=true` to force a full re-export.

## Notes vs Enhanced

- `notes.md` contains manual notes typed into Granola. It can be empty even when a transcript exists.
- `enhanced.md` contains Granola's AI-generated summary from the document panel.
- The exporter skips overwriting existing non-empty export files with empty/null content unless `ALLOW_EMPTY_CONTENT_OVERWRITE=true` is set.
