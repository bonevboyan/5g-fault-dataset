"""UERANSIM UE control, shared by testbed/tb (bring-up gates) and workload/driver.py.

RAN layout: G gNBs, each a pod (k8s: app=gnb-<g> in namespace f5g) or the single compose
container `ueransim`, with its UEs as nr-ue processes inside it. UEs are assigned in contiguous
IMSI blocks: UE i belongs to gNB i // ues_per_gnb. `tb up` writes the layout to
testbed/.state/ran.json; without that file the single-container compose layout is assumed.

Each UE runs as its own nr-ue process. A shared `nr-ue -n N` process cannot re-register a single
UE (normal de-registration parks it in ATTEMPTING-REGISTRATION behind a back-off timer, and
switch-off does not stop its thread), so registration churn = switch the UE off, then start a
fresh process for that IMSI.
"""
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

IMSI_BASE = 208930000000001
SLICES = [(1, "010203"), (1, "112233")]   # uecfg.yaml opens one PDU session per slice
# UERANSIM v3.3.0 limits, per gNB pod/container (measured 2026-09-27): the gNB's RLS layer
# rejects UEs past ~1024 ("Max UE count reached"), and TUN names stop at uesimtun1023.
MAX_UES_PER_GNB = 1023
MAX_TUN = 1024

_LAYOUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "testbed", ".state", "ran.json")
_pods = {}


def layout():
    try:
        with open(_LAYOUT_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"backend": "compose", "gnbs": 1, "ues_per_gnb": MAX_UES_PER_GNB}


def imsi(i):
    return f"imsi-{IMSI_BASE + i}"


def index(ue):
    return int(ue.split("-")[1]) - IMSI_BASE


def gnb_of(ue):
    return index(ue) // layout()["ues_per_gnb"]


def gnbs():
    return list(range(layout()["gnbs"]))


def _target(g, detach=False):
    lay = layout()
    if lay["backend"] == "compose":
        return ["docker", "exec"] + (["-d"] if detach else []) + ["ueransim"]
    ns = lay.get("namespace", "f5g")
    if g not in _pods:
        out = subprocess.run(["kubectl", "get", "pods", "-n", ns, "-l", f"app=gnb-{g}",
                              "--field-selector=status.phase=Running",
                              "-o", "jsonpath={.items[0].metadata.name}"],
                             capture_output=True, text=True).stdout.strip()
        if not out:
            return None
        _pods[g] = out
    return ["kubectl", "exec", "-n", ns, _pods[g], "--"]


def dexec(*cmd, gnb=0):
    """Run cmd in gNB g's pod/container; returns stdout ("" if the pod is unreachable)."""
    tgt = _target(gnb)
    if tgt is None:
        return ""
    r = subprocess.run([*tgt, *cmd], capture_output=True, text=True)
    if r.returncode != 0 and gnb in _pods and "not found" in r.stderr:
        _pods.pop(gnb)   # pod replaced (restart / rescheduling): re-resolve next time
    return r.stdout


def spawn(script, gnb=0):
    """Start a shell script in the background inside gNB g's pod/container."""
    if layout()["backend"] == "compose":
        tgt = _target(gnb, detach=True)
        subprocess.run([*tgt, "sh", "-c", script], capture_output=True)
        return
    q = "'" + script.replace("'", "'\\''") + "'"
    dexec("sh", "-c", f"setsid sh -c {q} >/dev/null 2>&1 </dev/null &", gnb=gnb)


def _per_gnb(fn):
    with ThreadPoolExecutor(max_workers=max(1, len(gnbs()))) as ex:
        return dict(zip(gnbs(), ex.map(fn, gnbs())))


# UE output is IMSI-prefixed and written to the pod's stdout (PID 1 is nr-gnb), so UE/NAS logs
# reach the log pipeline like every other component's. [debug] lines are dropped at the source
# (F5G_UE_DEBUG=1 keeps them): at hundreds of UEs they dominate log volume. Kept: the NAS debug
# lines that mark a message sent or received, a few per procedure. They give the UE-side
# procedure boundaries that analysis/proc_timing.py times from (e.g. "Sending PDU Session
# Establishment Request", which has no info-level counterpart).
_NAS_MARKS = r"\[nas\] \[debug\] (Sending |Performing local release|Starting de-registration|[A-Za-z0-9 ]+ received$)"
# sed -u, not awk: the image's mawk reads its input in blocks, so a quiet UE's lines sat in the
# pipe until the process exited (lost at teardown)
_UE_FILTER = ("" if os.environ.get("F5G_UE_DEBUG") == "1"
              else f"| sed -u -E '/\\[debug\\]/{{/{_NAS_MARKS}/!d}}' ")


def start(ue):
    """Start (or restart) the nr-ue process for one IMSI in its gNB's pod.

    -r: no per-TUN policy routing. UERANSIM's routing setup races when UEs attach concurrently
    (two UEs grab the same uesimtunN, the loser leaves a stale route in rt_uesimtunN, and every
    later UE that reuses that lowest-free name fails). The driver pings with `ping -I <tun>`,
    which binds to the device and does not need those routes."""
    g = gnb_of(ue)
    dexec("sh", "-c", f"pkill -f -- '-i {ue}$' ; true", gnb=g)
    spawn(f"./nr-ue -r -c ./config/uecfg.yaml -i {ue} 2>&1 {_UE_FILTER}"
          f"| sed -u 's/^/[{ue}] /' > /proc/1/fd/1", gnb=g)


def stop(ue, switch_off=True):
    g = gnb_of(ue)
    if switch_off:
        dexec(*NR_CLI, ue, "-e", "deregister switch-off", gnb=g)
    dexec("sh", "-c", f"sleep 0.5; pkill -f -- '-i {ue}$' ; true", gnb=g)


def stop_all():
    _per_gnb(lambda g: dexec("sh", "-c", "pkill -f nr-ue ; true", gnb=g))


# nr-cli has no reply timeout: a request that reaches a UE process as it exits (a state poll
# racing a registration-churn restart) blocks forever, and with it the snapshot and every ping
# round queued behind the RAN-poll lock. Every in-pod nr-cli call is bounded.
NR_CLI = ["timeout", "5", "./nr-cli"]
_NR_CLI_SH = " ".join(NR_CLI)


def cli(ue, command):
    return dexec(*NR_CLI, ue, "-e", command, gnb=gnb_of(ue))


def gnb_cli(g, command):
    """Run an nr-cli command against gNB g itself (e.g. ue-list, ue-release <id>)."""
    name = dexec("sh", "-c", f"{_NR_CLI_SH} --dump | grep '^UERANSIM-gnb'", gnb=g).strip()
    return dexec(*NR_CLI, name, "-e", command, gnb=g) if name else ""


# Per-UE status + PDU sessions, one line per UE, queried 16 at a time inside the pod.
_STATE_ONE = r"""ue="$1"; st=$(timeout 5 ./nr-cli "$ue" -e status 2>/dev/null | tr '\n' ' ')
ps=$(timeout 5 ./nr-cli "$ue" -e ps-list 2>/dev/null | tr '\n' ' '); echo "$ue|$st|$ps" """
_STATE_SCRIPT = ("timeout 5 ./nr-cli --dump 2>/dev/null | grep '^imsi-' | "
                 "xargs -P 16 -I{} sh -c '" + _STATE_ONE.replace("'", "'\\''") + "' _ {}")

_SESSION = re.compile(r"PDU Session(\d+):\s+state:\s+(\S+).*?sd:\s+0x(\w+).*?address:\s+(\S+)")


def _states_of(g, script=_STATE_SCRIPT):
    out = {}
    for line in dexec("sh", "-c", script, gnb=g).splitlines():
        try:
            u, st, ps = line.split("|", 2)
        except ValueError:
            continue
        out[u] = {**_parse(st, ps), "gnb": g}
    return out


def states():
    """{imsi: {rm, mm, cm, sessions: [{id, state, sd, address}], pdu_sessions: <#active>, gnb}}"""
    merged = {}
    for part in _per_gnb(_states_of).values():
        merged.update(part)
    return merged


def state(ue):
    """State of one UE, or None if its nr-ue process is not up."""
    return _states_of(gnb_of(ue), _STATE_SCRIPT.replace("grep '^imsi-'", f"grep -x '{ue}'")).get(ue)


def _parse(st, ps):
    rm = re.search(r"rm-state:\s*(\S+)", st)
    mm = re.search(r"mm-state:\s*(\S+)", st)
    cm = re.search(r"cm-state:\s*(\S+)", st)
    # split per session so the lazy match can't run across sessions
    sessions = []
    for chunk in re.split(r"(?=PDU Session\d+:)", ps):
        m = _SESSION.search(chunk)
        if m:
            sessions.append({"id": int(m.group(1)), "state": m.group(2), "sd": m.group(3).lower(),
                             "address": None if m.group(4) == "null" else m.group(4)})
    return {"rm": rm.group(1) if rm else None, "mm": mm.group(1) if mm else None,
            "cm": cm.group(1) if cm else None, "sessions": sessions,
            "pdu_sessions": sum(s["state"] == "PS-ACTIVE" for s in sessions)}


def tun_by_address():
    """{ue_ip: (gnb, tun_ifname)} for every UE TUN interface across all gNB pods."""
    def one(g):
        m = {}
        for line in dexec("ip", "-o", "-4", "addr", "show", gnb=g).splitlines():
            f = line.split()
            if len(f) > 3 and f[1].startswith("uesimtun"):
                m[f[3].split("/")[0]] = (g, f[1])
        return m
    merged = {}
    for part in _per_gnb(one).values():
        merged.update(part)
    return merged


# ---------------------------------------------------------------- idle mode (CM-IDLE)
class NgapIndex:
    """IMSI -> current AMF-UE-NGAP-ID, from the AMF's log (every Gmm line carries
    `AU:<id>(3GPP)][supi:SUPI:<imsi>`). The gNB's `ue-list` only shows NGAP ids, so this is what
    links an IMSI to the gNB-side UE id that `ue-release` needs. Read incrementally."""
    _AU = re.compile(r"AU:(\d+)\(3GPP\)\]\[supi:SUPI:(imsi-\d+)\]")

    def __init__(self):
        self.au, self.since = {}, None

    def refresh(self):
        lay = layout()
        if lay["backend"] == "compose":
            args = ["docker", "logs", "--timestamps", "amf"] + (["--since", self.since] if self.since else [])
        else:
            args = ["kubectl", "logs", "-n", lay.get("namespace", "f5g"), "deploy/amf", "--timestamps"]
            args += ["--since-time", self.since] if self.since else ["--tail=-1"]
        r = subprocess.run(args, capture_output=True, text=True)
        for line in (r.stdout + r.stderr).splitlines():
            m = self._AU.search(line)
            if m:
                self.au[m.group(2)] = int(m.group(1))
            ts = line.split(" ", 1)[0]
            if ts[:2] == "20":
                self.since = ts


_UE_LIST = re.compile(r"ue-id: (\d+)\n\s+ran-ngap-id: \d+\n\s+amf-ngap-id: (\d+)")


def release_to_idle(ue, index):
    """Ask the UE's gNB to release its RAN context (N2 UE Context Release -> CM-IDLE), as a gNB
    inactivity timer would. Returns False if the UE's gNB-side id can't be resolved."""
    index.refresh()
    au = index.au.get(ue)
    g = gnb_of(ue)
    ids = {int(a): int(u) for u, a in _UE_LIST.findall(gnb_cli(g, "ue-list"))}
    if au is None or au not in ids:
        return False
    gnb_cli(g, f"ue-release {ids[au]}")
    return True


def dn_ping(ip, count=3, timeout=2):
    """Downlink data to a UE from the DN host at N6 (the `dn` sidecar in the UPF pod). For an idle
    UE this triggers UPF buffering, SMF N1N2MessageTransfer and AMF paging."""
    if layout()["backend"] == "compose":
        return ""
    r = subprocess.run(["kubectl", "exec", "-n", layout().get("namespace", "f5g"), "deploy/upf", "-c", "dn", "--",
                        "ping", "-c", str(count), "-W", str(timeout), ip], capture_output=True, text=True)
    return r.stdout


_dn = {"ip": None, "at": 0.0}


def dn_ip(ttl=15.0):
    """Address of the DN host for uplink probes. On k8s: the UPF pod IP (the `dn` sidecar shares
    the UPF's netns, so traffic leaves the GTP tunnel at N6 and is answered there). Re-resolved
    every `ttl` s, since a restarted or rescheduled UPF gets a new pod IP."""
    import time
    lay = layout()
    if lay["backend"] == "compose":
        return "10.100.200.1"
    if _dn["ip"] is None or time.monotonic() - _dn["at"] > ttl:
        ip = subprocess.run(["kubectl", "get", "pods", "-n", lay.get("namespace", "f5g"), "-l", "app=upf",
                             "--field-selector=status.phase=Running", "-o", "jsonpath={.items[0].status.podIP}"],
                            capture_output=True, text=True).stdout.strip()
        if ip:
            _dn.update(ip=ip, at=time.monotonic())
    return _dn["ip"]
