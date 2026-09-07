#!/usr/bin/env bash
# Create a READ-ONLY IAM user + policy for Cost Explorer, and print access keys.
# Requires the AWS CLI authenticated as an admin (one-time setup).
#
# Usage: ./scripts/setup-aws-access.sh [user-name]
set -euo pipefail

USER_NAME="${1:-cloud-cost-reporter}"
POLICY_NAME="CloudCostReporterReadOnly"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"

echo ">> Account: ${ACCOUNT_ID}"

if ! aws iam get-policy --policy-arn "${POLICY_ARN}" >/dev/null 2>&1; then
  echo ">> Creating policy ${POLICY_NAME}"
  aws iam create-policy \
    --policy-name "${POLICY_NAME}" \
    --policy-document "file://${SCRIPT_DIR}/aws-cost-explorer-policy.json" >/dev/null
else
  echo ">> Policy ${POLICY_NAME} already exists"
fi

if ! aws iam get-user --user-name "${USER_NAME}" >/dev/null 2>&1; then
  echo ">> Creating user ${USER_NAME}"
  aws iam create-user --user-name "${USER_NAME}" >/dev/null
fi

aws iam attach-user-policy --user-name "${USER_NAME}" --policy-arn "${POLICY_ARN}"
echo ">> Attached ${POLICY_NAME} to ${USER_NAME}"

echo ">> Creating access key (store these in your secret / .env):"
aws iam create-access-key --user-name "${USER_NAME}" \
  --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text

cat <<'NOTE'

NOTE: Enable Cost Explorer once in the Billing console if you never have:
  Billing -> Cost Explorer -> Enable. Data appears within ~24h the first time.
NOTE
