#!/usr/bin/env python3
"""
Design sweep for the parametric aircraft
========================================

Generates a family of aircraft from one configuration by iterating selected
parameters while every other parameter stays fixed.

  * each iterated parameter gets a range [min, max] and a number of
    iterations (numbers), or a list of values (choices / NACA codes)
  * ranges must lie inside the parameter's own limits in the configuration
  * sampling: full grid (every combination), linked (all parameters step
    together) or random (Latin hypercube)
  * every design is checked exactly like a single aircraft: parameter
    limits, all cross-component constraints and the hard fit checks.
    Only designs that pass are written out / built; rejected ones are
    listed with the reason.

The sweep definition is stored in the configuration file under "sweep":

  "sweep": {"mode": "grid", "samples": 20, "seed": 1, "fill_valid": true,
            "variables": [{"param": "wing.span_mm", "min": 3000, "max": 5000, "count": 5},
                          {"param": "tail.configuration", "values": ["conventional", "v_tail"]}]}

Usage:
  python aircraft_sweep.py run   [--config FILE] [--vary wing.span_mm=3000:5000:5 ...]
                                 [--mode grid|linked|random] [--samples N] [--out DIR]
  python aircraft_sweep.py build DIR [--keep-open] [--cad solidworks|catia]
                                                       # build every design_*.json in DIR
"""

from __future__ import annotations

import argparse
import copy
import csv
import glob
import itertools
import json
import os
import re
import sys
from dataclasses import dataclass, field

import numpy as np

import aircraft_sw as A

MAX_DESIGNS = 5000          # designs evaluated in one sweep
MAX_COUNT = 200             # iterations of one parameter
MODES = {"grid": "Full grid", "linked": "Linked", "random": "Random (Latin hypercube)"}
METRICS = [("wing_area_mm2", "wing area [m2]", 1e-6), ("wing_mac_mm", "wing MAC [mm]", 1),
           ("tail_arm_mm", "tail arm [mm]", 1), ("tail_s_h_mm2", "S_h [m2]", 1e-6),
           ("tail_s_v_mm2", "S_v [m2]", 1e-6), ("aircraft_length_mm", "length [mm]", 1)]


# =============================================================================
# Sweep definition
# =============================================================================
@dataclass
class SweepVar:
    sec: str
    name: str
    kind: str                       # "number" or "list"
    lo: float = 0.0                 # iteration range (numbers)
    hi: float = 0.0
    count: int = 0
    is_int: bool = False
    lim_lo: float = 0.0             # the parameter's own limits
    lim_hi: float = 0.0
    values: list = field(default_factory=list)   # list parameters

    @property
    def key(self):
        return (self.sec, self.name)

    @property
    def label(self):
        return f"{self.sec}.{self.name}"

    def clean(self, v):
        """Round to a tidy value and keep it inside the parameter limits."""
        v = min(max(float(v), self.lim_lo), self.lim_hi)
        return int(round(v)) if self.is_int else float(f"{v:.6g}")

    def levels(self, n: int | None = None) -> list:
        """The values of this parameter for `n` evenly spaced iterations."""
        if self.kind == "list":
            return list(self.values)
        n = n or self.count
        raw = [self.lo] if n <= 1 else np.linspace(self.lo, self.hi, n)
        out = []
        for v in raw:
            v = self.clean(v)
            if v not in out:
                out.append(v)
        return out


def is_param_number(raw, sec, name) -> bool:
    default = getattr(A.SECTIONS[sec](), name)
    spec = raw[sec][name]
    return not isinstance(default, str) and "min" in spec and "max" in spec


def parse_sweep(raw: dict, sweep: dict | None = None):
    """Validate a sweep definition against the configuration.

    Returns (variables, settings, errors)."""
    sweep = raw.get("sweep", {}) if sweep is None else sweep
    errors, out, seen = [], [], set()
    settings = {"mode": sweep.get("mode", "grid"), "samples": sweep.get("samples", 20),
                "seed": sweep.get("seed", 1), "fill_valid": bool(sweep.get("fill_valid", True))}
    if settings["mode"] not in MODES:
        errors.append(f"sweep mode '{settings['mode']}' is not one of {list(MODES)}.")
    if not (isinstance(settings["samples"], int) and 1 <= settings["samples"] <= MAX_DESIGNS):
        errors.append(f"number of random designs must be an integer 1 … {MAX_DESIGNS}.")
    if not isinstance(settings["seed"], int):
        errors.append("random seed must be an integer.")

    for item in sweep.get("variables", []):
        param = str(item.get("param", ""))
        sec, _, name = param.partition(".")
        if sec not in A.SECTIONS or name not in raw.get(sec, {}):
            errors.append(f"sweep: unknown parameter '{param}'.")
            continue
        if name in A.DERIVED_FIELDS[sec]:
            errors.append(f"sweep: {param} is derived and cannot be iterated.")
            continue
        if param in seen:
            errors.append(f"sweep: {param} is listed twice.")
            continue
        seen.add(param)
        spec = raw[sec][name]
        if is_param_number(raw, sec, name):
            default = getattr(A.SECTIONS[sec](), name)
            v = SweepVar(sec, name, "number", is_int=isinstance(default, int),
                         lim_lo=float(spec["min"]), lim_hi=float(spec["max"]))
            lo, hi, n = item.get("min"), item.get("max"), item.get("count")
            if not all(A._is_number(x) for x in (lo, hi)):
                errors.append(f"sweep {param}: min and max must be numbers.")
                continue
            if not (isinstance(n, int) and 2 <= n <= MAX_COUNT):
                errors.append(f"sweep {param}: number of iterations must be an integer "
                              f"2 … {MAX_COUNT}.")
                continue
            bad = [x for x in (lo, hi) if not v.lim_lo <= x <= v.lim_hi]
            if bad:
                errors.append(f"sweep {param}: {bad[0]:g} is outside the parameter limits "
                              f"[{v.lim_lo:g}, {v.lim_hi:g}].")
                continue
            if lo >= hi:
                errors.append(f"sweep {param}: min ({lo:g}) must be smaller than max ({hi:g}).")
                continue
            v.lo, v.hi, v.count = float(lo), float(hi), n
        else:
            vals = item.get("values")
            if not isinstance(vals, list) or not vals:
                errors.append(f"sweep {param}: give a non-empty list of values.")
                continue
            ok = []
            for x in vals:
                if "choices" in spec and x not in spec["choices"]:
                    errors.append(f"sweep {param}: '{x}' is not one of {spec['choices']}.")
                elif "pattern" in spec and not re.search(spec["pattern"], str(x)):
                    errors.append(f"sweep {param}: '{x}' is not valid "
                                  f"({spec.get('description', spec['pattern'])}).")
                elif x not in ok:
                    ok.append(x)
            if not ok:
                continue
            v = SweepVar(sec, name, "list", values=ok)
        out.append(v)

    if not errors and out:
        n = design_count(out, settings["mode"], settings["samples"])
        if n > MAX_DESIGNS:
            errors.append(f"the sweep would create {n:,} designs (limit {MAX_DESIGNS:,}): "
                          "reduce the iterations or use random sampling.")
    return out, settings, errors


def design_count(variables, mode, samples) -> int:
    if not variables:
        return 0
    if mode == "random":
        return samples
    sizes = [len(v.levels()) for v in variables]
    if mode == "linked":
        return max(sizes)
    n = 1
    for s in sizes:
        n *= s
    return n


# =============================================================================
# Sampling
# =============================================================================
def assignments(variables, mode, samples=20, seed=1):
    """List of {(sec, name): value} for the chosen sampling mode."""
    if not variables:
        return []
    if mode == "grid":
        keys = [v.key for v in variables]
        return [dict(zip(keys, combo))
                for combo in itertools.product(*(v.levels() for v in variables))]
    if mode == "linked":
        n = max(len(v.levels()) for v in variables)
        cols = {}
        for v in variables:
            lv = v.levels(n)
            cols[v.key] = [lv[k % len(lv)] for k in range(n)]
        return [{k: col[i] for k, col in cols.items()} for i in range(n)]
    # Latin hypercube: every variable covers its range evenly, in random order
    rng = np.random.default_rng(seed)
    cols = {}
    for v in variables:
        if v.kind == "number":
            u = (rng.permutation(samples) + rng.random(samples)) / samples
            cols[v.key] = [v.clean(v.lo + x * (v.hi - v.lo)) for x in u]
        else:
            vals = list(np.resize(np.array(v.values, dtype=object), samples))
            rng.shuffle(vals)
            cols[v.key] = vals
    return [{k: col[i] for k, col in cols.items()} for i in range(samples)]


# =============================================================================
# Evaluation
# =============================================================================
@dataclass
class Design:
    index: int
    values: dict                    # {(sec, name): value} of the iterated parameters
    ok: bool
    issues: list
    metrics: dict


def base_of(raw: dict) -> dict:
    """The part of a configuration a design is made of (no sweep / comments)."""
    return {k: copy.deepcopy(raw[k]) for k in list(A.SECTIONS) + ["constraints"] if k in raw}


def design_raw(base: dict, values: dict) -> dict:
    raw = copy.deepcopy(base)
    for (sec, name), v in values.items():
        raw[sec][name]["value"] = v
    return raw


def analyse(raw: dict):
    """(cfg, ac, issues) - same checks as a single aircraft build."""
    try:
        cfg = A.config_from_dict(raw)
        p = cfg.params
        ac = A.integrate(p["wing"], p["fuselage"], p["tail"], p["integration"])
    except A.ConfigError as exc:
        return None, None, list(exc.errors)
    except Exception as exc:                                    # geometry failure
        return None, None, [f"{type(exc).__name__}: {exc}"]
    results = A.evaluate_constraints(cfg, ac)
    issues = A.hard_checks(ac) + [msg for _, _, ok, msg in results if not ok]
    return cfg, ac, issues


def evaluate(base: dict, index: int, values: dict) -> Design:
    _, ac, issues = analyse(design_raw(base, values))
    metrics = {k: ac.derived[k] for k, _, _ in METRICS} if ac is not None else {}
    return Design(index, values, not issues, issues, metrics)


def run(base: dict, variables, settings, callback=None, stop=None) -> list[Design]:
    """Evaluate the sweep. `callback(design, done, total)` after each design,
    `stop()` returning True cancels."""
    mode, n, seed = settings["mode"], settings["samples"], settings["seed"]
    designs = []
    if mode != "random" or not settings.get("fill_valid", True):
        todo = assignments(variables, mode, n, seed)
        for i, vals in enumerate(todo, 1):
            if stop and stop():
                break
            designs.append(evaluate(base, i, vals))
            if callback:
                callback(designs[-1], i, len(todo))
        return designs
    # random, keep sampling until n valid designs are found (bounded effort)
    max_tries, valid, batch = max(20 * n, 200), 0, 0
    while valid < n and len(designs) < max_tries:
        for vals in assignments(variables, mode, n, seed + batch):
            if stop and stop():
                return designs
            d = evaluate(base, len(designs) + 1, vals)
            designs.append(d)
            valid += d.ok
            if callback:
                callback(d, valid, n)
            if valid >= n or len(designs) >= max_tries:
                break
        batch += 1
    return designs


# =============================================================================
# Output
# =============================================================================
def design_file(folder, index, total):
    return os.path.join(folder, f"design_{index:0{max(3, len(str(total)))}d}.json")


def write_designs(base: dict, designs, folder, previews=False, progress=None):
    """Write every valid design as a configuration file plus a summary CSV of
    all designs. Returns (files written, csv path)."""
    os.makedirs(folder, exist_ok=True)
    for old in glob.glob(os.path.join(folder, "design_*.json")):
        os.remove(old)
    total = max((d.index for d in designs), default=0)
    keys = list(designs[0].values) if designs else []
    written = []
    valid = [d for d in designs if d.ok]
    for k, d in enumerate(valid, 1):
        raw = design_raw(base, d.values)
        raw["_design"] = {"index": d.index,
                          "iterated": {f"{s}.{n}": v for (s, n), v in d.values.items()}}
        path = design_file(folder, d.index, total)
        with open(path, "w") as fh:
            fh.write(A.dump_config(raw))
        written.append(path)
        if previews:
            _, ac, _ = analyse(design_raw(base, d.values))
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                A.plot_aircraft(ac, path[:-5] + ".png")
        if progress:
            progress(k, len(valid))
    csv_path = os.path.join(folder, "sweep_summary.csv")
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["design", "status"] + [f"{s}.{n}" for s, n in keys]
                   + [lab for _, lab, _ in METRICS] + ["file", "issues"])
        for d in designs:
            w.writerow([d.index, "valid" if d.ok else "rejected"]
                       + [d.values[k] for k in keys]
                       + [f"{d.metrics[m] * f:.6g}" if m in d.metrics else "" for m, _, f in METRICS]
                       + [os.path.basename(design_file(folder, d.index, total)) if d.ok else "",
                          " | ".join(d.issues)])
    return written, csv_path


def build_folder(folder, keep_open=False, cad=A.DEFAULT_CAD) -> int:
    """Build every design_*.json in `folder` into its own part in the chosen
    CAD system (design_###.SLDPRT for SolidWorks, design_###.CATPart for CATIA)."""
    files = sorted(glob.glob(os.path.join(folder, "design_*.json")))
    if not files:
        print(f"No design_*.json files in {folder}")
        return 1
    print(f"Building {len(files)} designs from {folder} in {A.cad_label(cad)}")
    builder, failed = None, 0
    for k, path in enumerate(files, 1):
        name = os.path.basename(path)[:-5]
        print(f"[{k}/{len(files)}] {name}", flush=True)
        try:
            with open(path) as fh:
                cfg, ac, issues = analyse(json.load(fh))
            if issues:                    # never build a design that breaks a rule
                failed += 1
                print("  skipped - " + "; ".join(issues))
                continue
            if builder is None:
                builder = A.make_builder(cad, new_part=True)
            try:
                saved = builder.build(cfg, ac, os.path.join(folder, name + A.cad_extension(cad)))
            finally:                       # never leave a half-built part open
                if not keep_open and getattr(builder, "model", None) is not None:
                    try:
                        builder.close()
                    except Exception as exc:
                        print(f"  [warn] could not close the part: {exc}")
            if not saved:
                failed += 1
        except Exception as exc:
            failed += 1
            print(f"  FAILED: {type(exc).__name__}: {exc}")
    print(f"Finished: {len(files) - failed} of {len(files)} designs built"
          + (f", {failed} failed" if failed else "") + ".")
    return 0 if not failed else 1


# =============================================================================
# CLI
# =============================================================================
def _parse_vary(raw, items):
    out = []
    for item in items:
        param, _, spec = item.partition("=")
        sec, _, name = param.partition(".")
        if sec in raw and name in raw[sec] and is_param_number(raw, sec, name):
            lo, hi, n = spec.split(":")
            out.append({"param": param, "min": float(lo), "max": float(hi), "count": int(n)})
        else:
            out.append({"param": param, "values": [s for s in spec.split(",") if s]})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Design sweep for the parametric aircraft")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="generate and check the designs")
    r.add_argument("--config", default=A.DEFAULT_CONFIG)
    r.add_argument("--vary", action="append", default=[],
                   metavar="SEC.NAME=MIN:MAX:N | SEC.NAME=A,B,C",
                   help="iterate a parameter (replaces the sweep in the config file)")
    r.add_argument("--mode", choices=list(MODES))
    r.add_argument("--samples", type=int, help="designs for random sampling")
    r.add_argument("--seed", type=int)
    r.add_argument("--out", help="write valid designs + sweep_summary.csv to this folder")
    r.add_argument("--previews", action="store_true", help="also save a preview image per design")
    b = sub.add_parser("build", help="build every design_*.json of a folder in SolidWorks "
                                     "or CATIA")
    b.add_argument("folder")
    b.add_argument("--cad", choices=list(A.CAD_SYSTEMS), default=A.DEFAULT_CAD,
                   help="CAD system to build in (default: %(default)s)")
    b.add_argument("--keep-open", action="store_true", help="leave the parts open")
    args = ap.parse_args(argv)

    if args.cmd == "build":
        return build_folder(args.folder, args.keep_open, args.cad)

    with open(args.config) as fh:
        raw = json.load(fh)
    sweep = dict(raw.get("sweep", {}))
    if args.vary:
        sweep["variables"] = _parse_vary(raw, args.vary)
    for k in ("mode", "samples", "seed"):
        if getattr(args, k) is not None:
            sweep[k] = getattr(args, k)
    variables, settings, errors = parse_sweep(raw, sweep)
    if not variables and not errors:
        errors.append("nothing to iterate: add --vary or a 'sweep' block to the config.")
    if errors:
        print("Sweep rejected:")
        for e in errors:
            print(f"  - {e}")
        return 2
    base = base_of(raw)
    print(f"Sweep: {MODES[settings['mode']]}, "
          f"{design_count(variables, settings['mode'], settings['samples'])} designs")
    for v in variables:
        lv = v.levels()
        print(f"  {v.label:32s} {len(lv):3d} values: "
              + ", ".join(map(str, lv[:6])) + (" …" if len(lv) > 6 else ""))
    designs = run(base, variables, settings)
    for d in designs:
        vals = "  ".join(f"{n}={v}" for (_, n), v in d.values.items())
        print(f"  #{d.index:<4d} {'valid   ' if d.ok else 'REJECTED'} {vals}"
              + ("" if d.ok else f"   ({d.issues[0][:90]})"))
    n_ok = sum(d.ok for d in designs)
    print(f"{n_ok} of {len(designs)} designs satisfy every limit and constraint.")
    if args.out:
        files, csv_path = write_designs(base, designs, args.out, args.previews)
        print(f"Wrote {len(files)} design files and {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
