#!/usr/bin/env python3
"""Classify a Browser CLI long-run resources.jsonl handle trend.

The long-run gate writes one JSON line per browser *session* sample.  A
normal+agent run therefore has two lines with the same `elapsed_s`; this tool
pairs them so the aggregate handle count is comparable to the report's
"handles" figure (11.7k -> 12.6k in the 2026-11-01 20-minute run).

It distinguishes one-time startup growth from a sustained leak:

  * startup: 0 <= t <= 75 s
  * post-startup: t > 75 s
  * late plateau: t >= 600 s

A sustained leak requires the late-window least-squares slope to be positive
and material AND the correlation to be strong.  A single step (a page/scenario
transition) must not be reported as a leak; the largest one-sample deltas are
printed separately.

Usage:
  python3 tests/windows-rc-audit/longrun_handle_trend.py RESOURCES_JSONL
  python3 tests/windows-rc-audit/longrun_handle_trend.py RESOURCES_JSONL --json
  python3 tests/windows-rc-audit/longrun_handle_trend.py --self-test

Exit: 0 = no sustained handle leak, 1 = sustained leak or unreadable input.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

LEAK_SLOPE_PER_MIN = 1.0
LEAK_R = 0.5
LEAK_MIN_GROWTH_FRACTION = 0.005
WATCH_SLOPE_PER_MIN = 2.0
WATCH_R = 0.8


@dataclass
class Sample:
    elapsed_s: float
    handles: int
    processes: List[dict]


@dataclass
class Slope:
    per_min: float
    r: float
    a: float


def least_squares(points: Sequence[Tuple[float, float]]) -> Slope:
    n = len(points)
    if n < 2:
        return Slope(0.0, 0.0, 0.0)
    mx = sum(t for t, _ in points) / n
    my = sum(y for _, y in points) / n
    num = sum((t - mx) * (y - my) for t, y in points)
    den = sum((t - mx) ** 2 for t, _ in points)
    sy = sum((y - my) ** 2 for _, y in points)
    slope_per_s = num / den if den else 0.0
    r = num / math.sqrt(den * sy) if den and sy else 0.0
    return Slope(slope_per_s * 60.0, r, my - slope_per_s * mx)


def session_name(sample: Sample) -> str:
    try:
        cmd = sample.processes[0].get("command_line") or ""
    except (IndexError, AttributeError):
        return "unknown"
    m = re.search(r"browser-cli-longrun-([a-z0-9]+)-", cmd)
    return m.group(1) if m else "unknown"


def load_samples(path: Path) -> List[Sample]:
    samples: List[Sample] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        summary = row.get("summary") or {}
        handles = summary.get("handles")
        if not isinstance(handles, int):
            raise ValueError(f"{path}:{lineno}: missing integer summary.handles")
        samples.append(
            Sample(
                elapsed_s=float(row["elapsed_s"]),
                handles=handles,
                processes=list(row.get("processes") or []),
            )
        )
    if not samples:
        raise ValueError(f"{path}: no samples")
    return samples


def pair_by_elapsed(samples: Sequence[Sample]) -> List[Tuple[float, List[Sample]]]:
    groups: Dict[float, List[Sample]] = collections.defaultdict(list)
    for sample in samples:
        groups[round(sample.elapsed_s, 3)].append(sample)
    return sorted(groups.items())


def process_type(proc: dict) -> str:
    return str(proc.get("type") or proc.get("name") or "unknown")


def fmt_slope(slope: Slope) -> str:
    return f"{slope.per_min:+.2f}/min r={slope.r:.2f}"


def phase(points: Sequence[Tuple[float, float]], lo: float, hi: float) -> List[Tuple[float, float]]:
    return [p for p in points if lo <= p[0] <= hi]


def analyze(path: Path) -> dict:
    samples = load_samples(path)
    groups = pair_by_elapsed(samples)
    times = [t for t, _ in groups]
    total = [(t, sum(s.handles for s in rows)) for t, rows in groups]
    per_session: Dict[str, List[Tuple[float, int]]] = collections.defaultdict(list)
    for t, rows in groups:
        for row in rows:
            per_session[session_name(row)].append((t, row.handles))

    # Per process type and per PID.  A PID is stable for the whole run in this
    # gate; the PID total is the handle count of that process.
    type_series: Dict[str, List[Tuple[float, int]]] = collections.defaultdict(list)
    pid_series: Dict[Tuple[str, int], List[Tuple[float, int]]] = collections.defaultdict(list)
    pid_meta: Dict[Tuple[str, int], str] = {}
    for t, rows in groups:
        type_totals: Dict[str, int] = collections.defaultdict(int)
        for row in rows:
            for proc in row.processes:
                h = proc.get("handles")
                if not isinstance(h, int):
                    continue
                typ = process_type(proc)
                pid = int(proc["pid"])
                type_totals[typ] += h
                key = (typ, pid)
                pid_series[key].append((t, h))
                pid_meta[key] = session_name(row)
        for typ, value in type_totals.items():
            type_series[typ].append((t, value))

    # The gate samples just after the nominal 75 s boundary (75.009 s); use a
    # small tolerance so the phase boundary is the intended ~75 s sample.
    startup_end = max((t for t in times if t <= 75.5), default=times[0])
    late_start = min((t for t in times if t >= 600.0), default=times[-1])

    startup = phase(total, 0.0, startup_end)
    late = phase(total, late_start, times[-1])
    sustained = phase(total, startup_end, times[-1])
    startup_delta = startup[-1][1] - startup[0][1] if len(startup) > 1 else 0
    post_delta = total[-1][1] - startup[-1][1]
    late_delta = late[-1][1] - late[0][1] if len(late) > 1 else 0
    baseline = max(1, total[0][1])
    late_slope = least_squares(late)
    leak = (
        late_slope.per_min > LEAK_SLOPE_PER_MIN
        and late_slope.r >= LEAK_R
        and late_delta > baseline * LEAK_MIN_GROWTH_FRACTION
    )

    deltas = [
        (total[i][0], total[i][1] - total[i - 1][1])
        for i in range(1, len(total))
    ]
    max_delta = max(deltas, key=lambda p: p[1])
    min_delta = min(deltas, key=lambda p: p[1])
    steps = [p for p in deltas if abs(p[1]) >= 20]

    per_type = {}
    for typ, pts in sorted(type_series.items()):
        late_pts = phase(pts, 300.0, times[-1])
        slope = least_squares(late_pts)
        per_type[typ] = {
            "first": pts[0][1],
            "last": pts[-1][1],
            "min": min(v for _, v in pts),
            "max": max(v for _, v in pts),
            "sustained_slope_per_min": round(slope.per_min, 3),
            "sustained_r": round(slope.r, 3),
        }

    per_pid = []
    watch = []
    for (typ, pid), pts in sorted(pid_series.items(), key=lambda kv: (kv[0][0], kv[1][0][1])):
        late_pts = phase(pts, 300.0, times[-1])
        slope = least_squares(late_pts)
        entry = {
            "session": pid_meta[(typ, pid)],
            "type": typ,
            "pid": pid,
            "first": pts[0][1],
            "last": pts[-1][1],
            "min": min(v for _, v in pts),
            "max": max(v for _, v in pts),
            "sustained_slope_per_min": round(slope.per_min, 3),
            "sustained_r": round(slope.r, 3),
        }
        per_pid.append(entry)
        if typ in ("renderer", "extension-renderer") and slope.per_min > WATCH_SLOPE_PER_MIN and slope.r >= WATCH_R:
            watch.append(entry)

    return {
        "source": str(path),
        "rows": len(samples),
        "paired_samples": len(groups),
        "sessions": dict(collections.Counter(session_name(s) for _, rows in groups for s in rows)),
        "time_range_s": [times[0], times[-1]],
        "aggregate": {
            "first": total[0][1],
            "last": total[-1][1],
            "min": min(v for _, v in total),
            "max": max(total, key=lambda p: p[1]),
            "net": total[-1][1] - total[0][1],
            "net_pct": round(100.0 * (total[-1][1] - total[0][1]) / baseline, 2),
        },
        "phases": {
            "startup_0_75s": {
                "delta": startup_delta,
                "slope": least_squares(startup),
            },
            "post_startup_75s_end": {
                "delta": post_delta,
                "slope": least_squares(sustained),
            },
            "late_600s_end": {
                "delta": late_delta,
                "slope": late_slope,
            },
        },
        "largest_single_sample_delta": max_delta,
        "largest_single_sample_drop": min_delta,
        "steps_abs_ge_20": steps,
        "per_session": {
            name: {
                "first": pts[0][1],
                "last": pts[-1][1],
                "max": max(v for _, v in pts),
                "sustained_slope_per_min": round(least_squares(phase(pts, 75.5, times[-1])).per_min, 3),
                "sustained_r": round(least_squares(phase(pts, 75.5, times[-1])).r, 3),
            }
            for name, pts in sorted(per_session.items())
        },
        "per_type": per_type,
        "per_pid": per_pid,
        "watch_renderers": watch,
        "verdict": "FAIL" if leak else "PASS",
        "classification": (
            "sustained handle leak"
            if leak
            else "startup warm-up followed by plateau; no sustained handle leak"
        ),
        "thresholds": {
            "leak_slope_per_min": LEAK_SLOPE_PER_MIN,
            "leak_r": LEAK_R,
            "leak_min_growth_fraction": LEAK_MIN_GROWTH_FRACTION,
        },
    }


def print_report(result: dict) -> None:
    print(f"source: {result['source']}")
    print(
        f"samples: {result['rows']} rows -> {result['paired_samples']} paired samples; "
        f"sessions={result['sessions']}; t={result['time_range_s'][0]:.3f}..{result['time_range_s'][1]:.3f}s"
    )
    agg = result["aggregate"]
    mx_t, mx_v = agg["max"]
    print(
        f"aggregate handles: start={agg['first']} end={agg['last']} "
        f"min={agg['min']} max={mx_v}@{mx_t:.1f}s net={agg['net']:+d} ({agg['net_pct']:+.2f}%)"
    )
    for name, key in (
        ("startup 0-75s", "startup_0_75s"),
        ("post-startup 75s-end", "post_startup_75s_end"),
        ("late 600s-end", "late_600s_end"),
    ):
        p = result["phases"][key]
        print(f"  {name:22s} delta={p['delta']:+6d} slope={fmt_slope(p['slope'])}")
    print(
        "largest one-sample delta: %+d @%.1fs; largest drop: %+d @%.1fs"
        % (
            result["largest_single_sample_delta"][1],
            result["largest_single_sample_delta"][0],
            result["largest_single_sample_drop"][1],
            result["largest_single_sample_drop"][0],
        )
    )
    if result["steps_abs_ge_20"]:
        print(
            "single-sample steps |delta|>=20: "
            + ", ".join(f"{d:+d}@{t:.1f}s" for t, d in result["steps_abs_ge_20"])
        )
    print("per session:")
    for name, info in result["per_session"].items():
        print(
            f"  {name:7s} first={info['first']:5d} last={info['last']:5d} max={info['max']:5d} "
            f"sustained={info['sustained_slope_per_min']:+.2f}/min r={info['sustained_r']:.2f}"
        )
    print("per process type (slope measured from 300 s):")
    for typ, info in result["per_type"].items():
        print(
            f"  {typ:20s} first={info['first']:5d} last={info['last']:5d} "
            f"min={info['min']:5d} max={info['max']:5d} "
            f"slope={info['sustained_slope_per_min']:+.2f}/min r={info['sustained_r']:.2f}"
        )
    print("renderer/extension-renderer PIDs:")
    for info in result["per_pid"]:
        if info["type"] not in ("renderer", "extension-renderer"):
            continue
        print(
            f"  {info['session']:7s} {info['type']:18s} pid={info['pid']:6d} "
            f"first={info['first']:4d} last={info['last']:4d} "
            f"min={info['min']:4d} max={info['max']:4d} "
            f"slope={info['sustained_slope_per_min']:+.2f}/min r={info['sustained_r']:.2f}"
        )
    if result["watch_renderers"]:
        print("watch renderers (per-PID late-window slope):")
        for info in result["watch_renderers"]:
            print(
                f"  pid={info['pid']} {info['sustained_slope_per_min']:+.2f}/min r={info['sustained_r']:.2f}"
            )
    print(f"verdict: {result['verdict']} - {result['classification']}")
    print(
        "caveat: this only classifies handle counts in one 20-minute run; "
        "longer runs, GDI/USER objects, paged pool, heap growth and per-handle type are NOT-TESTED."
    )


def synthetic_rows(
    sessions: Sequence[str],
    series: Sequence[Tuple[float, int]],
    renderer_slope_per_min: float = 0.0,
) -> List[dict]:
    rows = []
    for t, handles in series:
        for idx, session in enumerate(sessions):
            # Split the aggregate across the two sessions; renderer PID 5000+idx
            # carries any requested drift.
            per_session = handles // len(sessions)
            renderer_handles = 300 + int(renderer_slope_per_min * t / 60.0)
            rows.append(
                {
                    "elapsed_s": t,
                    "summary": {"handles": per_session},
                    "processes": [
                        {
                            "pid": 5000 + idx,
                            "type": "renderer",
                            "handles": renderer_handles,
                            "command_line": f"chrome.exe --user-data-dir=browser-cli-longrun-{session}-x",
                        }
                    ],
                }
            )
    return rows


def self_test() -> int:
    import tempfile

    ok = True
    with tempfile.TemporaryDirectory(prefix="longrun-trend-selftest-") as tmp:
        tmpdir = Path(tmp)
        # Startup warm-up + flat plateau: must PASS.
        plateau = [(0.0, 1000), (30.0, 1400), (75.0, 1500)] + [
            (float(t), 1500 + (t // 300) % 3) for t in range(150, 1200, 30)
        ]
        p = tmpdir / "plateau.jsonl"
        p.write_text(
            "\n".join(json.dumps(r) for r in synthetic_rows(("normal", "agent"), plateau)) + "\n",
            encoding="utf-8",
        )
        result = analyze(p)
        if result["verdict"] != "PASS":
            ok = False
            print("SELFTEST FAIL: plateau classified as leak", file=sys.stderr)

        # Linear leak: must FAIL.
        leak_series = [(float(t), 1000 + int(t / 6)) for t in range(0, 1200, 30)]
        p = tmpdir / "leak.jsonl"
        p.write_text(
            "\n".join(json.dumps(r) for r in synthetic_rows(("normal", "agent"), leak_series)) + "\n",
            encoding="utf-8",
        )
        result = analyze(p)
        if result["verdict"] != "FAIL":
            ok = False
            print("SELFTEST FAIL: linear leak classified as PASS", file=sys.stderr)
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resources", nargs="?", help="resources.jsonl from the long-run gate")
    parser.add_argument("--json", action="store_true", help="emit the full analysis as JSON")
    parser.add_argument("--self-test", action="store_true", help="run synthetic plateau/leak cases")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.resources:
        parser.error("RESOURCES_JSONL is required unless --self-test is used")
    path = Path(args.resources)
    try:
        result = analyze(path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_report(result)
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
