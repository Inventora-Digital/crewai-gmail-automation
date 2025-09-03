.PHONY: deploy openapi-url grant-invoker

PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
REGION ?= us-central1
SERVICE ?= gmail-crew-ai

deploy:
	@bash scripts/deploy.sh --region $(REGION) --service $(SERVICE)

openapi-url:
	@if [ -z "$(URL)" ]; then echo "Usage: make openapi-url URL=https://..."; exit 1; fi
	bash scripts/update-openapi-url.sh "$(URL)" docs/openapi.yaml

grant-invoker:
	@if [ -z "$(PROJECT_ID)" ]; then echo "PROJECT_ID is not set"; exit 1; fi
	PROJECT_NUMBER=$$(gcloud projects describe $(PROJECT_ID) --format='value(projectNumber)'); \
	VERTEX_SA=service-$$PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com; \
	gcloud run services add-iam-policy-binding $(SERVICE) \
	  --member="serviceAccount:$$VERTEX_SA" \
	  --role="roles/run.invoker" \
	  --region $(REGION) \
	  --project $(PROJECT_ID)

