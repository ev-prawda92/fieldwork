#!/usr/bin/env python3
"""
Sendero Classification Engine
=============================
The two-level outlier test at the heart of Sendero.

Given per-user (or per-unit) performance data for a single friction point,
it decides whether the friction is a SYSTEMATIC problem (a "build" /
configuration issue that a system owner must fix) or an INDIVIDUAL problem
(a "training" / adoption issue that coaching fixes) -- or a HYBRID of both.

The method is domain-agnostic. It works for Epic clinicians, Salesforce reps,
SAP planners, Workday buyers, retail cashiers -- anywhere a complex system is
used by many people and some of them struggle.

--------------------------------------------------------------------------
THE TWO LEVELS
--------------------------------------------------------------------------
Level 1 -- Dispersion test ("is everyone equally affected?")
    A build problem hits everyone roughly the same: the system is
    misconfigured, so the whole population is dragged down uniformly.
    That shows up as LOW relative dispersion (low coefficient of variation)
    combined with the whole population sitting worse than baseline.

    A training problem hits some people and not others: capable users are
    fine, strugglers are slow. That shows up as HIGH dispersion -- a long
    right tail of outliers over an otherwise-healthy body.

Level 2 -- Explanatory test ("what explains the spread?")
    We measure how much of the variation is explained by each cohort you
    provide (eta-squared from a one-way decomposition).
      * If a CAPABILITY cohort (tenure, experience, training-completed)
        explains the spread -> the strugglers cluster by skill -> TRAINING.
      * If a CONFIGURATION cohort (department, module, site, workflow,
        order-type) explains the spread AND those groups are internally
        uniform -> specific configurations are broken -> BUILD.

The two levels are combined into a single build_score in [0,1]:
    0.0 = pure training,  1.0 = pure build,  ~0.5 = genuine hybrid.

--------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------
    python classify.py data.csv --metric minutes --baseline 3 \
        --higher-is-worse \
        --capability tenure_months experience_level \
        --config department module

    # or drive it from JSON (what the skill does):
    python classify.py data.csv --json

CSV shape (one row per user/unit):
    user_id, minutes, tenure_months, department, ...
"""

import argparse
import csv
import json
import math
import sys
from collections import defaultdict

# Cohort-name keywords used to auto-classify a column when the caller does
# not tag it explicitly. Override with --capability / --config.
CAPABILITY_HINTS = (
    "tenure", "experience", "seniority", "training", "certified",
    "certification", "onboard", "hire", "skill", "proficiency", "level",
    "years", "age_in_role",
)
CONFIG_HINTS = (
    "department", "dept", "module", "site", "location", "facility", "store",
    "unit", "specialty", "workflow", "order_type", "ordertype", "template",
    "system", "instance", "region", "team", "line", "plant", "queue",
)


def _to_float(x):
    try:
        return float(str(x).strip())
    except (ValueError, AttributeError):
        return None


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def eta_squared(values, groups):
    """Fraction of total variance in `values` explained by `groups`.

    One-way ANOVA decomposition: SS_between / SS_total. Range [0,1].
    1.0 means the cohort perfectly predicts the metric; 0.0 means it tells
    you nothing.
    """
    paired = [(v, g) for v, g in zip(values, groups) if v is not None and g not in (None, "")]
    if len(paired) < 3:
        return 0.0
    vals = [v for v, _ in paired]
    grand = _mean(vals)
    ss_total = sum((v - grand) ** 2 for v in vals)
    if ss_total == 0:
        return 0.0
    buckets = defaultdict(list)
    for v, g in paired:
        buckets[g].append(v)
    if len(buckets) < 2:
        return 0.0
    ss_between = sum(len(b) * (_mean(b) - grand) ** 2 for b in buckets.values())
    return max(0.0, min(1.0, ss_between / ss_total))


def within_group_uniformity(values, groups):
    """Average coefficient of variation *inside* each group.

    Low = each configuration group is internally consistent (strong build
    signal: the whole group is uniformly affected). High = even within a
    group people vary (leans training).
    """
    buckets = defaultdict(list)
    for v, g in zip(values, groups):
        if v is not None and g not in (None, ""):
            buckets[g].append(v)
    cvs = []
    for b in buckets.values():
        if len(b) >= 2 and _mean(b) > 0:
            cvs.append(_std(b) / _mean(b))
    return _mean(cvs) if cvs else None


def tail_share(values, baseline, higher_is_worse):
    """Fraction of the population that is an outlier vs the healthy body.

    Uses a robust median + MAD fence. A big isolated tail => training;
    the whole body shifted => build.
    """
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0, 0.0
    n = len(vals)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    mad = _mean([abs(v - median) for v in vals]) or 1e-9
    if higher_is_worse:
        fence = median + 2.5 * mad
        outliers = [v for v in vals if v > fence]
        body_worse_than_baseline = median > baseline if baseline is not None else False
    else:
        fence = median - 2.5 * mad
        outliers = [v for v in vals if v < fence]
        body_worse_than_baseline = median < baseline if baseline is not None else False
    return len(outliers) / n, (1.0 if body_worse_than_baseline else 0.0)


def classify(rows, metric, baseline=None, higher_is_worse=True,
             capability_cols=None, config_cols=None):
    capability_cols = capability_cols or []
    config_cols = config_cols or []

    values = [_to_float(r.get(metric)) for r in rows]
    clean = [v for v in values if v is not None]
    if len(clean) < 3:
        return {"error": "Need at least 3 usable rows to classify."}

    mean = _mean(clean)
    std = _std(clean)
    cv = std / mean if mean else 0.0
    tail, body_shift = tail_share(clean, baseline, higher_is_worse)

    srt = sorted(clean)
    n = len(srt)
    median = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2

    # Body elevation: how far the ROBUST body (median, immune to the tail) sits
    # from baseline. A global config offset raises the floor for everyone --
    # including capable users -- without adding any cohort-explained variance,
    # so this is the one build signal a uniform misconfiguration leaves behind.
    if baseline and baseline > 0:
        if higher_is_worse:
            elevation = max(0.0, (median - baseline) / baseline)
        else:
            elevation = max(0.0, (baseline - median) / baseline)
    else:
        elevation = 0.0
    elevation_build = max(0.0, min(1.0, elevation / 0.5))  # 50% off baseline -> full

    # ---- LEVEL 1: dispersion signal -------------------------------------
    # High CV + big tail  -> training (some strugglers over a healthy body)
    # Low CV + body shifted worse than baseline -> build (uniform drag)
    # Map CV through a soft curve: CV 0.15 -> ~build, CV 0.55 -> ~training.
    cv_training = max(0.0, min(1.0, (cv - 0.15) / 0.40))
    tail_training = max(0.0, min(1.0, tail / 0.15))  # 15% tail -> full training
    disp_training = 0.6 * cv_training + 0.4 * tail_training
    if body_shift and cv < 0.25:
        disp_training *= 0.5
    dispersion_build = 1.0 - disp_training
    # Blend dispersion with the elevation signal.
    level1_build = 0.6 * dispersion_build + 0.4 * elevation_build
    level1_training = 1.0 - level1_build

    # ---- LEVEL 2: explanatory signal ------------------------------------
    cap_power = max([eta_squared(values, [r.get(c) for r in rows])
                     for c in capability_cols], default=0.0)
    cfg_eta = {c: eta_squared(values, [r.get(c) for r in rows]) for c in config_cols}
    cfg_power = max(cfg_eta.values(), default=0.0)
    # Config signal is only a *build* signal if groups are internally uniform.
    best_cfg = max(cfg_eta, key=cfg_eta.get) if cfg_eta else None
    cfg_uniform = None
    if best_cfg:
        cfg_uniform = within_group_uniformity(values, [r.get(best_cfg) for r in rows])
    cfg_build_signal = cfg_power
    if cfg_uniform is not None and cfg_uniform > 0.35:
        cfg_build_signal *= 0.5  # groups aren't actually uniform -> weaken

    # Level 2 leans build if config explains more than capability.
    total_power = cap_power + cfg_build_signal
    if total_power > 0.05:
        level2_build = cfg_build_signal / total_power
        level2_strength = min(1.0, total_power / 0.5)  # how confident level 2 is
    else:
        level2_build = 0.5
        level2_strength = 0.0

    # ---- COMBINE --------------------------------------------------------
    # Weight level 2 by how much explanatory power we actually found.
    w2 = 0.25 + 0.55 * level2_strength   # 0.25..0.80
    w1 = 1.0 - w2
    build_score = w1 * level1_build + w2 * level2_build
    # A strongly elevated body is a build signal that cohort-explained variance
    # (level 2) structurally cannot see, so let it lift the floor independently.
    # This is what makes genuine hybrids -- global config offset PLUS a skill
    # tail -- resolve to HYBRID instead of collapsing to one side.
    build_score = 1.0 - (1.0 - build_score) * (1.0 - 0.45 * elevation_build)
    build_score = max(0.0, min(1.0, build_score))

    if build_score >= 0.66:
        label, emoji = "BUILD", "🔴"
    elif build_score <= 0.34:
        label, emoji = "TRAINING", "🟢"
    else:
        label, emoji = "HYBRID", "🟡"

    # Confidence: strong when the two levels agree and level 2 has power.
    agreement = 1.0 - abs(level1_build - level2_build)
    confidence = round(100 * (0.4 + 0.35 * agreement + 0.25 * level2_strength))
    confidence = max(35, min(97, confidence))

    return {
        "classification": label,
        "emoji": emoji,
        "build_score": round(build_score, 3),
        "training_score": round(1 - build_score, 3),
        "confidence_pct": confidence,
        "n": len(clean),
        "metric": metric,
        "stats": {
            "mean": round(mean, 2),
            "std": round(std, 2),
            "coefficient_of_variation": round(cv, 3),
            "baseline": baseline,
            "pct_over_baseline": (
                round(100 * len([v for v in clean if (v > baseline) == higher_is_worse]) / len(clean))
                if baseline is not None else None
            ),
            "outlier_tail_share": round(tail, 3),
        },
        "level1_dispersion": {
            "build": round(level1_build, 3),
            "training": round(level1_training, 3),
            "reading": _level1_reading(cv, tail, body_shift, higher_is_worse),
        },
        "level2_explanatory": {
            "capability_power": round(cap_power, 3),
            "config_power": round(cfg_power, 3),
            "config_internally_uniform": (round(cfg_uniform, 3) if cfg_uniform is not None else None),
            "strongest_config_cohort": best_cfg,
            "build": round(level2_build, 3),
            "reading": _level2_reading(cap_power, cfg_power, cfg_uniform, best_cfg),
        },
        "recommendation": _recommend(label),
    }


def _level1_reading(cv, tail, body_shift, higher_is_worse):
    if cv < 0.25 and body_shift:
        return ("Low spread and the whole population sits worse than baseline: "
                "everyone is uniformly dragged down -> systematic (build) signal.")
    if cv > 0.45 or tail > 0.12:
        return ("High spread with a distinct tail of strugglers over an "
                "otherwise-healthy body -> individual (training) signal.")
    return "Mixed dispersion: neither a clean uniform drag nor a clean outlier tail."


def _level2_reading(cap, cfg, cfg_uniform, best_cfg):
    if cap > cfg and cap > 0.15:
        return (f"Most of the variation is explained by a capability cohort "
                f"(eta^2={cap:.2f}): the strugglers cluster by skill/experience -> training.")
    if cfg > cap and cfg > 0.15:
        u = "" if cfg_uniform is None else f", and those groups are internally {'uniform' if cfg_uniform < 0.35 else 'still varied'}"
        return (f"Most of the variation is explained by '{best_cfg}' (eta^2={cfg:.2f}){u} "
                f"-> specific configurations look broken -> build.")
    return "Neither cohort type explains much of the variation; leaning on the dispersion signal."


def _recommend(label):
    if label == "BUILD":
        return ("Route to the system/build owner. Fix the configuration once and "
                "the whole population benefits. Training these users will waste "
                "money -- the system is the constraint.")
    if label == "TRAINING":
        return ("Route to enablement/coaching. Pair strugglers with high performers, "
                "build muscle memory in a practice environment. Re-configuring the "
                "system will not help -- the design is fine, adoption is the gap.")
    return ("Split the work. Fix the configuration component first (build owner), "
            "then close the residual adoption gap with targeted coaching. Measure "
            "after the build fix to see how much training is actually still needed.")


def _load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _auto_cohorts(header, metric):
    cap, cfg = [], []
    for col in header:
        if col == metric:
            continue
        low = col.lower()
        if any(h in low for h in CAPABILITY_HINTS):
            cap.append(col)
        elif any(h in low for h in CONFIG_HINTS):
            cfg.append(col)
    return cap, cfg


def main():
    p = argparse.ArgumentParser(description="Sendero build-vs-training classifier")
    p.add_argument("csv", help="CSV with one row per user/unit")
    p.add_argument("--metric", required=True, help="Column holding the performance metric")
    p.add_argument("--baseline", type=float, default=None, help="Expected/target value")
    p.add_argument("--higher-is-worse", action="store_true", default=True,
                   help="Larger metric = worse (default). Use --lower-is-worse to flip.")
    p.add_argument("--lower-is-worse", dest="higher_is_worse", action="store_false")
    p.add_argument("--capability", nargs="*", default=None,
                   help="Cohort columns that reflect user capability (tenure, experience...)")
    p.add_argument("--config", nargs="*", default=None,
                   help="Cohort columns that reflect system configuration (department, module...)")
    p.add_argument("--json", action="store_true", help="Emit raw JSON only")
    args = p.parse_args()

    rows = _load_csv(args.csv)
    if not rows:
        print("No data rows found.", file=sys.stderr)
        sys.exit(1)

    cap, cfg = args.capability, args.config
    if cap is None and cfg is None:
        cap, cfg = _auto_cohorts(rows[0].keys(), args.metric)

    result = classify(rows, args.metric, args.baseline, args.higher_is_worse,
                      cap or [], cfg or [])

    if args.json:
        print(json.dumps(result, indent=2))
        return

    if "error" in result:
        print("Error:", result["error"], file=sys.stderr)
        sys.exit(1)

    r = result
    print(f"\n{r['emoji']}  SENDERO CLASSIFICATION: {r['classification']}  "
          f"({r['confidence_pct']}% confidence)")
    print(f"    build score {r['build_score']}  |  training score {r['training_score']}  "
          f"|  n={r['n']}")
    s = r["stats"]
    print(f"\n  Stats: mean={s['mean']} std={s['std']} CV={s['coefficient_of_variation']} "
          f"baseline={s['baseline']} tail={s['outlier_tail_share']}")
    print(f"\n  Level 1 (dispersion): {r['level1_dispersion']['reading']}")
    print(f"  Level 2 (explanatory): {r['level2_explanatory']['reading']}")
    print(f"\n  -> {r['recommendation']}\n")


if __name__ == "__main__":
    main()
