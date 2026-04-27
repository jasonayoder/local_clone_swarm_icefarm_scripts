#!/usr/bin/env python3
"""Aggregate live plotter for the EHW 8-FPGA swarm.

Tails the 8 workspaces under ./N/BitstreamEvolutionPico2ice/workspace/ and
renders a single matplotlib window combining:
  - overlaid scalar curves (best / avg / diversity) per generation
  - overlaid current-gen fitness scatter and violin plots
  - 2x4 small multiples for voltage/pulse heatmaps
  - 2x4 small multiples for current best waveforms

Usage:
  python3 aggregate_liveplot.py
  python3 aggregate_liveplot.py --runs 1,3,5 --frame-interval 5000
  python3 aggregate_liveplot.py --no-heatmap --no-waveform
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import matplotlib.animation as animation
import matplotlib.pyplot as plt
from matplotlib import gridspec, style
from matplotlib.lines import Line2D

ADC_MAX = 4095
HEATMAP_BINS = 40
NUM_RUNS = 8


EXAMPLES = """
Examples:
  # Default: all 8 runs, raw rendering, 10 s refresh
  python3 aggregate_liveplot.py

  # Subset of runs, faster refresh
  python3 aggregate_liveplot.py --runs 1,3,5 --frame-interval 5000

  # Lightweight: scalars only (skip heatmap + waveform grids)
  python3 aggregate_liveplot.py --no-heatmap --no-waveform

  # Prettified fitness overlays (faint raw + bold rolling mean trend)
  python3 aggregate_liveplot.py --pretty

  # Tolerate long generations (e.g. heavy FPGA evaluation) before flagging stalled
  python3 aggregate_liveplot.py --stall-seconds 300

  # Snapshot completed runs to PNGs (renders once, no live updates, then exits)
  python3 aggregate_liveplot.py --save plots/run_50000

  # Render images from an archived snapshot, with pretty overlays
  python3 aggregate_liveplot.py --pretty \\
      --source prev_workspaces/04-24-2026\\ -\\ 13:07:24 \\
      --save plots/snap_50000 --save-prefix swarm
"""


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--root", default=None, help="Swarm root (default: directory of this script).")
    p.add_argument("--runs", default="1-8", help="Runs to plot: '1-8' or '1,3,5'.")
    p.add_argument("--source", default="workspace",
                   help="Subpath under each clone's BitstreamEvolutionPico2ice/ to read. "
                        "Default: 'workspace'. Use e.g. 'prev_workspaces/<timestamp>' to read "
                        "an archived snapshot of a completed run.")
    p.add_argument("--frame-interval", type=int, default=10000, help="Refresh ms (live mode only).")
    p.add_argument("--stall-seconds", type=int, default=120,
                   help="Seconds with no generation advance before a run is marked 'stalled'.")
    p.add_argument("--no-heatmap", action="store_true", help="Skip heatmap grid.")
    p.add_argument("--no-waveform", action="store_true", help="Skip waveform grid.")
    p.add_argument("--no-violin", action="store_true", help="Skip violin overlay.")
    p.add_argument("--pretty", action="store_true",
                   help="Prettified fitness overlays: faint raw lines + bold rolling-mean trend. "
                        "Raw mode (default) is the authoritative view; pretty is a readability aid.")
    p.add_argument("--save", default=None, metavar="DIR",
                   help="Render once and save PNGs to DIR, then exit (no live windows). "
                        "Useful for snapshotting completed runs.")
    p.add_argument("--save-prefix", default="aggregate",
                   help="Filename prefix for --save output (default: 'aggregate' -> "
                        "aggregate_scalars.png, aggregate_heatmaps.png, aggregate_waveforms.png).")
    p.add_argument("--save-dpi", type=int, default=120,
                   help="DPI for --save PNGs. Default: 120.")
    return p.parse_args()


def parse_run_spec(spec):
    out = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, hi = chunk.split("-")
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(chunk))
    return sorted(set(n for n in out if 1 <= n <= NUM_RUNS))


def load_serials(root):
    serials = {}
    path = root / "serials.txt"
    if not path.exists():
        return serials
    lines = [ln.strip() for ln in path.read_text().splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    for i, s in enumerate(lines[:NUM_RUNS], start=1):
        serials[i] = s
    return serials


def workspace_path(root, n, source="workspace"):
    return root / str(n) / "BitstreamEvolutionPico2ice" / source


def read_text_or_none(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except (FileNotFoundError, PermissionError):
        return None


def run_state_basic(root, n, source="workspace"):
    """Classify a run based on what's on disk alone: 'no_data', 'waiting', or 'present'.
    ('stalled' vs 'live' requires epoch-advance tracking across ticks — see Aggregator.)"""
    wp = workspace_path(root, n, source)
    if not wp.is_dir():
        return "no_data"
    best = wp / "bestlivedata.log"
    if not best.exists() or best.stat().st_size == 0:
        return "waiting"
    return "present"


def latest_epoch(best_parse):
    """Last epoch from a parse_best() tuple, or None if no data."""
    xs = best_parse[0]
    return xs[-1] if xs else None


def parse_best(text):
    """bestlivedata.log -> dict of parallel lists."""
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


def parse_all(text):
    """alllivedata.log -> (xs, ys) for current-gen scatter."""
    xs, ys = [], []
    if not text:
        return xs, ys
    for line in text.split("\n"):
        if len(line) <= 1:
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            idx = int(parts[0])
            for y in parts[1].split(";"):
                xs.append(idx)
                ys.append(float(y))
        except ValueError:
            continue
    return xs, ys


def parse_violin_latest(text):
    """violinlivedata.log -> (gen, [fitness, ...]) of most recent generation."""
    if not text:
        return None, []
    lines = [ln for ln in text.split("\n") if len(ln) > 1]
    if not lines:
        return None, []
    last = lines[-1]
    if ":" not in last:
        return None, []
    gen_str, vals_str = last.split(":", 1)
    try:
        gen = int(gen_str)
        vals = [float(v) for v in vals_str.split(",") if v]
    except ValueError:
        return None, []
    return gen, vals


def parse_heatmap_points(text, is_pulse):
    """heatmap or pulse log -> parallel lists (gens, values). Matches PlotEvolutionLive.anim_heatmap."""
    gens, vals = [], []
    if not text:
        return gens, vals
    for line in text.split("\n"):
        if len(line) <= 1 or ":" not in line:
            continue
        g_str, d_str = line.split(":", 1)
        try:
            gen = int(g_str)
        except ValueError:
            continue
        if is_pulse:
            try:
                arrays = json.loads("[" + d_str + "]")
            except (json.JSONDecodeError, ValueError):
                continue
            for arr in arrays:
                if not arr:
                    continue
                gens.append(gen)
                vals.append(arr[0])
        else:
            for pt in d_str.split(","):
                if not pt:
                    continue
                try:
                    gens.append(gen)
                    vals.append(float(pt) * 3.3 / 715)
                except ValueError:
                    continue
    return gens, vals


def parse_waveform(text):
    """waveformlivedata.log -> (xs, volts in V)."""
    xs, ys = [], []
    if not text:
        return xs, ys
    for line in text.split("\n"):
        if len(line) <= 1 or "," not in line:
            continue
        x_str, y_str = line.split(",", 1)
        try:
            xs.append(int(x_str))
            ys.append(float(y_str) * 3.3 / ADC_MAX)
        except ValueError:
            continue
    return xs, ys


def detect_pulse_mode(root, runs, source="workspace"):
    """Look for any run with non-empty pulselivedata.log → pulse mode."""
    for n in runs:
        p = workspace_path(root, n, source) / "pulselivedata.log"
        if p.exists() and p.stat().st_size > 0:
            return True
    return False


def short_serial(s):
    return (s[:8] + "…") if s and len(s) > 8 else (s or "?")


def rolling_mean(xs, ys, window):
    """Trailing arithmetic rolling mean. Returns (xs_out, ys_out) aligned to the
    window's last sample. No numpy dependency."""
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


def make_label(n, serials, state):
    serial = short_serial(serials.get(n))
    base = f"{n} ({serial})"
    if state in ("no_data", "waiting"):
        return f"{base} [waiting]"
    if state == "stalled":
        return f"{base} [stalled]"
    return base


class Aggregator:
    def __init__(self, args):
        script_dir = Path(__file__).resolve().parent
        self.root = Path(args.root).resolve() if args.root else script_dir
        self.runs = parse_run_spec(args.runs)
        self.source = args.source
        self.frame_interval = args.frame_interval
        self.stall_seconds = args.stall_seconds
        self.show_heatmap = not args.no_heatmap
        self.show_waveform = not args.no_waveform
        self.show_violin = not args.no_violin
        self.pretty = args.pretty
        self.save_dir = Path(args.save) if args.save else None
        self.save_prefix = args.save_prefix
        self.save_dpi = args.save_dpi
        self.serials = load_serials(self.root)
        self.is_pulse = detect_pulse_mode(self.root, self.runs, self.source)
        # per-run epoch tracker: n -> (last_seen_epoch, wall_time_when_first_seen)
        self._epoch_tracker = {}

        cmap = plt.get_cmap("tab10")
        self.colors = {n: cmap((i % 10) / 10.0) for i, n in enumerate(self.runs)}

        style.use("dark_background")
        self._build_scalars_figure()
        if self.show_heatmap:
            self._build_heatmaps_figure()
        if self.show_waveform:
            self._build_waveforms_figure()

    def _build_scalars_figure(self):
        self.fig_scalars = plt.figure(figsize=(14, 8))
        title = "EHW swarm — scalars" + (" [pretty]" if self.pretty else "")
        self.fig_scalars.canvas.manager.set_window_title(title)
        gs = gridspec.GridSpec(
            2, 2, figure=self.fig_scalars,
            hspace=0.5, wspace=0.25,
            top=0.84, bottom=0.08, left=0.06, right=0.98,
        )
        self.ax_best = self.fig_scalars.add_subplot(gs[0, 0])
        self.ax_avg = self.fig_scalars.add_subplot(gs[0, 1])
        self.ax_scatter = self.fig_scalars.add_subplot(gs[1, 0])
        self.ax_violin = self.fig_scalars.add_subplot(gs[1, 1]) if self.show_violin else None

    def _build_heatmaps_figure(self):
        self.fig_heatmaps = plt.figure(figsize=(14, 7))
        self.fig_heatmaps.canvas.manager.set_window_title("EHW swarm — heatmaps")
        gs = gridspec.GridSpec(
            2, 4, figure=self.fig_heatmaps,
            hspace=0.55, wspace=0.3,
            top=0.90, bottom=0.08, left=0.05, right=0.98,
        )
        self.ax_heatmaps = {}
        for i, n in enumerate(self.runs):
            r, c = i // 4, i % 4
            if r < 2 and c < 4:
                self.ax_heatmaps[n] = self.fig_heatmaps.add_subplot(gs[r, c])

    def _build_waveforms_figure(self):
        self.fig_waveforms = plt.figure(figsize=(14, 7))
        self.fig_waveforms.canvas.manager.set_window_title("EHW swarm — waveforms")
        gs = gridspec.GridSpec(
            2, 4, figure=self.fig_waveforms,
            hspace=0.55, wspace=0.3,
            top=0.90, bottom=0.08, left=0.05, right=0.98,
        )
        self.ax_waveforms = {}
        for i, n in enumerate(self.runs):
            r, c = i // 4, i % 4
            if r < 2 and c < 4:
                self.ax_waveforms[n] = self.fig_waveforms.add_subplot(gs[r, c])

    def _draw_legend(self, fig, states):
        handles = []
        for n in self.runs:
            label = make_label(n, self.serials, states.get(n, "no_data"))
            color = self.colors[n]
            alpha = 1.0 if states.get(n) == "live" else 0.35
            handles.append(Line2D([0], [0], color=color, lw=2, label=label, alpha=alpha))
        fig.legend(
            handles=handles,
            loc="upper center",
            bbox_to_anchor=(0.5, 0.97),
            ncol=min(len(self.runs), 4),
            fontsize=8,
            framealpha=0.2,
        )

    def _placeholder(self, ax, msg):
        ax.clear()
        ax.text(0.5, 0.5, msg, ha="center", va="center", color="#888888", fontsize=9)
        ax.set_xticks([])
        ax.set_yticks([])

    def _update_scalar_overlays(self, per_run):
        self.ax_best.clear()
        self.ax_avg.clear()
        if self.pretty:
            self._draw_scalar_overlays_pretty(per_run)
        else:
            self._draw_scalar_overlays_raw(per_run)
        self.ax_best.set_yscale("log")
        self.ax_avg.set_yscale("log")

    def _draw_scalar_overlays_raw(self, per_run):
        for n in self.runs:
            state = per_run[n]["state"]
            if state in ("no_data", "waiting"):
                continue
            xs, best, worst, avg, ovr, div = per_run[n]["best"]
            if not xs:
                continue
            alpha = 0.45 if state == "stalled" else 1.0
            color = self.colors[n]
            self.ax_best.plot(xs, best, color=color, alpha=alpha, linewidth=1.2)
            self.ax_avg.plot(xs, avg, color=color, alpha=alpha, linewidth=1.2)
            self.ax_avg.fill_between(xs, worst, best, color=color, alpha=0.08 * alpha)
        self.ax_best.set(title="Best fitness per generation", xlabel="Generation", ylabel="Fitness (log)")
        self.ax_avg.set(title="Avg fitness per generation (worst–best shaded)", xlabel="Generation", ylabel="Fitness (log)")

    def _draw_scalar_overlays_pretty(self, per_run):
        for n in self.runs:
            state = per_run[n]["state"]
            if state in ("no_data", "waiting"):
                continue
            xs, best, worst, avg, ovr, div = per_run[n]["best"]
            if not xs:
                continue
            dim = 0.45 if state == "stalled" else 1.0
            color = self.colors[n]
            window = max(3, min(30, len(xs) // 20))
            self.ax_best.plot(xs, best, color=color, alpha=0.18 * dim, linewidth=0.8)
            self.ax_avg.plot(xs, avg, color=color, alpha=0.18 * dim, linewidth=0.8)
            sx, sy = rolling_mean(xs, best, window)
            self.ax_best.plot(sx, sy, color=color, alpha=0.95 * dim, linewidth=2.0)
            sx, sy = rolling_mean(xs, avg, window)
            self.ax_avg.plot(sx, sy, color=color, alpha=0.95 * dim, linewidth=2.0)
        self.ax_best.set(title="Best fitness per generation (smoothed)", xlabel="Generation", ylabel="Fitness (log)")
        self.ax_avg.set(title="Avg fitness per generation (smoothed)", xlabel="Generation", ylabel="Fitness (log)")

    def _update_current_gen(self, per_run):
        self.ax_scatter.clear()
        for n in self.runs:
            state = per_run[n]["state"]
            if state in ("no_data", "waiting"):
                continue
            xs, ys = per_run[n]["all"]
            if not xs:
                continue
            alpha = 0.3 if state == "stalled" else 0.7
            self.ax_scatter.scatter(xs, ys, color=self.colors[n], alpha=alpha, s=14)
        self.ax_scatter.set(title="Current-gen fitness scatter", xlabel="Circuit idx", ylabel="Fitness (log)")
        self.ax_scatter.set_yscale("log")

        if self.ax_violin is not None:
            self.ax_violin.clear()
            data = []
            positions = []
            colors = []
            pos = 0
            for n in self.runs:
                state = per_run[n]["state"]
                if state in ("no_data", "waiting"):
                    continue
                _, vals = per_run[n]["violin"]
                if not vals:
                    continue
                pos += 1
                data.append(vals)
                positions.append(pos)
                colors.append(self.colors[n])
            if data:
                parts = self.ax_violin.violinplot(data, positions=positions, widths=0.8, showmeans=True)
                for pc, c in zip(parts["bodies"], colors):
                    pc.set_facecolor(c)
                    pc.set_edgecolor(c)
                    pc.set_alpha(0.55)
            self.ax_violin.set(title="Current-gen fitness (violin)", xlabel="Run slot", ylabel="Fitness (log)")
            self.ax_violin.set_yscale("log")

    def _update_heatmaps(self, per_run):
        if not self.show_heatmap:
            return
        for n, ax in self.ax_heatmaps.items():
            state = per_run[n]["state"]
            label = make_label(n, self.serials, state)
            if state in ("no_data", "waiting"):
                self._placeholder(ax, f"run {label}")
                continue
            gens, vals = per_run[n]["heatmap"]
            if not gens:
                self._placeholder(ax, f"run {label}\nno heatmap yet")
                continue
            ax.clear()
            bins_x = max(2, min(HEATMAP_BINS, max(gens) - min(gens) + 1))
            ax.hist2d(gens, vals, bins=[bins_x, HEATMAP_BINS], cmap="viridis")
            title_suffix = "pulses" if self.is_pulse else "V"
            ax.set_title(f"run {label} — {title_suffix}", fontsize=8)
            ax.tick_params(labelsize=7)

    def _update_waveforms(self, per_run):
        if not self.show_waveform:
            return
        for n, ax in self.ax_waveforms.items():
            state = per_run[n]["state"]
            label = make_label(n, self.serials, state)
            if state in ("no_data", "waiting"):
                self._placeholder(ax, f"run {label}")
                continue
            xs, ys = per_run[n]["waveform"]
            if not xs:
                self._placeholder(ax, f"run {label}\nno waveform yet")
                continue
            ax.clear()
            alpha = 0.5 if state == "stalled" else 1.0
            ax.plot(xs, ys, color=self.colors[n], linewidth=0.8, alpha=alpha)
            ax.set_ylim(-0.2, 3.5)
            ax.set_title(f"run {label}", fontsize=8)
            ax.tick_params(labelsize=7)

    def collect(self):
        per_run = {}
        now = time.time()
        for n in self.runs:
            basic = run_state_basic(self.root, n, self.source)
            wp = workspace_path(self.root, n, self.source)
            data = {}
            if basic == "no_data":
                data.update({"best": ([], [], [], [], [], []), "all": ([], []), "violin": (None, []),
                             "heatmap": ([], []), "waveform": ([], [])})
                state = "no_data"
                self._epoch_tracker.pop(n, None)
            else:
                data["best"] = parse_best(read_text_or_none(wp / "bestlivedata.log"))
                data["all"] = parse_all(read_text_or_none(wp / "alllivedata.log"))
                data["violin"] = parse_violin_latest(read_text_or_none(wp / "violinlivedata.log"))
                if self.show_heatmap:
                    fname = "pulselivedata.log" if self.is_pulse else "heatmaplivedata.log"
                    data["heatmap"] = parse_heatmap_points(read_text_or_none(wp / fname), self.is_pulse)
                else:
                    data["heatmap"] = ([], [])
                if self.show_waveform:
                    data["waveform"] = parse_waveform(read_text_or_none(wp / "waveformlivedata.log"))
                else:
                    data["waveform"] = ([], [])

                if basic == "waiting":
                    state = "waiting"
                    self._epoch_tracker.pop(n, None)
                else:
                    epoch = latest_epoch(data["best"])
                    last = self._epoch_tracker.get(n)
                    if last is None or epoch != last[0]:
                        self._epoch_tracker[n] = (epoch, now)
                        state = "live"
                    else:
                        stalled_for = now - last[1]
                        state = "stalled" if stalled_for > self.stall_seconds else "live"
            data["state"] = state
            per_run[n] = data
        return per_run

    def _refresh_pulse_mode(self):
        if not self.is_pulse:
            self.is_pulse = detect_pulse_mode(self.root, self.runs, self.source)

    def _suptitle(self, fig, title, states):
        live = sum(1 for s in states.values() if s == "live")
        fig.suptitle(f"{title} — {live}/{len(self.runs)} runs live", fontsize=12, y=0.985)

    def tick_scalars(self, _frame):
        self._refresh_pulse_mode()
        per_run = self.collect()
        states = {n: per_run[n]["state"] for n in self.runs}
        self._update_scalar_overlays(per_run)
        self._update_current_gen(per_run)
        for legend in list(self.fig_scalars.legends):
            legend.remove()
        self._draw_legend(self.fig_scalars, states)
        title = "EHW swarm live — scalars" + (" (pretty)" if self.pretty else "")
        self._suptitle(self.fig_scalars, title, states)

    def tick_heatmaps(self, _frame):
        self._refresh_pulse_mode()
        per_run = self.collect()
        states = {n: per_run[n]["state"] for n in self.runs}
        self._update_heatmaps(per_run)
        self._suptitle(self.fig_heatmaps, "EHW swarm live — heatmaps", states)

    def tick_waveforms(self, _frame):
        per_run = self.collect()
        states = {n: per_run[n]["state"] for n in self.runs}
        self._update_waveforms(per_run)
        self._suptitle(self.fig_waveforms, "EHW swarm live — waveforms", states)

    def run(self):
        if self.save_dir is not None:
            self._render_and_save()
            return
        self._anims = []
        self.tick_scalars(0)
        self._anims.append(animation.FuncAnimation(
            self.fig_scalars, self.tick_scalars,
            interval=self.frame_interval, cache_frame_data=False,
        ))
        if self.show_heatmap:
            self.tick_heatmaps(0)
            self._anims.append(animation.FuncAnimation(
                self.fig_heatmaps, self.tick_heatmaps,
                interval=self.frame_interval, cache_frame_data=False,
            ))
        if self.show_waveform:
            self.tick_waveforms(0)
            self._anims.append(animation.FuncAnimation(
                self.fig_waveforms, self.tick_waveforms,
                interval=self.frame_interval, cache_frame_data=False,
            ))
        plt.show()

    def _render_and_save(self):
        """Render each enabled figure once and write a PNG; no live windows."""
        self.save_dir.mkdir(parents=True, exist_ok=True)
        # Use stall_seconds = +inf during one-shot save so completed runs aren't
        # spuriously labeled "stalled" — they are by definition not advancing.
        self.stall_seconds = float("inf")
        outputs = []
        self.tick_scalars(0)
        scalars_path = self.save_dir / f"{self.save_prefix}_scalars.png"
        self.fig_scalars.savefig(scalars_path, dpi=self.save_dpi, bbox_inches="tight")
        outputs.append(scalars_path)
        if self.show_heatmap:
            self.tick_heatmaps(0)
            heatmaps_path = self.save_dir / f"{self.save_prefix}_heatmaps.png"
            self.fig_heatmaps.savefig(heatmaps_path, dpi=self.save_dpi, bbox_inches="tight")
            outputs.append(heatmaps_path)
        if self.show_waveform:
            self.tick_waveforms(0)
            waveforms_path = self.save_dir / f"{self.save_prefix}_waveforms.png"
            self.fig_waveforms.savefig(waveforms_path, dpi=self.save_dpi, bbox_inches="tight")
            outputs.append(waveforms_path)
        for p in outputs:
            print(f"wrote {p}")


def main():
    args = parse_args()
    runs = parse_run_spec(args.runs)
    if not runs:
        print(f"No valid runs in --runs spec '{args.runs}'", file=sys.stderr)
        sys.exit(2)
    Aggregator(args).run()


if __name__ == "__main__":
    main()
