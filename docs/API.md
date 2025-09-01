# Agents API Reference

This document describes the HTTP API exposed by the Gmail Crew AI server. Use it to start runs, stream logs, and fetch results programmatically. The API is served by FastAPI and returns JSON.

## Base URL

- Local: `http://localhost:8080`
- Cloud Run: `https://<service>-<hash>-uc.a.run.app` (or your custom domain)
- All endpoints below are relative to `/api` unless noted.

## Authentication

Two modes are supported:

- Public access: If your service allows unauthenticated invocations, most endpoints work without a token. You must pass `email_address` and `app_password` in the request body to start a run.
- Firebase ID token: If Firebase is configured and you include `Authorization: Bearer <id_token>`, the server can load your saved settings (email + app password) from Secret Manager / Firestore and you may omit credentials in `POST /api/runs`. Endpoints under `/api/me/*` require authentication.

Helper endpoints:
- `GET /api/firebase-config` — Returns `{ apiKey, authDomain, projectId }` for web clients.
- `GET /api/whoami` — Returns `{ authenticated: boolean, uid?, email? }` based on the `Authorization` header.

## Start and Track Runs

POST `/api/runs`
- Starts a background run and returns its ID.
- Body (unauthenticated):
  - `email_address` (string) — Gmail address to use
  - `app_password` (string) — Gmail app password
  - `email_limit` (number, optional, default 5)
- Body (authenticated with saved settings):
  - `email_limit` (number, optional)
- Response: `{ run_id: string, status: "running", started_at: ISO8601 }`

GET `/api/runs`
- Lists known runs in this server process.
- Response item fields: `{ id, email_address (masked), status, started_at, ended_at?, return_code?, log_lines }`

GET `/api/runs/{run_id}`
- Returns details for a run.

GET `/api/runs/{run_id}/logs?start=N`
- Incrementally fetches logs starting at line index `N`.
- Response: `{ start, next, status, lines: string[] }`
- Client pattern: begin with `start=0`, then call again with the returned `next` until `status` is `completed` or `failed`.

Notes
- The server redacts the app password and masks the email address in logs.
- Runs execute in a background thread. Environment variables are set per run; do not run multiple concurrent runs in the same process if you require strict isolation.

## Outputs and Summaries

GET `/api/output`
- Lists JSON files produced in the `output/` directory.
- Typical files:
  - `fetched_emails.json`
  - `categorization_report.json`
  - `organization_report.json`
  - `response_plan.json`, `response_report.json`
  - `cleanup_plan.json`, `cleanup_report.json`
  - `notification_report.json`

GET `/api/output/{name}`
- Returns parsed JSON content if possible, or `{ raw: string }`.

GET `/api/summary`
- Aggregates key items for UI:
  - `deleted` and `not_deleted` from `cleanup_plan.json`
  - `drafts` from `response_plan.json`

## User Settings (Authenticated)

These endpoints require a Firebase ID token in `Authorization: Bearer <id_token>`.

GET `/api/me/settings`
- Returns saved settings for the current user: `{ email_address?, auth_type, has_secret, updated_at?, signature_name?, signature? }`

PUT `/api/me/settings`
- Body fields (all optional):
  - `email_address` (string)
  - `app_password` (string) — stored in Secret Manager if available; falls back to KMS-encrypted Firestore
  - `signature_name` (string)
  - `signature` (string)
  - `auth_type` (string, default `app_password`)
- Returns updated settings (same shape as GET).

## Health

GET `/health` (no `/api` prefix)
- Returns `{ ok: true }` when the server is healthy.

## Error Handling

- `400` — validation errors (e.g., missing `email_address`/`app_password` when unauthenticated)
- `401` — missing/invalid token on authenticated endpoints
- `404` — unknown run or file
- `500` — server errors, or Firebase not configured for `/api/me/*`

## End-to-End Examples

Start a run (no auth):

```bash
curl -sS -X POST http://localhost:8080/api/runs \
  -H 'Content-Type: application/json' \
  -d '{
        "email_address": "you@gmail.com",
        "app_password": "abcd efgh ijkl mnop",
        "email_limit": 5
      }'
```

Tail logs until completion:

```bash
RUN_ID=... # from the POST response
NEXT=0
while true; do
  RES=$(curl -sS "http://localhost:8080/api/runs/$RUN_ID/logs?start=$NEXT")
  echo "$RES" | jq -r '.lines[]'
  STATUS=$(echo "$RES" | jq -r '.status')
  NEXT=$(echo "$RES" | jq -r '.next')
  [ "$STATUS" = "completed" -o "$STATUS" = "failed" ] && break
  sleep 1
done
```

List outputs and fetch a file:

```bash
curl -sS http://localhost:8080/api/output | jq
curl -sS http://localhost:8080/api/output/categorization_report.json | jq
```

Authenticated run using saved secret:

```bash
ID_TOKEN=... # Firebase ID token for the current user
curl -sS -X POST http://localhost:8080/api/runs \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{ "email_limit": 10 }'
```

Save settings (authenticated):

```bash
curl -sS -X PUT http://localhost:8080/api/me/settings \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "email_address": "you@gmail.com",
        "app_password": "abcd efgh ijkl mnop",
        "signature_name": "Your Name",
        "signature": "Best regards,\nYour Name"
      }'
```

## Environment Variables of Interest

- `PORT` — HTTP port (default `8080`)
- `WEB_DIR` — Optional override for static UI directory
- `FIREBASE_PROJECT_ID`, `FIREBASE_WEB_API_KEY`, `FIREBASE_AUTH_DOMAIN` — Web/Firebase config
- `GOOGLE_APPLICATION_CREDENTIALS` — Service account JSON for Firebase Admin / GCP clients
- `KMS_KEY` — Full KMS key name for encrypting secrets when Secret Manager is unavailable

LLM configuration is handled by the crew; see `README.md` for `MODEL`, provider API keys, and other app-level settings.

## Notes and Caveats

- Concurrency: The server sets process-level environment variables during a run. Avoid running multiple concurrent runs if you need strict isolation.
- Secrets: App passwords sent in `POST /api/runs` are never written to disk and are redacted from logs. Prefer storing credentials via `/api/me/settings` with Firebase auth in production.
- UI: A simple UI is available at `/ui/` and calls these same endpoints. See `docs/WEB_APP.md`.

