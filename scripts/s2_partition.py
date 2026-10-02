#!/usr/bin/env python3
"""S2 partition of drone01's node (R11, docs/R11_S2_PARTITION.md, "Partizione").

DROP, not REJECT: in the node container's own network namespace, a dedicated
chain S2-PARTITION drops every packet from or to every other member of the k3d
network (server, the other agents, the load balancer), jumped to from the top
of INPUT, OUTPUT and FORWARD. Applied and removed from the host with
`docker exec` on the node container; the host's firewall is not touched.
Why this boundary covers the effective paths:
  - pod traffic to other nodes leaves as flannel VXLAN from the node's address
    (OUTPUT), Service traffic to the API is DNATed to the server's address
    before FORWARD, hostNetwork Pods and the kubelet use the node's address:
    all of it is to or from a peer address;
  - the rules sit before any ACCEPT of established traffic, so connections
    already open are cut too;
  - traffic between Pods of the node goes through its bridge, never to a peer
    address, and stays allowed (PX4, Agent, analytics, harness, observers).
A peer with an IPv6 address is refused: these rules are IPv4 only.
Traffic is identified by counting-only rules ahead of the drops (no target: a
packet is counted and goes on to the DROP rules), per peer and direction: VXLAN
(flannel, udp 8472 on both ends), the API (tcp 6443 on the peer), the discovery
server (udp 11811, hostNetwork on the server), the kubelet (tcp 10250 on the
node). They count what the cut drops, with known source and destination; they
decide nothing (the qualification's evidence, the same in every cell).
A positive counter proves traffic intercepted, not that the cut held: the hold is
read apart (decision after the fifth qualification) -- each hook's rules in order
(the jump must be the first: a jump below an ACCEPT is not the declared cut),
every second while the cut lasts, with an ICMP echo to every peer from the node
and TCP connects from one persistent process in drone01's harness, slots on a
monotonic 1 s grid, each with a 0.8 s deadline.
A guard on the host removes the chain after a maximum duration even if the
runner dies (the run is then interrupted). Probes from a container of the node
check the cut (a TCP connect that times out, not refused) and the restore.
Tested offline: operator/tests/test_s2_partition.py.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys

CHAIN = "S2-PARTITION"
HOOKS = ("INPUT", "OUTPUT", "FORWARD")
GUARD_MAX_SEC = 120
# (name out, name in, protocol, port, where the port is: on the peer, on the node, or on
# both). The discovery server does not send from 11811: the "in" counter of that port
# is named by its filter, not "discovery in" (decision 5 after the first qualification
# round) -- a raw count, not a measure of discovery traffic towards the node.
COUNTED = (("vxlan", "vxlan", "udp", 8472, "both"), ("api", "api", "tcp", 6443, "peer"),
           ("discovery", "udp-sport-11811", "udp", 11811, "peer"), ("kubelet", "kubelet", "tcp", 10250, "node"))
ACCOUNTING = re.compile(r'--comment "?s2 acct ([\w-]+ (?:in|out) [\d.]+)"?.*?-c (\d+) (\d+)')


class PartitionError(RuntimeError):
    pass


def peers(network, node):
    """The other members of the node's docker network: [{name, ipv4, ipv6}]."""
    out = []
    for container in (network.get("Containers") or {}).values():
        if container.get("Name") == node:
            continue
        out.append({"name": container.get("Name"),
                    "ipv4": (container.get("IPv4Address") or "").split("/")[0] or None,
                    "ipv6": (container.get("IPv6Address") or "").split("/")[0] or None})
    if not out:
        raise PartitionError(f"no peer of {node} in network {network.get('Name')}")
    ipv6 = [p["name"] for p in out if p["ipv6"]]
    if ipv6:
        raise PartitionError(f"peers with IPv6 addresses, not covered by these rules: {ipv6}")
    missing = [p["name"] for p in out if not p["ipv4"]]
    if missing:
        raise PartitionError(f"peers without an IPv4 address: {missing}")
    return sorted(out, key=lambda p: p["ipv4"])


def counting_rules(peer_list):
    lines = []
    for p in peer_list:
        ip = p["ipv4"]
        for name_out, name_in, proto, port, side in COUNTED:
            out_port = "--sport" if side == "node" else "--dport"      # towards the peer
            in_port = "--sport" if side == "peer" else "--dport"       # from the peer
            for direction, name, address, port_flag in (("out", name_out, f"-d {ip}", out_port),
                                                        ("in", name_in, f"-s {ip}", in_port)):
                lines.append(f'iptables -A {CHAIN} {address} -p {proto} -m {proto} {port_flag} {port} '
                             f'-m comment --comment "s2 acct {name} {direction} {ip}"')
    return lines


def apply_script(peer_list):
    """One shell script, run in the node container: chain, counting rules, drops,
    then the jumps."""
    lines = ["set -e", f"iptables -N {CHAIN}"] + counting_rules(peer_list)
    for p in peer_list:
        lines += [f"iptables -A {CHAIN} -s {p['ipv4']} -j DROP", f"iptables -A {CHAIN} -d {p['ipv4']} -j DROP"]
    lines += [f"iptables -I {hook} 1 -j {CHAIN}" for hook in HOOKS]
    return "\n".join(lines)


def remove_script():
    """Idempotent: every jump, then the chain; nothing left if run twice."""
    lines = [f"while iptables -C {hook} -j {CHAIN} 2>/dev/null; do iptables -D {hook} -j {CHAIN}; done"
             for hook in HOOKS]
    lines += [f"if iptables -L {CHAIN} -n >/dev/null 2>&1; then iptables -F {CHAIN}; iptables -X {CHAIN}; fi"]
    return "\n".join(lines)


def _hook_listings():
    """Each hook's rules in order, every line prefixed with its hook: the jump's
    position, and what sits above it if it is not the first."""
    return "; ".join(f'iptables -S {hook} 2>/dev/null | sed "s/^/hook {hook} /"' for hook in HOOKS)


def status_script():
    return (f"for h in {' '.join(HOOKS)}; do iptables -C $h -j {CHAIN} 2>/dev/null && echo jump $h; done; "
            f"{_hook_listings()}; "
            f"iptables -L {CHAIN} -v -n -x 2>/dev/null || echo no-chain; "
            f"echo ---acct; iptables -S {CHAIN} -v 2>/dev/null || true")


def _hook_line(fields, hooks):
    hook, rule = fields[1], fields[2:]
    entry = hooks.setdefault(hook, {"rules": 0, "positions": [], "above": []})
    if rule[:2] != ["-A", hook]:
        return                                          # the policy line
    entry["rules"] += 1
    if rule[2:] == ["-j", CHAIN]:
        entry["positions"].append(entry["rules"])
    elif not entry["positions"] and len(entry["above"]) < 5:
        entry["above"].append(" ".join(rule)[:160])


def parse_status(text):
    """{"jumps": [...], "chain": bool, "rules": [{"pkts", "bytes", "source", "destination"}] (the drops),
    "accounting": {"<service> <in|out> <peer>": {"pkts", "bytes"}},
    "hooks": {"<hook>": {"rules", "positions" (1-based, of the jumps), "above"}} (the hooks read),
    "chain_listed": the chain's own listing read}."""
    jumps, rules, chain, accounting, hooks, listed = [], [], True, {}, {}, False
    for line in text.splitlines():
        fields = line.split()
        if line.startswith("-A "):
            match = ACCOUNTING.search(line)
            if match:
                accounting[match.group(1)] = {"pkts": int(match.group(2)), "bytes": int(match.group(3))}
            continue
        if line.startswith("hook ") and len(fields) >= 3 and fields[1] in HOOKS:
            _hook_line(fields, hooks)
        elif line.startswith("jump "):
            jumps.append(fields[1])
        elif line.strip() == "no-chain":
            chain = False
        elif line.startswith(f"Chain {CHAIN} "):
            listed = True
        elif len(fields) >= 8 and fields[0].isdigit() and fields[2] == "DROP":
            addresses = [f for f in fields[3:] if f.count(".") == 3 or f.startswith("0.0.0.0")]
            if len(addresses) >= 2:
                rules.append({"pkts": int(fields[0]), "bytes": int(fields[1]),
                              "source": addresses[0], "destination": addresses[1]})
    return {"jumps": jumps, "chain": chain and bool(rules or jumps), "rules": rules, "accounting": accounting,
            "hooks": hooks, "chain_listed": listed}


def unreadable(status):
    """What a read missed -- a gap in the measurement, not a deviation: a hook not
    listed, or the chain's listing missing while a hook jumps to it (a jump to a
    chain that does not exist cannot be)."""
    hooks = status.get("hooks") or {}
    reasons = [f"{hook}: not read" for hook in HOOKS if hook not in hooks]
    if any(entry.get("positions") for entry in hooks.values()) and not status.get("chain_listed"):
        reasons.append("chain jumped to, its listing not read")
    return reasons


def conformity(status, peer_list):
    """The reasons the rules read are not the declared cut (empty: as declared):
    the jump the first rule of INPUT, OUTPUT and FORWARD -- ahead of any ACCEPT --,
    the chain, a DROP from and to every peer."""
    reasons = []
    hooks = status.get("hooks") or {}
    for hook in HOOKS:
        entry = hooks.get(hook)
        if entry is None:
            reasons.append(f"{hook}: not read")
        elif not entry.get("positions"):
            reasons.append(f"{hook}: no jump to {CHAIN}")
        elif entry["positions"][0] != 1:
            reasons.append(f"{hook}: jump at position {entry['positions'][0]}, below {entry.get('above')}")
    if not status.get("chain"):
        reasons.append("chain missing")
    rules = {(r["source"], r["destination"]) for r in status.get("rules") or []}
    for p in peer_list:
        for pair in ((p["ipv4"], "0.0.0.0/0"), ("0.0.0.0/0", p["ipv4"])):
            if pair not in rules:
                reasons.append(f"no DROP rule {pair[0]} -> {pair[1]} ({p['name']})")
    return reasons


# ---- the cut read every second (decision after the fifth qualification) ----
# One read of the rules, bracketed by the node's UTC, then an ICMP echo to every
# peer from the node's own namespace (INPUT/OUTPUT), in parallel, each with its
# send instant: a reply crossed the cut; "blocked" is the local DROP in OUTPUT
# (sendto refused), "timeout" no reply within the deadline.

def sample_script(peer_list, deadline_sec=1):
    ips = " ".join(p["ipv4"] for p in peer_list)
    return "\n".join([
        'echo "backend $(iptables -V 2>&1)"',        # nf_tables or legacy (a lock could make a read miss)
        'echo "at $(date +%s.%N)"',
        status_script(),
        'echo "end $(date +%s.%N)"',
        f'for ip in {ips}; do (s=$(date +%s.%N); o=$(ping -c 1 -W {int(deadline_sec)} $ip 2>&1); r=$?; '
        'if [ $r -eq 0 ]; then c=reply; else case "$o" in *"not permitted"*) c=blocked;; '
        '*" 0 packets received"*) c=timeout;; *) c=other;; esac; fi; echo "icmp $ip $s $r $c") & done; wait',
    ])


def _float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_sample(text):
    """parse_status's reading, plus "read_utc": [at, end] (the node's UTC around the
    rules read), "icmp": [{"target", "sent_utc", "rc", "result"}] and "backend"
    (iptables -V)."""
    out = parse_status(text)
    at = end = backend = None
    icmp = []
    for line in text.splitlines():
        fields = line.split()
        if fields[:1] == ["at"] and len(fields) == 2:
            at = _float(fields[1])
        elif fields[:1] == ["end"] and len(fields) == 2:
            end = _float(fields[1])
        elif fields[:1] == ["backend"]:
            backend = line[len("backend "):].strip()[:80]
        elif fields[:1] == ["icmp"] and len(fields) == 5:
            icmp.append({"target": f"icmp {fields[1]}", "sent_utc": _float(fields[2]),
                         "rc": int(fields[3]) if fields[3].lstrip("-").isdigit() else None, "result": fields[4]})
    out["read_utc"] = [at, end]
    out["icmp"] = icmp
    out["backend"] = backend
    return out


def guard_script(node, result_dir, max_sec=GUARD_MAX_SEC):
    """A detached host process: after max_sec, unless the run marked the restore,
    remove the partition and record that the guard fired."""
    restored, fired = f"{result_dir}/partition-restored", f"{result_dir}/guard-fired"
    return "\n".join([
        "#!/bin/sh",
        f"end=$(($(date +%s) + {int(max_sec)}))",
        f'while [ "$(date +%s)" -lt "$end" ]; do [ -e {shlex.quote(restored)} ] && exit 0; sleep 1; done',
        f"date -u +%s.%N > {shlex.quote(fired)}",
        f"docker exec {shlex.quote(node)} sh -c {shlex.quote(remove_script())}",
    ]) + "\n"


PROBE = """import socket,sys,time
out=[]
for target in sys.argv[1:]:
    h,p=target.rsplit(":",1); s=socket.socket(); s.settimeout(3); t=time.monotonic()
    try:
        s.connect((h,int(p))); r="connected"
    except socket.timeout: r="timeout"
    except ConnectionRefusedError: r="refused"
    except OSError as e: r="oserror:"+str(e).replace(" ","_")
    out.append({"target":target,"result":r,"elapsed":round(time.monotonic()-t,3)}); s.close()
print(__import__("json").dumps(out))"""


def probe_command(node, container_id, targets):
    """TCP connects from a container of the node (its network namespace, its routes)."""
    return ["docker", "exec", node, "crictl", "exec", container_id, "python3", "-c", PROBE, *targets]


def parse_probe(stdout):
    return {r["target"]: r for r in json.loads(stdout.strip().splitlines()[-1])}


# The TCP probe as ONE process in drone01's harness for the whole monitor (decision
# after the third round's cell 5: the start-up of a process per round entered the
# sampling). Each target on its own grid of monotonic deadlines t0 + k*period: a slot
# is sent only within late_max of its instant, its timeout bounded to end before the
# next slot (never two attempts of a target at once); a slot missed is recorded as
# skipped, never made up with a burst. Every slot recorded once: its planned instant,
# the actual send, the outcome. It ends at the stop file or after max_sec; a closed
# output ends it too.
PROBE_LOOP = """import json,os,socket,sys,threading,time
period,deadline,late_max,max_sec=(float(x) for x in sys.argv[1:5]); stop=sys.argv[5]; targets=sys.argv[6:]
lock=threading.Lock()
def emit(r):
    try:
        with lock:
            sys.stdout.write(json.dumps(r)+"\\n"); sys.stdout.flush()
    except (OSError,ValueError):
        os._exit(3)
t0=time.monotonic(); u0=time.time(); end=t0+max_sec
emit({"event":"start","t0_mono":t0,"t0_utc":u0,"period":period,"deadline":deadline,"late_max":late_max,
      "max_sec":max_sec,"targets":targets,"pid":os.getpid()})
def stopped():
    return os.path.exists(stop)
def wait_until(t):
    while not stopped():
        now=time.monotonic()
        if now>=t: return True
        time.sleep(min(0.05,t-now))
    return False
def run(target):
    h,p=target.rsplit(":",1); k=0
    while True:
        planned=t0+k*period
        if planned>=end or not wait_until(planned): return
        rec={"target":target,"slot":k,"planned_mono":planned,"planned_utc":u0+(planned-t0)}
        now=time.monotonic()
        if now-planned>late_max:
            rec.update(result="skipped-late",late_sec=round(now-planned,4))
        else:
            timeout=min(deadline,planned+period-now-0.05)
            s=socket.socket(); s.settimeout(timeout); u=time.time(); m=time.monotonic()
            try:
                s.connect((h,int(p))); r="connected"
            except socket.timeout: r="timeout"
            except ConnectionRefusedError: r="refused"
            except OSError as e: r="oserror:"+str(e).replace(" ","_")
            finally: s.close()
            rec.update(sent_mono=m,sent_utc=u,timeout=round(timeout,4),result=r,elapsed=round(time.monotonic()-m,4))
        emit(rec)
        k+=1
ts=[threading.Thread(target=run,args=(t,)) for t in targets]
for t in ts: t.start()
for t in ts: t.join()
emit({"event":"end","reason":"stop" if stopped() else "max_sec","mono":time.monotonic(),"utc":time.time()})"""


def probe_loop_command(node, container_id, targets, period, deadline, late_max, max_sec, stop_path):
    return ["docker", "exec", node, "crictl", "exec", container_id, "python3", "-S", "-c", PROBE_LOOP,
            str(period), str(deadline), str(late_max), str(max_sec), stop_path, *targets]


def _docker(*args, check=True):
    p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    if check and p.returncode != 0:
        raise PartitionError(f"docker {' '.join(args[:3])}: {p.stderr.strip()[:300]}")
    return p.stdout


def node_network(node):
    info = json.loads(_docker("inspect", node))[0]
    names = list((info.get("NetworkSettings") or {}).get("Networks") or {})
    if len(names) != 1:
        raise PartitionError(f"{node} is on {len(names)} networks: {names}")
    return json.loads(_docker("network", "inspect", names[0]))[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("peers", "apply", "remove", "status"):
        sub.add_parser(name).add_argument("--node", required=True)
    guard = sub.add_parser("guard")
    guard.add_argument("--node", required=True)
    guard.add_argument("--result-dir", required=True)
    guard.add_argument("--max-sec", type=int, default=GUARD_MAX_SEC)
    args = parser.parse_args(argv)
    if args.cmd == "guard":
        sys.stdout.write(guard_script(args.node, args.result_dir, args.max_sec))
        return 0
    if args.cmd == "remove":
        _docker("exec", args.node, "sh", "-c", remove_script())
        return 0
    if args.cmd == "status":
        print(json.dumps(parse_status(_docker("exec", args.node, "sh", "-c", status_script(), check=False))))
        return 0
    peer_list = peers(node_network(args.node), args.node)
    if args.cmd == "peers":
        print(json.dumps(peer_list))
        return 0
    _docker("exec", args.node, "sh", "-c", apply_script(peer_list))
    print(json.dumps(peer_list))
    return 0


if __name__ == "__main__":
    sys.exit(main())
