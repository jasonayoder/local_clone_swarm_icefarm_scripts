#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="ehw"

# Graceful compose down for each project — compose sends SIGINT,
# bitstream.yml sets stop_grace_period: 30s so atexit handlers run
# and iCEFARM device reservations get released.
for i in 1 2 3 4 5 6 7 8; do
  COMPOSE_FILES=(-f bitstream_local.yml)
  if [[ -f "$ROOT/$i/BitstreamEvolutionPico2ice/docker/bitstream_local.override.yml" ]]; then
    COMPOSE_FILES+=(-f bitstream_local.override.yml)
  fi
  (cd "$ROOT/$i/BitstreamEvolutionPico2ice/docker" && \
   COMPOSE_PROJECT_NAME=ehw$i docker compose "${COMPOSE_FILES[@]}" down) || true
done

screen -S "$SESSION" -X quit 2>/dev/null || true
echo "All stacks stopped."
