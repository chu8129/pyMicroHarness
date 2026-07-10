#!/bin/bash
# Run harness container (image must already exist, use build_docker.sh to build)
# Usage: ./run.sh [args passed to python .]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

DOCKER_FLAGS="--rm"
if [ -t 0 ]; then
    DOCKER_FLAGS="$DOCKER_FLAGS -it"
fi

docker run $DOCKER_FLAGS \
    --env-file "$SCRIPT_DIR/.env" \
    -v "$SCRIPT_DIR:/app" \
    --privileged \
    harness "$@"
