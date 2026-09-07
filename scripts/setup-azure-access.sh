#!/usr/bin/env bash
# Grant "Cost Management Reader" (READ-ONLY) on each Example subscription.
#
# Two modes:
#   PRINCIPAL_ID set  -> assign role to that object id (managed identity / SP)
#   PRINCIPAL_ID unset-> assign to your own logged-in user (for LOCAL testing)
#
# Requires: az login (as someone who can manage role assignments).
set -euo pipefail

SUBSCRIPTIONS=(
  "00000000-0000-0000-0000-000000000011"   # Production
  "00000000-0000-0000-0000-000000000012"   # NonProduction
  "00000000-0000-0000-0000-000000000013"   # Connectivity
  "00000000-0000-0000-0000-000000000014"   # Management
)
ROLE="Cost Management Reader"

PRINCIPAL_ID="${PRINCIPAL_ID:-$(az ad signed-in-user show --query id -o tsv)}"
echo ">> Assigning '${ROLE}' to principal ${PRINCIPAL_ID}"

for SUB in "${SUBSCRIPTIONS[@]}"; do
  echo ">> ${SUB}"
  az role assignment create \
    --assignee-object-id "${PRINCIPAL_ID}" \
    --assignee-principal-type "$( [ -n "${PRINCIPAL_ID_TYPE:-}" ] && echo "${PRINCIPAL_ID_TYPE}" || echo User )" \
    --role "${ROLE}" \
    --scope "/subscriptions/${SUB}" \
    --only-show-errors >/dev/null && echo "   assigned" || echo "   already assigned / skipped"
done

cat <<'NOTE'

For the IN-CLUSTER app, create a user-assigned managed identity and run this
with PRINCIPAL_ID=<identity objectId> PRINCIPAL_ID_TYPE=ServicePrincipal, then
federate it to the K8s ServiceAccount (Workload Identity) and set the
azure.workload.identity/client-id annotation in k8s/serviceaccount.yaml.
NOTE
