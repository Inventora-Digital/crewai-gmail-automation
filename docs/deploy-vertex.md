Vertex AI Agent Engine deployment guide

Overview
- One-command deploy to Cloud Run from your CLI.
- Import `docs/openapi.yaml` as an HTTP tool into a Vertex AI Agent (Agent Engine / Agent Builder).
- Grant the Vertex AI service account permission to call your Cloud Run service, or allow unauthenticated access for quick testing.

One-command deploy
```bash
# Optionally export these so you won't be prompted
export PROJECT_ID=your-project-id
export GEMINI_API_KEY=your-gemini-api-key
# Optional defaults for headless Agent calls
export DEFAULT_EMAIL_ADDRESS=you@example.com
export DEFAULT_APP_PASSWORD=your-app-password

# Public (dev) deploy
make deploy REGION=us-central1 SERVICE=gmail-crew-ai

# Secure deploy (authenticated-only)
bash scripts/deploy.sh --secure --region us-central1 --service gmail-crew-ai
```

After deploy, the script prints the Cloud Run URL and patches `docs/openapi.yaml` with it.

Prerequisites
- gcloud CLI installed and authenticated.
- A Google Cloud project with billing enabled.
- Roles: Owner or equivalent to enable services and deploy Cloud Run.

1) Configure project and enable services
```bash
PROJECT_ID="your-project-id"
REGION="us-central1"    # choose your preferred region
REPO="gmail-automation"

gcloud config set project "$PROJECT_ID"
gcloud config set run/region "$REGION"

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com

# One-time: create an Artifact Registry Docker repo
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Images for Gmail Crew AI"
```

2) Build and push the image
```bash
IMAGE_URI="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/gmail-crew-ai:$(date +%Y%m%d-%H%M%S)"
gcloud builds submit --tag "$IMAGE_URI"
```

3) Deploy to Cloud Run
Choose either quick (public) or secure (service-to-service) access.

Quick (public; dev only)
```bash
gcloud run deploy gmail-crew-ai \
  --image "$IMAGE_URI" \
  --allow-unauthenticated \
  --port 8080 \
  --set-env-vars DEFAULT_EMAIL_ADDRESS=you@example.com \
  --set-env-vars DEFAULT_APP_PASSWORD=your-app-password \
  --set-env-vars GEMINI_API_KEY=your-gemini-api-key
```

Secure (recommended)
```bash
gcloud run deploy gmail-crew-ai \
  --image "$IMAGE_URI" \
  --no-allow-unauthenticated \
  --port 8080 \
  --set-env-vars DEFAULT_EMAIL_ADDRESS=you@example.com \
  --set-env-vars DEFAULT_APP_PASSWORD=your-app-password \
  --set-env-vars GEMINI_API_KEY=your-gemini-api-key

# Note the service URL
SERVICE_URL=$(gcloud run services describe gmail-crew-ai --format='value(status.url)')
echo "Cloud Run URL: $SERVICE_URL"
```

Optional environment variables
- DEFAULT_EMAIL_ADDRESS / DEFAULT_APP_PASSWORD: lets `/api/runs` work without passing credentials in the body (useful for headless Agent calls).
- SIGNATURE_NAME / EMAIL_SIGNATURE: personalize generated replies.
- FIREBASE_PROJECT_ID, FIREBASE_WEB_API_KEY, GOOGLE_APPLICATION_CREDENTIALS, KMS_KEY: enable optional auth + secret flows already built into the server.

4) Import the tool into Vertex AI Agent Engine
- Console → Vertex AI → Agent Builder → Agents → Create Agent.
  - Type: Action-based (Agent Engine)
  - Model: Gemini (e.g., Gemini 1.5/2.0 Flash)
  - System Instructions: describe your agent’s goal (e.g., “Manage my Gmail via the tool. When asked to process, call startRun, then stream logs and return a concise summary.”)
- Tools → Add Tool → HTTP API → Import OpenAPI.
  - Upload `docs/openapi.yaml`
  - Set the base URL to your Cloud Run URL (e.g., `https://...-a.run.app`).
  - Auth:
    - If Cloud Run is public: None.
    - If Cloud Run requires auth (recommended): select Google Service Account. The Vertex AI Service Agent identity will call the tool.

Grant Cloud Run invoker to Vertex AI (secure setup)
```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
VERTEX_SA="service-$PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com"
gcloud run services add-iam-policy-binding gmail-crew-ai \
  --member="serviceAccount:$VERTEX_SA" \
  --role="roles/run.invoker"
```

5) Test in the Agent Builder Playground
- Ask: “Process 5 unread emails and summarize results.”
- The agent should:
  - Call `startRun` (without credentials if you set defaults).
  - Poll `getRunLogs` until status ≠ running.
  - Call `getSummary` and present deleted/preserved/drafts.

6) Notes and best practices
- Secrets: do not commit `.env` or secrets. Use Cloud Run env vars and/or Secret Manager.
- LLM: the crew uses the Gemini API via `GEMINI_API_KEY`. You can keep this, or refactor to call Vertex AI directly; the Agent itself already runs on Vertex.
- Firebase: optional; server supports user auth + Secret Manager/KMS fallback if you later wire a UI.
- Observability: Review logs with `getRunLogs` or Cloud Run logs.

API quick reference
- POST `/api/runs` → `{ run_id, status, started_at }`
- GET `/api/runs/{run_id}` → run metadata
- GET `/api/runs/{run_id}/logs?start=N` → incremental logs
- GET `/api/summary` → deleted/not_deleted/drafts summary
