#!/usr/bin/env bash
set -Eeuo pipefail

# Standalone Zhao CBS deployment; never stops or modifies the existing real-cbs container.
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HAMZA_LIB="${TSN_LIBRARY_HOST_PATH:-}"
IMAGE="zhao-cbs-standalone:local"
CONTAINER="zhao-cbs-standalone"
PORT=8005

if [[ ! -f "$PROJECT_ROOT/Dockerfile" ]]; then
  echo "Error: Dockerfile not found in $PROJECT_ROOT" >&2
  exit 1
fi
if [[ -z "$HAMZA_LIB" || ! -f "$HAMZA_LIB/usecase_generator.py" ]]; then
  echo "Error: Set TSN_LIBRARY_HOST_PATH to the local Hamza library folder containing usecase_generator.py" >&2
  exit 1
fi

# Never replace a container implicitly, including a previous standalone run.
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Error: Container '$CONTAINER' already exists. No containers were changed." >&2
  exit 1
fi

docker info >/dev/null
cd "$PROJECT_ROOT"
docker build -f Dockerfile -t "$IMAGE" .

docker run -d \
  --name "$CONTAINER" \
  -p "127.0.0.1:${PORT}:${PORT}" \
  --mount "type=bind,source=${HAMZA_LIB},target=/opt/hamza,readonly" \
  -e TSN_LIBRARY_PATH=/opt/hamza \
  "$IMAGE"

for attempt in {1..30}; do
  if curl --fail --silent --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null; then
    echo "Standalone Zhao CBS healthy: http://127.0.0.1:${PORT}/health"
    curl --fail --silent --show-error "http://127.0.0.1:${PORT}/capabilities" | python3 -m json.tool
    exit 0
  fi
  sleep 2
done

echo "Error: Standalone CBS did not become healthy. Logs:" >&2
docker logs "$CONTAINER" --tail 100 >&2
exit 1
