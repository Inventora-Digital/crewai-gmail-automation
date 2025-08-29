# Web App Guide

This project includes a minimal FastAPI web app with a static UI to run the crew, provide Gmail credentials, and stream logs for each run.

## Run Locally

- Start server:
  - `uv run serve`
  - Default port: `8080` (override with `PORT=9000 uv run serve`)

- Open UI:
  - `http://localhost:8080/ui/`

- Health check:
  - `http://localhost:8080/health`

## Endpoints

- `POST /api/runs` — start a run
  - JSON body: `{ "email_address": string, "app_password": string, "email_limit": number }`
  - The server masks the email and redacts the app password from logs.

- `GET /api/runs` — list all runs

- `GET /api/runs/{id}/logs?start=N` — tail logs from index `N`

- `GET /api/output` — list output JSON files in `output/`

- `GET /api/output/{name}` — read a specific output JSON file

- `GET /api/summary` — categorized summary for UI
  - Deleted Emails (from `cleanup_plan.json`)
  - Read Only (from `categorization_report.json`)
  - Drafts (from `response_plan.json`)

## UI Features

- New Run form (email, app password, email limit)
- Runs list with per-run selection
- Logs tab with live updates, autoscroll, and copy-to-clipboard
- Outputs tab listing `output/*.json` with JSON preview
- Summary tab grouping Deleted Emails, Read Only, Drafts
- Status tab with counters (running, completed, failed)

## Notes

- Use a Gmail App Password for IMAP. See README for setup.
- Logs are stored in `output/server.log`. Follow with `tail -f output/server.log`.
- Static UI is served from `src/web`. You can override with `WEB_DIR=/path/to/web uv run serve`.

