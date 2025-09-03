#!/usr/bin/env bash
set -euo pipefail

# One-command deploy to Cloud Run and patch OpenAPI servers URL
# Requires: gcloud CLI authenticated with a project

DEFAULT_REGION="us-central1"
DEFAULT_SERVICE="gmail-crew-ai"

usage() {
  cat <<EOF
Usage: scripts/deploy.sh [--secure] [--region REGION] [--service NAME]

Options:
  --secure          Deploy Cloud Run as authenticated-only (no public access). Defaults to public.
  --region REGION   Cloud Run region (default: ${DEFAULT_REGION}).
  --service NAME    Cloud Run service name (default: ${DEFAULT_SERVICE}).

Environment (read if set; otherwise interactive prompts):
  PROJECT_ID                GCP Project ID (falls back to gcloud config)
  GEMINI_API_KEY            Gemini API Key used by CrewAI LLM
  DEFAULT_EMAIL_ADDRESS     Optional: default Gmail address for headless runs
  DEFAULT_APP_PASSWORD      Optional: default Gmail app password for headless runs

This script will:
  - Enable required services
  - Deploy source to Cloud Run using Cloud Build
  - Set env vars
  - Patch docs/openapi.yaml servers.url with the Cloud Run URL
EOF
}

ALLOW_UNAUTH=true
REGION="${DEFAULT_REGION}"
SERVICE="${DEFAULT_SERVICE}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --secure)
      ALLOW_UNAUTH=false
      shift
      ;;
    --region)
      REGION="$2"; shift 2
      ;;
    --service)
      SERVICE="$2"; shift 2
      ;;
    -h|--help)
      usage; exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2; usage; exit 1
      ;;
  esac
done

require_bin() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 1; }
}

require_bin gcloud

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  read -rp "Enter GCP Project ID: " PROJECT_ID
fi

if ! gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  echo "Project not found or you lack access: $PROJECT_ID" >&2
  exit 1
fi

echo "Using project: $PROJECT_ID"
echo "Using region:  $REGION"
echo "Service name:  $SERVICE"

echo "Enabling required services..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  --project "$PROJECT_ID" >/dev/null

# Collect env vars
GEMINI_API_KEY="${GEMINI_API_KEY:-}"
if [[ -z "$GEMINI_API_KEY" ]]; then
  read -rp "Enter GEMINI_API_KEY (used by CrewAI LLM): " GEMINI_API_KEY
fi

DEFAULT_EMAIL_ADDRESS="${DEFAULT_EMAIL_ADDRESS:-}"
DEFAULT_APP_PASSWORD="${DEFAULT_APP_PASSWORD:-}"

if [[ -z "$DEFAULT_EMAIL_ADDRESS" || -z "$DEFAULT_APP_PASSWORD" ]]; then
  echo "You can configure default Gmail creds for headless Agent calls."
  read -rp "Default Gmail address (optional, press Enter to skip): " DEFAULT_EMAIL_ADDRESS || true
  if [[ -n "$DEFAULT_EMAIL_ADDRESS" ]]; then
    read -rsp "Default Gmail app password (input hidden): " DEFAULT_APP_PASSWORD || true
    echo
  fi
fi

ENV_VARS=("GEMINI_API_KEY=${GEMINI_API_KEY}")
if [[ -n "$DEFAULT_EMAIL_ADDRESS" ]]; then ENV_VARS+=("DEFAULT_EMAIL_ADDRESS=${DEFAULT_EMAIL_ADDRESS}"); fi
if [[ -n "$DEFAULT_APP_PASSWORD" ]]; then ENV_VARS+=("DEFAULT_APP_PASSWORD=${DEFAULT_APP_PASSWORD}"); fi

# Join env vars with commas
ENV_CSV=$(IFS=, ; echo "${ENV_VARS[*]}")

echo "Ensuring Artifact Registry repository exists..."
REPO="gmail-automation"
if ! gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPO" \
    --repository-format docker \
    --location "$REGION" \
    --description "Images for Gmail Crew AI" \
    --project "$PROJECT_ID"
fi

IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:$(date +%Y%m%d-%H%M%S)"

echo "Building image via Cloud Build: $IMAGE_URI"
gcloud builds submit --tag "$IMAGE_URI" --project "$PROJECT_ID"

echo "Deploying to Cloud Run..."
if $ALLOW_UNAUTH; then AUTH_FLAG="--allow-unauthenticated"; else AUTH_FLAG="--no-allow-unauthenticated"; fi
gcloud run deploy "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --image "$IMAGE_URI" \
  --port 8080 \
  --set-env-vars "$ENV_CSV" \
  $AUTH_FLAG

SERVICE_URL=$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" \
  --format='value(status.url)')

if [[ -z "$SERVICE_URL" ]]; then
  echo "Failed to resolve Cloud Run URL" >&2
  exit 1
fi

echo "Cloud Run URL: $SERVICE_URL"

echo "Patching docs/openapi.yaml with Cloud Run URL..."
bash ./scripts/update-openapi-url.sh "$SERVICE_URL" docs/openapi.yaml

if ! $ALLOW_UNAUTH; then
  echo "\nService is secured. You may need to grant Vertex AI Service Agent 'roles/run.invoker':"
  echo "  PROJECT_NUMBER=\$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')"
  echo "  VERTEX_SA=service-\$PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com"
  echo "  gcloud run services add-iam-policy-binding $SERVICE \\\n+    --project $PROJECT_ID --region $REGION \\\n+    --member=serviceAccount:\$VERTEX_SA \\\n+    --role=roles/run.invoker"
fi

echo "\nDone! Import docs/openapi.yaml into Agent Builder as an HTTP tool."
echo "Open UI: ${SERVICE_URL}/ui (if public)"
