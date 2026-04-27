# EHW Bitstream Swarm

This directory hosts 8 parallel instances of `BitstreamEvolutionPico2ice`, each driving a single FPGA through a shared iCEFARM server at `http://localhost:8080`. Directories `1/` through `8/` are independent clones of the client library; each runs its own evolutionary experiment container.

## Layout

```
./
├── 1/ .. 8/                       # one clone per FPGA (each contains BitstreamEvolutionPico2ice/)
├── farmconfig.template.ini        # canonical experiment config (shared across all 8)
├── serials.txt                    # 8 FPGA serials, one per line; line N → directory N
├── launch_all.sh                  # renders per-clone configs, brings up 8 compose stacks in a screen session
└── stop_all.sh                    # graceful teardown of all 8 stacks + screen session
```

## Usage

```bash
# Edit shared experiment parameters (generations, GA knobs, fitness, etc.)
$EDITOR farmconfig.template.ini

# Update FPGA serials if they change (line N = directory N)
$EDITOR serials.txt

# Launch everything
./launch_all.sh
screen -r ehw           # attach; Ctrl-a 0..7 switches windows, Ctrl-a d detaches

# Stop everything
./stop_all.sh
```

`launch_all.sh` regenerates each clone's `data/farmconfig.ini` from `farmconfig.template.ini` on every launch — the `devices = ["__DEVICE__"]` placeholder is substituted with that directory's serial from `serials.txt`. Edits made directly to a clone's `data/farmconfig.ini` are overwritten on the next launch.

## Why each piece matters

- **`COMPOSE_PROJECT_NAME=ehw{N}`** is set per window. Without it, every clone's compose file would default to project name `docker` (the compose file's parent directory name), producing identical container names and collisions.
- **`bitstream_local.yml`** (not `bitstream.yml`) is used. The pip-published `icefarm` package is older than the client code expects and is missing `EvaluationFailed`; the bundled `iCEFARM/` source inside each clone has it. `bitstream_local.yml` builds from that bundled source.
- **`--build --force-recreate`** flags on `docker compose up` ensure image and container are rebuilt even if a previous pip-based image is cached.
- **Per-clone serial pinning** (vs. `devices = 1` / random) removes a reservation race and makes debugging deterministic: directory N always talks to FPGA N.

## Operational gotchas

- **Workspace ownership.** `bitstream_local.yml` runs as `user: "1000:1000"`. If a `workspace/` directory ends up owned by `root` (e.g., from an earlier run of `bitstream.yml` which didn't set `user:`), the container will hit `PermissionError` on `workspace/builtconfig.ini`. Fix: `sudo chown -R 1000:1000 <clone>/BitstreamEvolutionPico2ice/workspace`.
- **First launch is slow.** Eight parallel image builds — several minutes of CPU churn. Subsequent launches reuse cached layers.
- **`network_mode: host`** is set on the compose file. All 8 containers share the host network namespace. This is fine because `evolve.py` is a client (no listener); it just needs to reach `localhost:8080`.
- **`stop_grace_period: 30s`** on the compose file gives Python's `atexit` hooks time to release iCEFARM device reservations when `docker compose down` sends SIGINT. Killing the screen session directly (instead of using `stop_all.sh`) can leave zombie reservations.
- **Log/output location.** Each clone writes to its own `BitstreamEvolutionPico2ice/workspace/` (naturally isolated — the compose mounts a relative path that resolves differently per clone). Look there for `log`, `best.asc`, `alllivedata.log`, `generations/`, etc.
