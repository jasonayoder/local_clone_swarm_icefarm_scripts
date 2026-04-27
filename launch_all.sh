#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$ROOT/farmconfig.template.ini"
SERIALS_FILE="$ROOT/serials.txt"
SESSION="ehw"

[[ -f "$TEMPLATE" ]] || { echo "Missing $TEMPLATE"; exit 1; }
[[ -f "$SERIALS_FILE" ]] || { echo "Missing $SERIALS_FILE"; exit 1; }

mapfile -t SERIALS < <(grep -v '^\s*#' "$SERIALS_FILE" | grep -v '^\s*$')
if [[ "${#SERIALS[@]}" -ne 8 ]]; then
  echo "Need exactly 8 non-comment, non-blank lines in $SERIALS_FILE, found ${#SERIALS[@]}"
  exit 1
fi

for s in "${SERIALS[@]}"; do
  if [[ "$s" == REPLACE_ME_* ]]; then
    echo "Placeholder serial still present in $SERIALS_FILE: $s"
    echo "Replace all REPLACE_ME_SERIAL_N lines with real FPGA serials before launching."
    exit 1
  fi
done

if screen -ls 2>/dev/null | grep -qE "\.${SESSION}[[:space:]]"; then
  echo "screen session '$SESSION' already exists — run stop_all.sh first"
  exit 1
fi

for i in 1 2 3 4 5 6 7 8; do
  CLONE="$ROOT/$i/BitstreamEvolutionPico2ice"
  if [[ ! -d "$CLONE/docker" ]]; then
    echo "Missing expected clone directory: $CLONE/docker"
    exit 1
  fi
  SERIAL="${SERIALS[$((i-1))]}"
  sed "s|__DEVICE__|$SERIAL|" "$TEMPLATE" > "$CLONE/data/farmconfig.ini"

  # End-of-run output directories. Container user is 1000:1000 and the image's
  # /usr/local/app is root-owned, so anything written there at cleanup time
  # (WorkspaceFormatter writes to ./experiments, Logger writes to ./prev_workspaces)
  # must be bind-mounted to a host-owned dir or copytree() fails with
  # PermissionError / FileNotFoundError at Evolution.clean_up().
  mkdir -p "$CLONE/prev_workspaces" "$CLONE/experiments"
  cat > "$CLONE/docker/bitstream_local.override.yml" <<'EOF'
services:
  bitstreamevolution:
    volumes:
      - ../prev_workspaces:/usr/local/app/prev_workspaces
      - ../experiments:/usr/local/app/experiments
EOF
done

screen -dmS "$SESSION" -t "ehw1" bash -c \
  "cd '$ROOT/1/BitstreamEvolutionPico2ice/docker' && \
   COMPOSE_PROJECT_NAME=ehw1 CONFIG_PATH=data/farmconfig.ini \
   docker compose -f bitstream_local.yml -f bitstream_local.override.yml up --build --force-recreate; exec bash"

for i in 2 3 4 5 6 7 8; do
  screen -S "$SESSION" -X screen -t "ehw$i" bash -c \
    "cd '$ROOT/$i/BitstreamEvolutionPico2ice/docker' && \
     COMPOSE_PROJECT_NAME=ehw$i CONFIG_PATH=data/farmconfig.ini \
     docker compose -f bitstream_local.yml -f bitstream_local.override.yml up --build --force-recreate; exec bash"
done

echo "Launched. Attach with: screen -r $SESSION"
echo "Switch windows: Ctrl-a 0..7.  Detach: Ctrl-a d."
