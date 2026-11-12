#!/usr/bin/env bash
# Mirror the Langfuse v4 self-host images (linux/amd64) into the Snowflake image repository.
# Uses REFUND_LAB_CONNECTION and REFUND_LAB_ACCOUNT (ORG-ACCOUNT), like the Python code.
set -euo pipefail

CONNECTION="${REFUND_LAB_CONNECTION:-default}"
ACCOUNT="${REFUND_LAB_ACCOUNT:?Set REFUND_LAB_ACCOUNT to your ORG-ACCOUNT identifier}"
REGISTRY="$(echo "$ACCOUNT" | tr '[:upper:]_' '[:lower:]-').registry.snowflakecomputing.com"
REPO="$REGISTRY/refund_agent_lab/langfuse/images"

# source image -> repository name:tag (versions mirror langfuse/docker-compose.yml)
IMAGES=(
  "docker.langfuse.com/langfuse/langfuse:4|langfuse-web:4"
  "docker.langfuse.com/langfuse/langfuse-worker:4|langfuse-worker:4"
  "docker.io/clickhouse/clickhouse-server:26.3.62.3|clickhouse:26.3.62.3"
  "docker.io/postgres:17|postgres:17"
  "docker.io/redis:7|redis:7"
  "cgr.dev/chainguard/minio:latest|minio:latest"
)

snow spcs image-registry login --connection "$CONNECTION"

for entry in "${IMAGES[@]}"; do
  source_image="${entry%%|*}"
  target="$REPO/${entry##*|}"
  echo "== $source_image -> $target"
  docker pull --platform linux/amd64 "$source_image"
  docker tag "$source_image" "$target"
  docker push "$target"
done
echo "MIRROR_DONE"
