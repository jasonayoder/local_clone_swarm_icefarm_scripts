#!/usr/bin/env python3
"""Export all-run results from the EHW swarm into a single Excel workbook.

By default reads each run's live `workspace/` directory. Use `--source` to
point at a different subpath inside each clone (e.g. a specific
`prev_workspaces/<timestamp>/` snapshot) — the same relative path is applied
to every discovered run.

Discovery is dynamic: every top-level directory under the swarm root whose
name is a positive integer and which contains
`BitstreamEvolutionPico2ice/<source>/` is treated as a run. Works for 1, 8,
42+ runs — nothing hardcoded.

Sheets:
  - summary         one row per run (meta: final best, peak best, total gens)
  - config          one row per run (flattened builtconfig.ini)
  - best_per_gen    wide: generation x run{N}_{best,worst,avg,overall_best,diversity}
  - violin          wide: generation x run{N}_c{J} (per-circuit fitness)
  - heatmap         wide: generation x run{N}_s{J} (voltage V or pulse count)
  - all_data        long: (run, generation, best/worst/avg/div, violin agg, heat agg)
"""

import argparse
import configparser
import json
import sys
from pathlib import Path
from statistics import mean, median, pstdev

import pandas as pd


EXAMPLES = """
Examples:
  # Export live workspaces into ./swarm_results.xlsx (default)
  python3 export_results.py

  # Export a specific archived snapshot from every clone
  python3 export_results.py --source prev_workspaces/2026-04-18_12-34-56

  # Write to a custom output path
  python3 export_results.py --out /tmp/today.xlsx

  # Run against a different swarm root (e.g. a backup checkout)
  python3 export_results.py --root /path/to/other/swarm
"""


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--root", default=None, help="Swarm root (default: script's directory).")
    p.add_argument("--source", default="workspace",
                   help="Subpath under each clone's BitstreamEvolutionPico2ice/ to read. "
                        "Default: 'workspace'. Example: 'prev_workspaces/2026-04-18_12-34-56'.")
    p.add_argument("--out", default="swarm_results.xlsx",
                   help="Output .xlsx path (relative to --root unless absolute). Default: swarm_results.xlsx.")
    return p.parse_args()


def discover_runs(root, source):
    """Return a sorted list of (run_number, clone_dir, source_dir) tuples."""
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.isdigit():
            continue
        clone = child / "BitstreamEvolutionPico2ice"
        if not clone.is_dir():
            continue
        src = clone / source
        if src.is_dir():
            out.append((int(child.name), clone, src))
    out.sort(key=lambda t: t[0])
    return out


def load_serials(root):
    serials = {}
    path = root / "serials.txt"
    if not path.exists():
        return serials
    lines = [ln.strip() for ln in path.read_text().splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    for i, s in enumerate(lines, start=1):
        serials[i] = s
    return serials


def read_text_or_none(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        return None


def parse_best(text):
    xs, best, worst, avg, ovr, div = [], [], [], [], [], []
    if not text:
        return xs, best, worst, avg, ovr, div
    for line in text.split("\n"):
        if len(line) <= 1:
            continue
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            xs.append(int(parts[0]))
            best.append(float(parts[1]))
            worst.append(float(parts[2]))
            avg.append(float(parts[3]))
            ovr.append(float(parts[4]))
            div.append(float(parts[5]))
        except ValueError:
            continue
    return xs, best, worst, avg, ovr, div


def parse_violin_all(text):
    """{generation: [fitness, ...]}"""
    out = {}
    if not text:
        return out
    for line in text.split("\n"):
        if len(line) <= 1 or ":" not in line:
            continue
        g_str, vals_str = line.split(":", 1)
        try:
            g = int(g_str)
            vals = [float(v) for v in vals_str.split(",") if v]
        except ValueError:
            continue
        out[g] = vals
    return out


def parse_heatmap_all(text, is_pulse):
    """{generation: [value, ...]}  (voltage scaled to V, or raw pulse count)."""
    out = {}
    if not text:
        return out
    for line in text.split("\n"):
        if len(line) <= 1 or ":" not in line:
            continue
        g_str, d_str = line.split(":", 1)
        try:
            g = int(g_str)
        except ValueError:
            continue
        if is_pulse:
            try:
                arrays = json.loads("[" + d_str + "]")
            except (json.JSONDecodeError, ValueError):
                continue
            vals = [arr[0] for arr in arrays if arr]
        else:
            vals = []
            for pt in d_str.split(","):
                if not pt:
                    continue
                try:
                    vals.append(float(pt) * 3.3 / 715)
                except ValueError:
                    pass
        out[g] = vals
    return out


def detect_pulse_mode(src):
    p = src / "pulselivedata.log"
    return p.exists() and p.stat().st_size > 0


def flatten_config(path):
    if not path.exists():
        return {}
    cfg = configparser.ConfigParser()
    try:
        cfg.read(path)
    except configparser.Error:
        return {}
    out = {}
    for section in cfg.sections():
        for key, val in cfg.items(section):
            out[f"{section}.{key}"] = val
    return out


def agg_stats(values):
    if not values:
        return (None, None, None, None)
    if len(values) == 1:
        v = values[0]
        return (v, v, v, 0.0)
    return (min(values), median(values), max(values), pstdev(values))


def collect_run(run_n, clone, src):
    best_text = read_text_or_none(src / "bestlivedata.log")
    violin_text = read_text_or_none(src / "violinlivedata.log")
    is_pulse = detect_pulse_mode(src)
    heat_name = "pulselivedata.log" if is_pulse else "heatmaplivedata.log"
    heat_text = read_text_or_none(src / heat_name)
    # builtconfig lives in workspace/ typically; try source first then workspace fallback.
    cfg_candidates = [src / "builtconfig.ini", clone / "workspace" / "builtconfig.ini"]
    cfg_path = next((p for p in cfg_candidates if p.exists()), None)
    return {
        "run": run_n,
        "clone": clone,
        "source": src,
        "is_pulse": is_pulse,
        "best": parse_best(best_text),
        "violin": parse_violin_all(violin_text),
        "heatmap": parse_heatmap_all(heat_text, is_pulse),
        "config": flatten_config(cfg_path) if cfg_path else {},
        "config_path": cfg_path,
    }


def build_summary(runs_data, serials):
    rows = []
    for r in runs_data:
        xs, best, worst, avg, ovr, div = r["best"]
        row = {
            "run": r["run"],
            "serial": serials.get(r["run"], ""),
            "source_dir": str(r["source"]),
            "mode": "pulse" if r["is_pulse"] else "voltage",
            "total_generations": len(xs),
            "first_gen": xs[0] if xs else None,
            "last_gen": xs[-1] if xs else None,
            "final_best": best[-1] if best else None,
            "final_worst": worst[-1] if worst else None,
            "final_avg": avg[-1] if avg else None,
            "final_overall_best": ovr[-1] if ovr else None,
            "final_diversity": div[-1] if div else None,
            "peak_best": max(best) if best else None,
            "peak_overall_best": max(ovr) if ovr else None,
            "violin_gens_recorded": len(r["violin"]),
            "heatmap_gens_recorded": len(r["heatmap"]),
            "config_source": str(r["config_path"]) if r["config_path"] else "",
        }
        rows.append(row)
    return pd.DataFrame(rows)


def build_config(runs_data):
    all_keys = set()
    for r in runs_data:
        all_keys.update(r["config"].keys())
    cols = ["run"] + sorted(all_keys)
    rows = []
    for r in runs_data:
        row = {"run": r["run"]}
        for k in all_keys:
            row[k] = r["config"].get(k, "")
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


def build_best_wide(runs_data):
    """Generations as index; one column block per run."""
    frames = []
    for r in runs_data:
        xs, best, worst, avg, ovr, div = r["best"]
        if not xs:
            continue
        n = r["run"]
        df = pd.DataFrame({
            f"run{n}_best": best,
            f"run{n}_worst": worst,
            f"run{n}_avg": avg,
            f"run{n}_overall_best": ovr,
            f"run{n}_diversity": div,
        }, index=pd.Index(xs, name="generation"))
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["generation"])
    out = pd.concat(frames, axis=1).sort_index()
    return out.reset_index()


def build_samples_wide(runs_data, key, col_prefix):
    """Generic wide-format builder for violin/heatmap-style data.
    `key` is 'violin' or 'heatmap'; each run has {gen: [vals]}."""
    max_per_run = {}
    all_gens = set()
    for r in runs_data:
        d = r[key]
        all_gens.update(d.keys())
        max_per_run[r["run"]] = max((len(v) for v in d.values()), default=0)
    gens = sorted(all_gens)
    if not gens:
        return pd.DataFrame(columns=["generation"])
    columns = {"generation": gens}
    for r in runs_data:
        n = r["run"]
        d = r[key]
        w = max_per_run.get(n, 0)
        for j in range(w):
            col = f"run{n}_{col_prefix}{j + 1}"
            columns[col] = [
                (d.get(g)[j] if d.get(g) is not None and j < len(d.get(g)) else None)
                for g in gens
            ]
    return pd.DataFrame(columns)


def build_all_data_long(runs_data):
    rows = []
    for r in runs_data:
        xs, best, worst, avg, ovr, div = r["best"]
        violin = r["violin"]
        heat = r["heatmap"]
        heat_label = "pulse" if r["is_pulse"] else "voltage_v"
        for i, g in enumerate(xs):
            v_min, v_med, v_max, v_std = agg_stats(violin.get(g, []))
            h_min, h_med, h_max, h_std = agg_stats(heat.get(g, []))
            rows.append({
                "run": r["run"],
                "generation": g,
                "best": best[i],
                "worst": worst[i],
                "avg": avg[i],
                "overall_best": ovr[i],
                "diversity": div[i],
                "violin_min": v_min, "violin_median": v_med,
                "violin_max": v_max, "violin_std": v_std,
                f"{heat_label}_min": h_min, f"{heat_label}_median": h_med,
                f"{heat_label}_max": h_max, f"{heat_label}_std": h_std,
            })
    return pd.DataFrame(rows)


def main():
    args = parse_args()
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = root / out_path

    runs = discover_runs(root, args.source)
    if not runs:
        print(f"No runs found under {root} matching '<N>/BitstreamEvolutionPico2ice/{args.source}/'",
              file=sys.stderr)
        sys.exit(2)

    print(f"Discovered {len(runs)} run(s): {[n for n, _, _ in runs]}")
    print(f"Source subpath: {args.source}")

    serials = load_serials(root)
    runs_data = [collect_run(n, clone, src) for n, clone, src in runs]

    summary_df = build_summary(runs_data, serials)
    config_df = build_config(runs_data)
    best_df = build_best_wide(runs_data)
    violin_df = build_samples_wide(runs_data, "violin", "c")
    heatmap_df = build_samples_wide(runs_data, "heatmap", "s")
    all_df = build_all_data_long(runs_data)

    print(f"Writing {out_path}")
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        summary_df.to_excel(w, sheet_name="summary", index=False)
        config_df.to_excel(w, sheet_name="config", index=False)
        best_df.to_excel(w, sheet_name="best_per_gen", index=False)
        violin_df.to_excel(w, sheet_name="violin", index=False)
        heatmap_df.to_excel(w, sheet_name="heatmap", index=False)
        all_df.to_excel(w, sheet_name="all_data", index=False)

    print(f"Done. Sheets:")
    print(f"  summary      {summary_df.shape[0]} rows x {summary_df.shape[1]} cols")
    print(f"  config       {config_df.shape[0]} rows x {config_df.shape[1]} cols")
    print(f"  best_per_gen {best_df.shape[0]} rows x {best_df.shape[1]} cols")
    print(f"  violin       {violin_df.shape[0]} rows x {violin_df.shape[1]} cols")
    print(f"  heatmap      {heatmap_df.shape[0]} rows x {heatmap_df.shape[1]} cols")
    print(f"  all_data     {all_df.shape[0]} rows x {all_df.shape[1]} cols")


if __name__ == "__main__":
    main()
