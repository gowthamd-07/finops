#!/usr/bin/env bash
# Create a READ-ONLY GCP service account for Cloud Billing, grant it
# "Billing Account Viewer" on each Example billing account, provision the BigQuery
# billing-export dataset + read access, and emit a JSON key.
# Requires gcloud authenticated as someone who can manage the host project's
# service accounts/IAM AND the billing accounts' IAM (one-time setup).
#
# NOTE: "Billing Account Viewer" (roles/billing.viewer) returns billing-account
# metadata only. Per-service COST BREAKDOWNS come from Cloud Billing export to
# BigQuery. This script prepares the dataset + IAM the export writes into and the
# app reads from; the export itself is turned on per billing account in the Cloud
# Console (Billing -> Billing export -> BigQuery export), pointed at BQ_DATASET.
#
# Usage: ./scripts/setup-gcp-access.sh [service-account-name] [host-project-id]
set -euo pipefail

SA_NAME="${1:-cloud-cost-reporter}"
PROJECT_ID="${2:-billing-prod}"                 # Example - PROD
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY_FILE="${KEY_FILE:-${SCRIPT_DIR}/../gcp-cost-reporter-key.json}"

# BigQuery billing-export location (dataset the Console export writes into).
BQ_PROJECT="${BQ_PROJECT:-${PROJECT_ID}}"
BQ_DATASET="${BQ_DATASET:-billing_export}"
BQ_LOCATION="${BQ_LOCATION:-US}"

# Billing accounts to grant read access on (ID from `gcloud billing accounts list`).
BILLING_ACCOUNTS=(
  "000000-000000-000000"   # Example Billing Account
  "01AFAE-DF8A68-940788"   # Example-Photon Billing
)
ROLE="roles/billing.viewer"

echo ">> Host project: ${PROJECT_ID}"
echo ">> Service account: ${SA_EMAIL}"

if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo ">> Creating service account ${SA_NAME}"
  gcloud iam service-accounts create "${SA_NAME}" \
    --project="${PROJECT_ID}" \
    --display-name="Cloud Cost Reporter (read-only billing)" >/dev/null
  # IAM is eventually consistent: wait for the new SA to propagate before it can
  # be referenced in a billing-account / project IAM binding.
  echo ">> Waiting for service account to propagate..."
  for i in $(seq 1 12); do
    gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1 && break
    sleep 5
  done
  sleep 10
else
  echo ">> Service account ${SA_NAME} already exists"
fi

for BA in "${BILLING_ACCOUNTS[@]}"; do
  echo ">> Granting ${ROLE} on billing account ${BA}"
  gcloud billing accounts add-iam-policy-binding "${BA}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" >/dev/null && echo "   granted" || echo "   already granted / skipped"
done

echo ">> Ensuring BigQuery export dataset ${BQ_PROJECT}:${BQ_DATASET} (${BQ_LOCATION})"
if bq --project_id="${BQ_PROJECT}" show --dataset "${BQ_PROJECT}:${BQ_DATASET}" >/dev/null 2>&1; then
  echo "   dataset already exists"
else
  bq --project_id="${BQ_PROJECT}" mk --dataset --location="${BQ_LOCATION}" \
    --description="GCP Cloud Billing export (read-only by cloud-cost-reporter)" \
    "${BQ_PROJECT}:${BQ_DATASET}" >/dev/null && echo "   created"
fi

echo ">> Granting roles/bigquery.jobUser on ${BQ_PROJECT} (run queries)"
gcloud projects add-iam-policy-binding "${BQ_PROJECT}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.jobUser" \
  --condition=None >/dev/null && echo "   granted"

echo ">> Granting READER (dataViewer) scoped to ${BQ_PROJECT}:${BQ_DATASET}"
# Dataset-level IAM via add-iam-policy-binding needs org allowlisting; use the
# portable dataset-ACL method instead.
ACL_TMP="$(mktemp)"
bq --project_id="${BQ_PROJECT}" show --format=prettyjson "${BQ_PROJECT}:${BQ_DATASET}" > "${ACL_TMP}"
python3 - "${ACL_TMP}" "${SA_EMAIL}" <<'PY'
import json, sys
path, sa = sys.argv[1], sys.argv[2]
with open(path) as f:
    ds = json.load(f)
access = ds.get("access", [])
if not any(a.get("userByEmail") == sa and a.get("role") == "READER" for a in access):
    access.append({"role": "READER", "userByEmail": sa})
    ds["access"] = access
    with open(path, "w") as f:
        json.dump(ds, f)
PY
bq update --source "${ACL_TMP}" "${BQ_PROJECT}:${BQ_DATASET}" >/dev/null && echo "   granted"
rm -f "${ACL_TMP}"

echo ">> Creating JSON key (store this in your secret / Key Vault, then delete the file):"
gcloud iam service-accounts keys create "${KEY_FILE}" \
  --iam-account="${SA_EMAIL}"
echo ">> Wrote ${KEY_FILE}"

cat <<NOTE

NEXT STEPS
  1. This key is a credential. Put its contents in Key Vault / your Secret as
     GOOGLE_APPLICATION_CREDENTIALS_JSON (or set GOOGLE_APPLICATION_CREDENTIALS
     to the file path), then delete the local gcp-cost-reporter-key.json.
  2. Turn ON the billing export in the Cloud Console (no CLI equivalent):
       Billing -> (each account) -> Billing export -> BigQuery export -> Edit
       Project: ${BQ_PROJECT}   Dataset: ${BQ_DATASET}
     Enable "Detailed usage cost" for per-service/SKU breakdowns. Do this for
     BOTH billing accounts. Data begins landing within ~24h as tables named
     gcp_billing_export_v1_<ACCOUNT_ID> in ${BQ_DATASET}.
  3. Historical months (before export) load from Console cost-table CSVs in
     gcp-billing/ via the gcp collector (config gcp.source=csv|auto).
NOTE
