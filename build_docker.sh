#!/bin/bash
# Run harness container with current directory mounted
# Usage: ./run_docker.sh [extra args passed to python .]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE_NAME="harness"

# Build if image doesn't exist
if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "Building image '$IMAGE_NAME'..."
    docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"
fi

# Use -it only when stdin is a terminal
DOCKER_FLAGS="--rm"
if [ -t 0 ]; then
    DOCKER_FLAGS="$DOCKER_FLAGS -it"
fi

docker run $DOCKER_FLAGS \
    --env-file "$SCRIPT_DIR/.env" \
    -v "$SCRIPT_DIR:/app" \
    --privileged \
    "$IMAGE_NAME" "$@"
