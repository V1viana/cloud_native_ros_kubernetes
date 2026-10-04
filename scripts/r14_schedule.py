#!/usr/bin/env python3
"""R14 calendar (docs/R14_PREREGISTRATION_DRAFT.md, sections 2 and 3): the fixed order of the
314 runs, generated and checked here, never decided by the wrapper at run time.

  r14_schedule.py              -> the calendar as JSON lines on stdout
  r14_schedule.py --summary    -> pairs and runs per configuration, per round

Rules:
- [D] pairs per configuration (section 2): 10 for the central configurations; 1 for the S2
  controls; 5 for each S4-l1 cell; 10 at N=3 and 10 at N=10 for S3; 10 TTR.
- [D] a pair is A and B of the same configuration, back to back; the leading variant
  alternates with the pair index k: A-B for odd k, B-A for even k.
- [D] by rounds: pair k of every configuration before pair k+1.
- [P] inside a round, groups in the fixed order base, S1, S2, S4, TTR, S3; inside a group,
  the order of CONFIGS below.
- [D] exception in round 1: the S3 N=10 pair runs FIRST (it must confirm the window E
  measurable with L3 in A and B); in later rounds S3 is back at the end.
- A configuration with fewer pairs than the round number is simply absent from that round.
Pilots, gates, qualifications and revalidations are not in the calendar (section 2).
"""

import collections
import json
import sys

# (group, configuration, pairs, how it is run) -- the "how" is read by the wrapper
CONFIGS = [
    ("base", "e0", 10, {"runner": "campaign", "scenario": "e0"}),
    ("base", "e1", 10, {"runner": "campaign", "scenario": "e1"}),
    ("base", "e2", 10, {"runner": "campaign", "scenario": "e2"}),
    ("base", "p2", 10, {"runner": "campaign", "scenario": "p2"}),
    ("base", "e4", 10, {"runner": "campaign", "scenario": "e4"}),
    ("base", "u1", 10, {"runner": "campaign", "scenario": "u1"}),
    ("base", "u2", 10, {"runner": "campaign", "scenario": "u2"}),
    ("s1", "s1-delete", 10, {"runner": "campaign", "scenario": "s1", "S1_CASE": "delete"}),
    ("s2", "s2-control", 1, {"runner": "s2", "case": "control"}),
    ("s2", "s2-partition-only", 1, {"runner": "s2", "case": "partition-only"}),
    ("s2", "s2-short", 10, {"runner": "s2", "case": "short"}),
    ("s2", "s2-long", 10, {"runner": "s2", "case": "long"}),
    ("s4", "s4-l3", 10, {"runner": "s4", "cell": "l3"}),
    ("s4", "s4-l1-battery", 5, {"runner": "s4", "cell": "l1-battery"}),
    ("s4", "s4-l1-telemetry", 5, {"runner": "s4", "cell": "l1-telemetry"}),
    ("s4", "s4-l1-edge", 5, {"runner": "s4", "cell": "l1-edge"}),
    ("ttr", "ttr", 10, {"runner": "ttr"}),
    ("s3", "s3-n3", 10, {"runner": "s3", "n_robots": 3}),
    ("s3", "s3-n10", 10, {"runner": "s3", "n_robots": 10}),
]
GROUP_ORDER = ("base", "s1", "s2", "s4", "ttr", "s3")
FIRST_IN_ROUND_1 = "s3-n10"
EXPECTED = {"pairs": 157, "runs": 314}


def pair_variants(k):
    """A-B for odd k, B-A for even k (k counts from 1)."""
    return ("a", "b") if k % 2 == 1 else ("b", "a")


def build():
    rounds = max(c[2] for c in CONFIGS)
    by_group = {g: [c for c in CONFIGS if c[0] == g] for g in GROUP_ORDER}
    out = []
    for k in range(1, rounds + 1):
        order = [c for g in GROUP_ORDER for c in by_group[g] if k <= c[2]]
        if k == 1:
            first = [c for c in order if c[1] == FIRST_IN_ROUND_1]
            order = first + [c for c in order if c[1] != FIRST_IN_ROUND_1]
        for group, config, _, how in order:
            for position, variant in enumerate(pair_variants(k), 1):
                out.append({"seq": len(out) + 1, "round": k, "group": group, "config": config, "pair": k,
                            "variant": variant, "position_in_pair": position, "how": how})
    return out


def check(calendar):
    """Every rule above, on the generated calendar; raises on the first violation."""
    pairs = collections.Counter((e["config"], e["pair"]) for e in calendar)
    assert all(n == 2 for n in pairs.values()), "a pair without exactly two runs"
    assert len(pairs) == EXPECTED["pairs"] and len(calendar) == EXPECTED["runs"], (len(pairs), len(calendar))
    for group, config, n, _ in CONFIGS:
        assert sorted({p for c, p in pairs if c == config}) == list(range(1, n + 1)), config
    for a, b in zip(calendar[0::2], calendar[1::2]):
        assert (a["config"], a["pair"]) == (b["config"], b["pair"]), "A and B not back to back"
        assert (a["variant"], b["variant"]) == pair_variants(a["pair"]) and a["variant"] != b["variant"]
    rounds = [e["round"] for e in calendar]
    assert rounds == sorted(rounds), "pair k+1 before pair k of another configuration"
    assert calendar[0]["config"] == FIRST_IN_ROUND_1 and calendar[1]["config"] == FIRST_IN_ROUND_1
    for k in sorted(set(rounds)):
        rest = [e for e in calendar if e["round"] == k and not (k == 1 and e["config"] == FIRST_IN_ROUND_1)]
        groups = [e["group"] for e in rest]
        assert groups == sorted(groups, key=GROUP_ORDER.index), f"group order in round {k}"
    return True


S1_BLOCK = {"config": "s1-delete", "pairs": 10}


def build_s1_block():
    """The S1-delete block run after the campaign (docs/S1_DELETE_BLOCK_PROPOSAL_DRAFT.md 3):
    10 NEW A/B pairs of the same configuration and the same `how` as in the campaign, back to
    back, the leading variant alternating with the pair (A-B, B-A, ...). The rows are numbered
    from 1 in this block; they are not rows of the 314-run calendar."""
    config = next(c for c in CONFIGS if c[1] == S1_BLOCK["config"])
    out = []
    for k in range(1, S1_BLOCK["pairs"] + 1):
        for position, variant in enumerate(pair_variants(k), 1):
            out.append({"seq": len(out) + 1, "round": k, "group": config[0], "config": config[1], "pair": k,
                        "variant": variant, "position_in_pair": position, "how": config[3]})
    return out


def check_s1_block(calendar):
    assert len(calendar) == 2 * S1_BLOCK["pairs"], len(calendar)
    assert [e["seq"] for e in calendar] == list(range(1, len(calendar) + 1))
    assert {e["config"] for e in calendar} == {S1_BLOCK["config"]}
    assert all(e["how"] == next(c for c in CONFIGS if c[1] == S1_BLOCK["config"])[3] for e in calendar)
    for a, b in zip(calendar[0::2], calendar[1::2]):
        assert a["pair"] == b["pair"] and a["variant"] != b["variant"], "A and B not back to back"
        assert (a["variant"], b["variant"]) == pair_variants(a["pair"])
    assert [e["pair"] for e in calendar[0::2]] == list(range(1, S1_BLOCK["pairs"] + 1))
    return True


def summary(calendar):
    per_config = collections.Counter(e["config"] for e in calendar)
    per_round = collections.Counter(e["round"] for e in calendar)
    return {"runs": len(calendar), "pairs": len(calendar) // 2,
            "runs_per_config": dict(per_config), "runs_per_round": dict(sorted(per_round.items()))}


if __name__ == "__main__":
    cal = build()
    check(cal)
    if len(sys.argv) > 1 and sys.argv[1] == "--summary":
        print(json.dumps(summary(cal), indent=1))
    else:
        for entry in cal:
            print(json.dumps(entry))
