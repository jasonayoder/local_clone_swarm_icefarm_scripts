#!/usr/bin/env python3
"""Parameter sweep runner for the EHW swarm.

For each value in --values, the script:
  1. renders a modified farmconfig with <section>.<key> = <value>
     (while preserving the rest of the template and per-clone __DEVICE__
     substitution) and writes it to every discovered clone's
     data/farmconfig.ini
  2. launches docker compose up on every clone in parallel, headless
     (no screen), tailing each run's output to a log file
  3. waits for every container to exit, with a wall-clock timeout per value
  4. calls docker compose down to release iCEFARM reservations
  5. invokes export_results.py to snapshot that value's workbook
  6. advances to the next value; on timeout or failure, logs and continues

Discovery is dynamic: any <N>/BitstreamEvolutionPico2ice/docker/ under the
swarm root counts as a run. Not hardcoded to 8.
"""

import argparse
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

OVERRIDE_YML = """\
services:
  bitstreamevolution:
    volumes:
      - ../prev_workspaces:/usr/local/app/prev_workspaces
      - ../experiments:/usr/local/app/experiments
"""

EXAMPLES = """
Examples:
  # Sweep target pulse count over 5 orders of magnitude, 4-hour cap per value
  python3 sweep.py \\
      --param "FITNESS PARAMETERS.desired_freq" \\
      --values 10000000,1000000,100000,10000,1000 \\
      --timeout-min 240

  # Custom output naming + skip values whose xlsx already exists
  python3 sweep.py --param "GA PARAMETERS.mutation_probability" \\
      --values 0.001,0.002,0.005 \\
      --out-template "sweep_mut/results_mut_{value}.xlsx" \\
      --skip-existing

  # Sanity check (writes per-clone configs, doesn't launch containers)
  python3 sweep.py --param "STOPPING CONDITION PARAMETERS.generations" \\
      --values 10,20 --dry-run
"""


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--param", required=True,
                   help="Parameter to sweep, as 'SECTION.key' (section names may contain spaces). "
                        "Example: 'FITNESS PARAMETERS.desired_freq'.")
    p.add_argument("--values", required=True,
                   help="Comma-separated list of values to sweep, e.g. "
                        "'10000000,1000000,100000,10000,1000'.")
    p.add_argument("--root", default=None,
                   help="Swarm root (default: directory of this script).")
    p.add_argument("--template", default=None,
                   help="Path to farmconfig.template.ini (default: <root>/farmconfig.template.ini).")
    p.add_argument("--out-template", default="results_{param_short}_{value}.xlsx",
                   help="Output .xlsx path per value. Placeholders: {value}, {param_short}, "
                        "{section}, {key}. Relative paths resolve against --root.")
    p.add_argument("--logs-dir", default=None,
                   help="Per-run container log directory (default: <root>/sweep_logs/<timestamp>).")
    p.add_argument("--timeout-min", type=int, default=240,
                   help="Wall-clock max per sweep value, minutes. Default: 240 (4 hours).")
    p.add_argument("--poll-seconds", type=int, default=10,
                   help="How often to poll container status. Default: 10.")
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip values whose output xlsx already exists.")
    p.add_argument("--dry-run", action="store_true",
                   help="Write per-clone configs and print commands, but don't launch containers.")
    return p.parse_args()


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_serials(root):
    path = root / "serials.txt"
    if not path.exists():
        return {}
    serials = {}
    lines = [ln.strip() for ln in path.read_text().splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    for i, s in enumerate(lines, start=1):
        if s.startswith("REPLACE_ME_"):
            raise SystemExit(f"serials.txt line {i} still has placeholder: {s}")
        serials[i] = s
    return serials


def discover_runs(root):
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.isdigit():
            continue
        clone = child / "BitstreamEvolutionPico2ice"
        if (clone / "docker").is_dir():
            out.append((int(child.name), clone))
    out.sort(key=lambda t: t[0])
    return out


def modify_param(template_text, section, key, value):
    """Replace `key = ...` inside the matching [section] block. Raises if not found."""
    section_re = re.compile(r"^\s*\[(.+?)\]\s*$")
    key_re = re.compile(rf"^(\s*){re.escape(key)}(\s*=\s*)(.*)$")
    out_lines = []
    current = None
    replaced = False
    for line in template_text.splitlines(keepends=False):
        m = section_re.match(line)
        if m:
            current = m.group(1).strip()
            out_lines.append(line)
            continue
        if current == section and not replaced:
            km = key_re.match(line)
            if km:
                indent, eq, _old = km.group(1), km.group(2), km.group(3)
                line = f"{indent}{key}{eq}{value}"
                replaced = True
        out_lines.append(line)
    if not replaced:
        raise ValueError(f"Could not find [{section}] {key} in template")
    suffix = "\n" if template_text.endswith("\n") else ""
    return "\n".join(out_lines) + suffix


def ensure_clone_override(clone):
    """Mirror the setup launch_all.sh does: host output dirs + override.yml."""
    (clone / "prev_workspaces").mkdir(exist_ok=True)
    (clone / "experiments").mkdir(exist_ok=True)
    override = clone / "docker" / "bitstream_local.override.yml"
    if not override.exists() or override.read_text() != OVERRIDE_YML:
        override.write_text(OVERRIDE_YML)


def write_configs(runs, serials, swept_template):
    """Write per-clone data/farmconfig.ini + ensure override scaffolding."""
    for run_n, clone in runs:
        serial = serials.get(run_n)
        if not serial:
            raise SystemExit(f"No serial in serials.txt for run {run_n}")
        rendered = swept_template.replace("__DEVICE__", serial)
        cfg_path = clone / "data" / "farmconfig.ini"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(rendered)
        ensure_clone_override(clone)


def compose_up(clone, project, log_path):
    cmd = ["docker", "compose",
           "-f", "bitstream_local.yml",
           "-f", "bitstream_local.override.yml",
           "up", "--build", "--force-recreate"]
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = project
    env["CONFIG_PATH"] = "data/farmconfig.ini"
    fp = open(log_path, "w")
    # start_new_session so our SIGINT handler can reap the whole group if needed.
    proc = subprocess.Popen(
        cmd, cwd=str(clone / "docker"), env=env,
        stdout=fp, stderr=subprocess.STDOUT, start_new_session=True,
    )
    proc._log_fp = fp  # keep handle alive; we close in finally
    return proc


def compose_down(clone, project):
    cmd = ["docker", "compose",
           "-f", "bitstream_local.yml",
           "-f", "bitstream_local.override.yml",
           "down"]
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = project
    try:
        subprocess.run(cmd, cwd=str(clone / "docker"), env=env,
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except subprocess.TimeoutExpired:
        log(f"  [{project}] compose down timed out")


def wait_with_timeout(procs_by_run, timeout_seconds, poll_seconds):
    """Poll until all procs exit or timeout. Returns dict of run_n -> exit_code (None if killed)."""
    deadline = time.time() + timeout_seconds
    exit_codes = {}
    while time.time() < deadline:
        done = all(p.poll() is not None for _, p in procs_by_run)
        if done:
            break
        time.sleep(poll_seconds)
    # Record exit codes for those that finished naturally
    for run_n, p in procs_by_run:
        exit_codes[run_n] = p.poll()
    # Timeout path: terminate any still running
    still = [(n, p) for n, p in procs_by_run if p.poll() is None]
    if still:
        log(f"Timeout reached; sending SIGTERM to {len(still)} still-running container group(s)")
        for _, p in still:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        # Grace period (compose has 30s stop_grace_period)
        grace_until = time.time() + 45
        while time.time() < grace_until and any(p.poll() is None for _, p in still):
            time.sleep(2)
        for run_n, p in still:
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            exit_codes[run_n] = p.poll()
    return exit_codes


def shutdown_all(procs_by_run, runs_by_n):
    """Best-effort: terminate processes and run compose down for each project."""
    for _, p in procs_by_run:
        if p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
    time.sleep(2)
    for run_n, _ in procs_by_run:
        compose_down(runs_by_n[run_n], f"ehw{run_n}")


def run_export(root, out_path):
    cmd = [sys.executable, str(SCRIPT_DIR / "export_results.py"),
           "--root", str(root), "--out", str(out_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


def resolve_out_path(root, template_str, section, key, value):
    param_short = re.sub(r"[^A-Za-z0-9_]+", "_", key).lower().strip("_")
    name = template_str.format(value=value, param=f"{section}.{key}",
                               param_short=param_short, section=section, key=key)
    out = Path(name)
    if not out.is_absolute():
        out = root / out
    return out


def main():
    args = parse_args()
    root = Path(args.root).resolve() if args.root else SCRIPT_DIR
    template_path = Path(args.template) if args.template else root / "farmconfig.template.ini"
    if not template_path.is_file():
        raise SystemExit(f"Template not found: {template_path}")
    template_text = template_path.read_text()

    if "." not in args.param:
        raise SystemExit("--param must be 'SECTION.key' (found no dot)")
    section, key = args.param.rsplit(".", 1)
    # Validate the key exists up front so we fail fast before spinning containers.
    modify_param(template_text, section, key, "PLACEHOLDER")

    values = [v.strip() for v in args.values.split(",") if v.strip()]
    if not values:
        raise SystemExit("--values is empty")

    runs = discover_runs(root)
    if not runs:
        raise SystemExit(f"No runs discovered under {root}/*/BitstreamEvolutionPico2ice/docker/")
    runs_by_n = {n: clone for n, clone in runs}
    serials = load_serials(root)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logs_root = Path(args.logs_dir) if args.logs_dir else root / f"sweep_logs/{ts}"
    logs_root.mkdir(parents=True, exist_ok=True)

    log(f"Swarm root:       {root}")
    log(f"Discovered runs:  {[n for n, _ in runs]}")
    log(f"Param sweep:      [{section}] {key} over {values}")
    log(f"Timeout per val:  {args.timeout_min} min")
    log(f"Logs directory:   {logs_root}")

    # SIGINT: terminate current value's containers then exit.
    active = {"procs": [], "runs_by_n": runs_by_n}

    def sigint_handler(signum, frame):
        log("SIGINT received; tearing down active containers...")
        if active["procs"]:
            shutdown_all(active["procs"], active["runs_by_n"])
        sys.exit(130)

    signal.signal(signal.SIGINT, sigint_handler)

    results_summary = []

    for idx, value in enumerate(values, start=1):
        out_path = resolve_out_path(root, args.out_template, section, key, value)
        value_log_dir = logs_root / f"value_{value}"
        value_log_dir.mkdir(parents=True, exist_ok=True)

        log(f"=== [{idx}/{len(values)}] value = {value} -> {out_path} ===")

        if args.skip_existing and out_path.exists():
            log(f"  output already exists, skipping (per --skip-existing)")
            results_summary.append((value, "skipped", str(out_path)))
            continue

        # Render + write per-clone configs
        swept = modify_param(template_text, section, key, value)
        (value_log_dir / "farmconfig_swept.ini").write_text(swept)
        write_configs(runs, serials, swept)
        log(f"  wrote {len(runs)} per-clone configs")

        if args.dry_run:
            for run_n, clone in runs:
                cmd = ("cd " + shlex.quote(str(clone / "docker")) +
                       " && COMPOSE_PROJECT_NAME=ehw" + str(run_n) +
                       " CONFIG_PATH=data/farmconfig.ini docker compose"
                       " -f bitstream_local.yml -f bitstream_local.override.yml"
                       " up --build --force-recreate")
                log(f"    would run: {cmd}")
            results_summary.append((value, "dry-run", ""))
            continue

        # Safety: bring any stale containers down first
        for run_n, clone in runs:
            compose_down(clone, f"ehw{run_n}")

        # Launch
        procs_by_run = []
        for run_n, clone in runs:
            project = f"ehw{run_n}"
            log_path = value_log_dir / f"run_{run_n}.log"
            proc = compose_up(clone, project, log_path)
            procs_by_run.append((run_n, proc))
            log(f"  launched {project} (pid {proc.pid}) -> {log_path}")
        active["procs"] = procs_by_run

        # Wait
        timeout_s = args.timeout_min * 60
        exit_codes = wait_with_timeout(procs_by_run, timeout_s, args.poll_seconds)

        # Close log file handles
        for _, p in procs_by_run:
            try:
                p._log_fp.close()
            except Exception:
                pass

        # Always compose down for a clean slate
        for run_n, clone in runs:
            compose_down(clone, f"ehw{run_n}")
        active["procs"] = []

        failed = [n for n, rc in exit_codes.items() if rc not in (0,)]
        if failed:
            log(f"  runs with non-zero/killed exit: {sorted(failed)} "
                f"(codes: { {n: exit_codes[n] for n in sorted(failed)} })")
        else:
            log(f"  all {len(exit_codes)} runs exited cleanly")

        # Export regardless — partial data is still useful
        log(f"  exporting -> {out_path}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        res = run_export(root, out_path)
        if res.returncode == 0:
            status = "ok" if not failed else "partial"
            log(f"  export OK")
        else:
            status = "export_failed"
            log(f"  export FAILED (rc={res.returncode}):\n{res.stderr.strip()}")

        results_summary.append((value, status, str(out_path)))

    log("=== Sweep complete ===")
    for value, status, path in results_summary:
        log(f"  value={value:<16}  status={status:<14}  out={path}")


if __name__ == "__main__":
    main()
