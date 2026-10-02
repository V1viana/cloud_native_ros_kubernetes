#!/usr/bin/env python3
"""S2 run record (R11, contract s2-partition-v1): the pieces of run.json that
scripts/run_s2.sh writes, and the cell's REPORT.md from the judge's output.

  inputs SOURCE                 the inputs as they are: HEAD, the SHA-256 of every
                                tracked file (submodules included; the thesis and
                                results excluded, the proposal kept), the modified
                                files, and the bench files one by one -- taken
                                before and after the run, compared by the judge;
  set RUN_JSON KEY VALUE        a field (dotted key), VALUE parsed as JSON if it
                                is JSON, else kept as text;
  set-file RUN_JSON KEY PATH    a field from a JSON file;
  precondition RUN_JSON NAME OK DETAIL   one precondition (OK: true/false);
  report RESULT_DIR [SUFFIX]    REPORT.md from s2-judge<SUFFIX>.json;
  qreport RESULT_DIR [SUFFIX]   REPORT.md of a qualification, from s2-qualify<SUFFIX>.json;
  concurrent-runs PID           processes really running a test script, PID and its
                                descendants excluded (S2_PROC_ROOT: /proc by default).
Tested offline: operator/tests/test_s2_record.py.
"""

import hashlib
import json
import os
import re
import subprocess
import sys

# The thesis and the results are not inputs; the proposal (Casale/proposal/) is
# the scope reference and stays recorded (review of a41c087, choice 14).
EXCLUDED = ("Casale/thesis/", "results/")
BENCH = ("scripts/run_s2.sh", "scripts/s2_phase.py", "scripts/s2_judge.py", "scripts/s2_control.py",
         "scripts/s2_qualify.py", "scripts/s2_qualify_judge.py",
         "scripts/s2_partition.py", "scripts/s2_observers.py", "scripts/s2_ground_truth.py", "scripts/s2_record.py",
         "scripts/run_e0.sh", "scripts/render_s2_k3d_config.py", "manifests/kubernetes/e0/k3d-cloud-native-e0.yaml",
         "manifests/kubernetes/s2/10-s2-harness.yaml",
         "manifests/kubernetes/s2/20-s2-health-prober.yaml", "manifests/kubernetes/s2/70-s2-adaptationpolicy-b.yaml",
         "sources/s2_harness/s2_harness/core.py", "sources/s2_harness/s2_harness/node.py",
         "sources/s2_harness/s2_harness/prober_core.py", "sources/s2_harness/s2_harness/prober.py",
         "sources/operational_event_dispatcher/operational_event_dispatcher/event_trace.py")


# A process runs a test script when one of its arguments IS a runner's path -- not
# when a longer text (a `bash -c` command, a heredoc) merely mentions it (decision 6
# after the first qualification round: the launch shell was refused).
RUNNER_ARG = re.compile(r"(?:\S*/)?scripts/run_(?:campaign|e[0-9]|p2|s[0-9]|u[0-9]|window_transport)[^/\s]*\.sh")


def _read_proc(root, pid):
    try:
        with open(os.path.join(root, str(pid), "cmdline"), "rb") as h:
            argv = [a.decode(errors="replace") for a in h.read().split(b"\0") if a]
        with open(os.path.join(root, str(pid), "stat")) as h:
            ppid = int(h.read().rsplit(")", 1)[1].split()[1])
        return ppid, argv
    except (OSError, ValueError, IndexError):
        return None                          # gone meanwhile, or not readable


def concurrent_runs(proc_root, self_pid):
    """Processes running a test script, other than `self_pid` and its descendants."""
    table = {}
    for name in os.listdir(proc_root):
        if name.isdigit():
            entry = _read_proc(proc_root, int(name))
            if entry is not None:
                table[int(name)] = entry

    def descends(pid):
        seen = set()
        while pid in table and pid not in seen:
            if pid == self_pid:
                return True
            seen.add(pid)
            pid = table[pid][0]
        return pid == self_pid
    return [{"pid": pid, "ppid": ppid, "argv": argv[:6]} for pid, (ppid, argv) in sorted(table.items())
            if not descends(pid) and any(RUNNER_ARG.fullmatch(a) for a in argv)]


def _git(source, *args):
    return subprocess.run(["git", "-C", source, *args], capture_output=True, text=True, check=True).stdout


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inputs(source):
    files = [f for f in _git(source, "ls-files", "-z", "--recurse-submodules").split("\0")
             if f and not f.startswith(EXCLUDED)]
    tree = hashlib.sha256()
    missing = []
    for f in sorted(files):
        path = os.path.join(source, f)
        if os.path.isfile(path) and not os.path.islink(path):
            tree.update(f"{f}\0{_sha(path)}\n".encode())
        else:
            missing.append(f)
    dirty = [line[3:] for line in _git(source, "status", "--porcelain").splitlines()
             if line[3:] and not line[3:].startswith(EXCLUDED)]
    return {"head": _git(source, "rev-parse", "HEAD").strip(), "tree_sha256": tree.hexdigest(),
            "files": len(files), "not_regular_count": len(missing), "not_regular": missing[:20], "dirty": dirty,
            "bench": {f: _sha(os.path.join(source, f)) if os.path.isfile(os.path.join(source, f)) else None
                      for f in BENCH}}


def _load(path):
    try:
        with open(path) as h:
            return json.load(h)
    except (OSError, ValueError):
        return {}


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as h:
        json.dump(data, h, indent=1)
    os.replace(tmp, path)


def set_field(path, key, value):
    data = _load(path)
    node = data
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    _save(path, data)


def parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def report(result_dir, suffix=""):
    with open(os.path.join(result_dir, f"s2-judge{suffix}.json")) as h:
        j = json.load(h)
    gt, rec, act = j.get("ground_truth") or {}, j.get("recognized") or {}, j.get("action") or {}
    episode = gt.get("episode") or {}
    missed = j.get("missed_transient") or {}
    lines = [f"# S2 {j.get('variant', '?').upper()} / {j.get('case')}: {j.get('verdict')}", "",
             f"Protocollo `{j.get('protocol_id')}`, run `{j.get('run_id')}`, cartella `{j.get('result_dir')}`.", "",
             "| Dimensione | Esito |", "| --- | --- |",
             f"| Validita' | {'ok' if (j.get('validity') or {}).get('ok') else 'problemi'} |",
             f"| Episodi locali nella fase | {gt.get('phase_episodes')} |",
             f"| Episodio valido | {gt.get('episode_valid')} |",
             f"| Rientro locale (UTC) | {(episode.get('returned') or {}).get('utc')} |",
             f"| Riconosciuto | {rec.get('value')} ({rec.get('reason') or 'ok'}) |",
             f"| Esito azione | {act.get('outcome')} |",
             f"| Ordine azione/rientro | {(act.get('order_vs_local_return') or {}).get('request')} |",
             f"| Riconvergenza | {(j.get('reconvergence') or {}).get('value')} |",
             f"| Isolamento drone02/03 | {(j.get('isolation') or {}).get('status')} |",
             f"| Continuita' PX4 | {(j.get('px4_continuity') or {}).get('status')} |"]
    if missed:
        lines.append(f"| missed_within_180s | {missed.get('missed_within_180s')} (eleggibile: {missed.get('eligible')}) |")
    lines += ["", "## Motivi", ""]
    for kind, reasons in (j.get("reasons") or {}).items():
        for reason in reasons:
            lines.append(f"- {kind}: {reason}")
    if not any((j.get("reasons") or {}).values()):
        lines.append("- nessuno")
    lines += ["", "Il valutatore e' offline (`scripts/s2_judge.py`); i dettagli sono in "
              f"`s2-judge{suffix}.json`. Una rivalutazione va su un file nuovo."]
    path = os.path.join(result_dir, f"REPORT{suffix}.md")
    with open(path, "w") as h:
        h.write("\n".join(lines) + "\n")
    return path


def qreport(result_dir, suffix=""):
    with open(os.path.join(result_dir, f"s2-qualify{suffix}.json")) as h:
        j = json.load(h)
    lines = [f"# S2 qualification {str(j.get('variant', '?')).upper()}: {j.get('verdict')}", "",
             f"Protocollo `{j.get('protocol_id')}`, run `{j.get('run_id')}`, cartella `{j.get('result_dir')}`.",
             "Evidenza del banco: non entra nelle otto celle.", "", "| Passo | Esito |", "| --- | --- |"]
    for name, step in (j.get("steps") or {}).items():
        lines.append(f"| {name} | {step.get('status')} |")
    lines += ["", "## Controlli", ""]
    for name, step in (j.get("steps") or {}).items():
        for c in step.get("checks") or []:
            lines.append(f"- {name}: {c.get('check')} -- {c.get('status')}")
    lines += ["", "## Motivi", ""]
    for kind, reasons in (j.get("reasons") or {}).items():
        for reason in reasons:
            lines.append(f"- {kind}: {reason}")
    if not any((j.get("reasons") or {}).values()):
        lines.append("- nessuno")
    after = (j.get("actions_after_restore") or {}).get("recorded") or []
    lines += ["", f"Azioni dopo il ripristino (registrate, non contate): {len(after)}."]
    path = os.path.join(result_dir, f"REPORT{suffix}.md")
    with open(path, "w") as h:
        h.write("\n".join(lines) + "\n")
    return path


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "inputs":
        print(json.dumps(inputs(args[0]), indent=1))
    elif cmd == "set":
        set_field(args[0], args[1], parse_value(args[2]))
    elif cmd == "set-file":
        set_field(args[0], args[1], _load(args[2]) if args[2].endswith(".json") else open(args[2]).read())
    elif cmd == "precondition":
        set_field(args[0], f"preconditions.{args[1]}", {"ok": args[2] == "true", "detail": args[3][:500]})
    elif cmd == "report":
        print(report(args[0], args[1] if len(args) > 1 else ""))
    elif cmd == "qreport":
        print(qreport(args[0], args[1] if len(args) > 1 else ""))
    elif cmd == "concurrent-runs":
        print(json.dumps(concurrent_runs(os.environ.get("S2_PROC_ROOT", "/proc"), int(args[0]))))
    else:
        print(f"unknown command {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
