# Deploy to GCP (Cloud Run) and Firebase (Auth + Hosting)

This guide outlines how to add Google Sign‑In and persist user settings (email + secret) so users don’t re-enter credentials each time, and how to deploy the stack.

## Overview

- Frontend: Firebase Hosting serving `src/web` UI
- Auth: Firebase Authentication (Google provider) for sign-in
- Backend: FastAPI on Cloud Run verifying Firebase ID tokens
- Data: Firestore for user profiles/preferences + Secret storage (Secret Manager or KMS-encrypted Firestore field)

## Security Recommendation

Prefer Gmail API OAuth over storing an App Password. If you must use App Passwords, encrypt secrets with KMS or store per-user secrets in Secret Manager.

## Backend Changes

1. Verify Firebase ID tokens on each `/api/*` request:
   - Add `firebase-admin` and initialize with default credentials on Cloud Run.
   - Middleware verifies `Authorization: Bearer <ID_TOKEN>` and sets `request.state.user`.

2. Add endpoints:
   - `GET /api/me/settings` → returns profile (email), and a boolean `has_secret`.
   - `PUT /api/me/settings` → stores `email_address` and upserts secret (either refresh token for Gmail API or encrypted App Password).

3. Secret storage:
   - Option A (recommended): Secret Manager secret per user: `user-<uid>-gmail`.
   - Option B: Firestore field encrypted with KMS (AES‑GCM), store ciphertext + IV + keyId.

4. When starting a run (`POST /api/runs`):
   - If body omits `email_address`/secret, load from stored settings using `uid`.

## Firebase Setup

- Create a Firebase project (or use an existing one).
- Enable Authentication → Sign-in method → Google.
- Enable Firestore in Native mode.
- (Optional) Firebase Hosting to serve UI.

### Firestore Rules (minimal)

```
service cloud.firestore {
  match /databases/{database}/documents {
    match /users/{userId} {
      allow read, write: if request.auth != null && request.auth.uid == userId;
    }
  }
}
```

## Cloud Run Deployment

1. Create a Dockerfile for the FastAPI app (Uvicorn).
2. Deploy to Cloud Run with a service account that has:
   - `roles/datastore.user` (Firestore)
   - `roles/secretmanager.secretAccessor` and `secretVersionAdder` (if using Secret Manager)
   - (If KMS) `roles/cloudkms.cryptoKeyDecrypter` and `Encrypter`
3. Set env vars:
   - `FIREBASE_PROJECT_ID`
   - `PORT=8080`

## Firebase Hosting Rewrite (optional)

If you want `/api/*` proxied to Cloud Run and host UI on Firebase:

```
{
  "hosting": {
    "public": "src/web",
    "rewrites": [
      { "source": "/api/**", "run": { "serviceId": "gmail-crew-ai", "region": "us-central1" } },
      { "source": "**", "destination": "/index.html" }
    ]
  }
}
```

## Frontend Integration

- Add Firebase web SDK in UI, show Google Sign‑In button.
- On sign‑in, fetch ID token and call backend with `Authorization: Bearer <token>`.
- Add a Settings panel to save email + secret once; server stores securely.
- When starting runs, omit secret if already stored.

## Migration to Gmail API (Recommended)

Replace IMAP tools with Gmail API equivalents and store OAuth refresh tokens instead of App Passwords. This improves security (revocable, scoped access) and makes deployment smoother.

