#!/usr/bin/env python3
"""Render aggregate plots as PNG files directly from swarm_results.xlsx workbooks.

Each input xlsx (produced by export_results.py) generates a similarly-named
set of PNGs alongside it (or in --out-dir if given):
  <basename>_scalars.png    — best/avg overlays + current-gen scatter + violin
  <basename>_heatmaps.png   — 2×4 small multiples (one per run)

Pass any number of xlsx files; each is processed independently. Sheets
expected in each workbook: summary, best_per_gen, violin, heatmap (extras
ignored). Waveform images are not produced because waveform samples are not
in the export.
"""

import argparse
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import gridspec, style
from matplotlib.lines import Line2D

HEATMAP_BINS = 40
SCALARS_SUFFIX = "_scalars.png"
HEATMAPS_SUFFIX = "_heatmaps.png"
RUN_COL_RE = re.compile(r"^run(\d+)_(.+)$")

EXAMPLES = """
Examples:
  # One xlsx
  python3 plot_from_xlsx.py results_desired_freq_50000.xlsx

  # All sweep outputs at once (uses shell glob)
  python3 plot_from_xlsx.py results_desired_freq_*.xlsx

  # Prettified fitness curves (faint raw + bold rolling-mean trend)
  python3 plot_from_xlsx.py --pretty results_desired_freq_*.xlsx

  # Redirect images to a separate directory
  python3 plot_from_xlsx.py --out-dir plots/ results_desired_freq_*.xlsx
"""


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("xlsx", nargs="+", help="One or more xlsx files from export_results.py.")
    p.add_argument("--out-dir", default=None,
                   help="Directory to write PNGs (default: alongside each xlsx).")
    p.add_argument("--pretty", action="store_true",
                   help="Faint raw lines + bold rolling-mean trend on fitness overlays "
                        "(same option as aggregate_liveplot.py --pretty).")
    p.add_argument("--dpi", type=int, default=120, help="PNG DPI. Default: 120.")
    return p.parse_args()


def short_serial(s):
    return (s[:8] + "…") if s and len(s) > 8 else (s or "?")


def rolling_mean(xs, ys, window):
    """Trailing arithmetic rolling mean. Returns (xs_out, ys_out)."""
    n = len(ys)
    if n < window or window < 2:
        return list(xs), list(ys)
    out_x, out_y = [], []
    acc = sum(ys[:window])
    out_x.append(xs[window - 1])
    out_y.append(acc / window)
    for i in range(window, n):
        acc += ys[i] - ys[i - window]
        out_x.append(xs[i])
        out_y.append(acc / window)
    return out_x, out_y


def discover_runs_from_columns(df):
    runs = set()
    for col in df.columns:
        m = RUN_COL_RE.match(str(col))
        if m:
            runs.add(int(m.group(1)))
    return sorted(runs)


def load_workbook(xlsx_path):
    sheets = {}
    for name in ["summary", "best_per_gen", "violin", "heatmap"]:
        try:
            sheets[name] = pd.read_excel(xlsx_path, sheet_name=name)
        except (ValueError, KeyError):
            sheets[name] = None
    return sheets


def get_serials_map(summary_df):
    if summary_df is None or "run" not in summary_df.columns:
        return {}
    out = {}
    for _, row in summary_df.iterrows():
        try:
            out[int(row["run"])] = str(row.get("serial", "") or "")
        except (ValueError, TypeError):
            continue
    return out


def get_mode(summary_df):
    if summary_df is None or "mode" not in summary_df.columns or len(summary_df) == 0:
        return "voltage"
    modes = [m for m in summary_df["mode"].dropna().unique().tolist() if m]
    return modes[0] if modes else "voltage"


def runs_present_in_best(best_df):
    runs = []
    if best_df is None:
        return runs
    for n in discover_runs_from_columns(best_df):
        col = f"run{n}_best"
        if col in best_df.columns and best_df[col].dropna().shape[0] > 0:
            runs.append(n)
    return runs


def make_label(n, serials):
    s = serials.get(n, "")
    return f"{n} ({short_serial(s)})"


def plot_scalars(stem, sheets, out_path, pretty, dpi):
    style.use("dark_background")
    fig = plt.figure(figsize=(14, 8))
    gs = gridspec.GridSpec(
        2, 2, figure=fig,
        hspace=0.5, wspace=0.25,
        top=0.84, bottom=0.08, left=0.06, right=0.98,
    )
    ax_best = fig.add_subplot(gs[0, 0])
    ax_avg = fig.add_subplot(gs[0, 1])
    ax_scatter = fig.add_subplot(gs[1, 0])
    ax_violin = fig.add_subplot(gs[1, 1])

    best_df = sheets.get("best_per_gen")
    violin_df = sheets.get("violin")
    summary_df = sheets.get("summary")
    serials = get_serials_map(summary_df)

    runs = runs_present_in_best(best_df)
    cmap = plt.get_cmap("tab10")
    colors = {n: cmap((i % 10) / 10.0) for i, n in enumerate(runs)}

    # Best & avg overlays
    if best_df is not None and "generation" in best_df.columns and runs:
        for n in runs:
            sub = best_df[["generation", f"run{n}_best", f"run{n}_worst", f"run{n}_avg"]].dropna()
            if sub.empty:
                continue
            gens = sub["generation"].tolist()
            best = sub[f"run{n}_best"].tolist()
            worst = sub[f"run{n}_worst"].tolist()
            avg = sub[f"run{n}_avg"].tolist()
            color = colors[n]
            if pretty:
                window = max(3, min(30, len(best) // 20))
                ax_best.plot(gens, best, color=color, alpha=0.18, linewidth=0.8)
                ax_avg.plot(gens, avg, color=color, alpha=0.18, linewidth=0.8)
                sx, sy = rolling_mean(gens, best, window)
                ax_best.plot(sx, sy, color=color, alpha=0.95, linewidth=2.0)
                sx, sy = rolling_mean(gens, avg, window)
                ax_avg.plot(sx, sy, color=color, alpha=0.95, linewidth=2.0)
            else:
                ax_best.plot(gens, best, color=color, linewidth=1.2)
                ax_avg.plot(gens, avg, color=color, linewidth=1.2)
                ax_avg.fill_between(gens, worst, best, color=color, alpha=0.08)
        title_suffix = " (smoothed)" if pretty else ""
        avg_extra = " (smoothed)" if pretty else " (worst–best shaded)"
        ax_best.set(title=f"Best fitness per generation{title_suffix}",
                    xlabel="Generation", ylabel="Fitness (log)")
        ax_avg.set(title=f"Avg fitness per generation{avg_extra}",
                   xlabel="Generation", ylabel="Fitness (log)")
        ax_best.set_yscale("log")
        ax_avg.set_yscale("log")

    # Current-gen scatter + violin from latest violin row
    if violin_df is not None and len(violin_df) > 0:
        last_row = violin_df.iloc[-1]
        positions, data, vcolors = [], [], []
        pos = 0
        for n in runs:
            sample_cols = [c for c in violin_df.columns
                           if isinstance(c, str) and c.startswith(f"run{n}_c")]
            if not sample_cols:
                continue
            vals = [float(v) for v in last_row[sample_cols].tolist() if pd.notna(v)]
            if not vals:
                continue
            ax_scatter.scatter(range(1, len(vals) + 1), vals,
                               color=colors[n], alpha=0.7, s=14)
            pos += 1
            positions.append(pos)
            data.append(vals)
            vcolors.append(colors[n])
        ax_scatter.set(title="Current-gen fitness scatter",
                       xlabel="Circuit idx", ylabel="Fitness (log)")
        ax_scatter.set_yscale("log")
        if data:
            parts = ax_violin.violinplot(data, positions=positions,
                                         widths=0.8, showmeans=True)
            for pc, c in zip(parts["bodies"], vcolors):
                pc.set_facecolor(c)
                pc.set_edgecolor(c)
                pc.set_alpha(0.55)
        ax_violin.set(title="Current-gen fitness (violin)",
                      xlabel="Run slot", ylabel="Fitness (log)")
        ax_violin.set_yscale("log")

    # Legend
    if runs:
        handles = [Line2D([0], [0], color=colors[n], lw=2,
                          label=make_label(n, serials)) for n in runs]
        fig.legend(handles=handles, loc="upper center",
                   bbox_to_anchor=(0.5, 0.97),
                   ncol=min(len(runs), 4), fontsize=8, framealpha=0.2)

    title = f"{stem} — scalars" + (" (pretty)" if pretty else "")
    fig.suptitle(title, fontsize=12, y=0.995)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def plot_heatmaps(stem, sheets, out_path, dpi):
    style.use("dark_background")
    heat_df = sheets.get("heatmap")
    summary_df = sheets.get("summary")
    serials = get_serials_map(summary_df)
    mode = get_mode(summary_df)

    runs = []
    if heat_df is not None:
        runs = sorted({n for n in discover_runs_from_columns(heat_df)})
    n_runs = max(len(runs), 1)
    cols = min(4, n_runs)
    rows = max(1, (n_runs + cols - 1) // cols)

    fig = plt.figure(figsize=(14, 3.5 * rows))
    gs = gridspec.GridSpec(
        rows, cols, figure=fig,
        hspace=0.55, wspace=0.3,
        top=0.90, bottom=0.08, left=0.05, right=0.98,
    )

    if heat_df is None or not runs or "generation" not in heat_df.columns:
        ax = fig.add_subplot(gs[0, 0])
        ax.text(0.5, 0.5, "no heatmap data in workbook",
                ha="center", va="center", color="#888888")
        ax.set_xticks([]); ax.set_yticks([])
    else:
        gens_col = heat_df["generation"]
        for i, n in enumerate(runs):
            r, c = i // cols, i % cols
            ax = fig.add_subplot(gs[r, c])
            sample_cols = [col for col in heat_df.columns
                           if isinstance(col, str) and col.startswith(f"run{n}_s")]
            if not sample_cols:
                ax.text(0.5, 0.5, f"run {make_label(n, serials)}\nno data",
                        ha="center", va="center", color="#888888", fontsize=9)
                ax.set_xticks([]); ax.set_yticks([])
                continue
            # Flatten (gen, sample) → parallel lists, dropping NaN
            gens, vals = [], []
            sub = heat_df[["generation"] + sample_cols]
            for _, row in sub.iterrows():
                g = row["generation"]
                if pd.isna(g):
                    continue
                g = int(g)
                for col in sample_cols:
                    v = row[col]
                    if pd.notna(v):
                        gens.append(g)
                        vals.append(float(v))
            if not gens:
                ax.text(0.5, 0.5, f"run {make_label(n, serials)}\nno samples",
                        ha="center", va="center", color="#888888", fontsize=9)
                ax.set_xticks([]); ax.set_yticks([])
                continue
            bins_x = max(2, min(HEATMAP_BINS, max(gens) - min(gens) + 1))
            ax.hist2d(gens, vals, bins=[bins_x, HEATMAP_BINS], cmap="viridis")
            unit = "pulses" if mode == "pulse" else "V"
            ax.set_title(f"run {make_label(n, serials)} — {unit}", fontsize=8)
            ax.tick_params(labelsize=7)

    fig.suptitle(f"{stem} — heatmaps", fontsize=12, y=0.985)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def main():
    args = parse_args()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for xlsx_str in args.xlsx:
        path = Path(xlsx_str)
        if not path.is_file():
            print(f"skipping {xlsx_str}: not found", file=sys.stderr)
            failures += 1
            continue
        try:
            sheets = load_workbook(path)
        except Exception as e:
            print(f"failed to read {xlsx_str}: {e}", file=sys.stderr)
            failures += 1
            continue
        target_dir = out_dir or path.parent
        stem = path.stem
        scalars_out = target_dir / f"{stem}{SCALARS_SUFFIX}"
        heatmaps_out = target_dir / f"{stem}{HEATMAPS_SUFFIX}"
        plot_scalars(stem, sheets, scalars_out, args.pretty, args.dpi)
        plot_heatmaps(stem, sheets, heatmaps_out, args.dpi)
        print(f"wrote {scalars_out}")
        print(f"wrote {heatmaps_out}")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
