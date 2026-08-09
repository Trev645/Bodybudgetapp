#!/usr/bin/env bash
# Deploy the AWA Utilisation Platform to Azure.
#
#   Azure Container Apps  <- API + web console (cloud-built from Dockerfile)
#   PostgreSQL Flexible Server <- storage layer
#
# Prereqs: azure-cli logged in (az login), run from the repository root.
# Idempotent-ish: safe to re-run; existing resources are reused.
set -euo pipefail

LOCATION="${LOCATION:-uksouth}"
RG="${RG:-awa-utilisation-rg}"
APP="${APP:-awa-utilisation}"
ACA_ENV="${ACA_ENV:-awa-env}"
PG_NAME="${PG_NAME:-awa-pg-$(az account show --query id -o tsv | cut -c1-8)}"
PG_ADMIN="${PG_ADMIN:-awaadmin}"
PG_PASSWORD="${PG_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=')}"
DB_NAME="awa"

echo "==> Resource group $RG in $LOCATION"
az group create -n "$RG" -l "$LOCATION" -o none

echo "==> PostgreSQL flexible server $PG_NAME (Burstable B1ms)"
if ! az postgres flexible-server show -g "$RG" -n "$PG_NAME" -o none 2>/dev/null; then
  az postgres flexible-server create -g "$RG" -n "$PG_NAME" -l "$LOCATION" \
    --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 \
    --admin-user "$PG_ADMIN" --admin-password "$PG_PASSWORD" \
    --public-access 0.0.0.0 --yes -o none
  echo "    admin password: $PG_PASSWORD  (store this securely NOW)"
fi
az postgres flexible-server db create -g "$RG" -s "$PG_NAME" -d "$DB_NAME" -o none

PG_HOST="$PG_NAME.postgres.database.azure.com"
DB_URL="postgresql+psycopg://$PG_ADMIN:$PG_PASSWORD@$PG_HOST/$DB_NAME?sslmode=require"

echo "==> Container Apps environment + app (cloud build from Dockerfile)"
az containerapp env create -g "$RG" -n "$ACA_ENV" -l "$LOCATION" -o none 2>/dev/null || true
az containerapp up --name "$APP" --resource-group "$RG" \
  --environment "$ACA_ENV" --location "$LOCATION" \
  --source . --ingress external --target-port 8000 -o none

echo "==> Wiring database secret"
az containerapp secret set -g "$RG" -n "$APP" \
  --secrets awa-database-url="$DB_URL" -o none
az containerapp update -g "$RG" -n "$APP" \
  --set-env-vars AWA_DATABASE_URL=secretref:awa-database-url \
  --min-replicas 1 --max-replicas 3 -o none

URL="https://$(az containerapp show -g "$RG" -n "$APP" \
  --query properties.configuration.ingress.fqdn -o tsv)"

echo "==> Waiting for /health"
for i in $(seq 1 30); do
  curl -fsS "$URL/health" >/dev/null 2>&1 && break || sleep 5
done
curl -fsS "$URL/health" && echo

echo "==> Bootstrapping admin API key (shown once — store it securely)"
az containerapp exec -g "$RG" -n "$APP" \
  --command "python -m awa.cli create-admin-key" || \
  echo "    (exec unavailable in this terminal — run:
     az containerapp exec -g $RG -n $APP --command 'python -m awa.cli create-admin-key')"

cat <<EOF

Deployed.
  Console:   $URL/
  API docs:  $URL/docs
  Database:  $PG_HOST/$DB_NAME

Next: use the admin key to register your first client
  curl -X POST $URL/admin/clients -H "X-API-Key: <admin-key>" \\
    -H "Content-Type: application/json" \\
    -d '{"name":"Client Ltd","sector":"finance","region":"london"}'
EOF
