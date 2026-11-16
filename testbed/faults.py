"""Fault injection for `tb run --fault <recipe.yaml>` and the ground-truth labels of a run.

A recipe (format: faults/README.md) is injected on a timer relative to the workload start, in a
background thread, while the workload driver runs. The thread records what it did and when, and
`write_labels` turns that into runs/<id>/labels.json (schema f5g-labels/1).

Two injectors:
  chaos   a Chaos Mesh object (level E, and network faults on the database), withdrawn by deleting it
  steps   a list of `apply` actions and the `revert` actions that undo them (level D):
            mongo: <js>       run in the testbed's MongoDB (database `free5gc`, also the NRF's registry)
            cert: <kind>      replace the target NF's SBI key/cert (kind: expired | untrusted) and
                              restart its pod; `cert: restore` puts the testbed PKI back (and restarts)
            storm: <spec>     start a burst of extra UERANSIM UEs that flood the AMF/AUSF/UDM with
                              registrations (level A); `storm_stop: <same spec>` kills them. The UEs
                              are placed round-robin on the running gNB pods at a high IMSI index
                              (default base 900000) so they never collide with the workload's UEs.
"""
import datetime as dt
import tempfile
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PKI = os.path.join(HERE, "k8s", "pki")
STORM_INDEX_BASE = 900000    # storm UEs use imsi index >= this: a textual prefix (imsi-2089300009)
                             # the workload's UEs never reach, so one pkill per pod hits only them
sys.path.insert(0, os.path.join(HERE, "..", "workload"))
import ue  # noqa: E402


def _storm_prefix(base):
    """IMSI prefix that matches the storm UEs (index >= base) but no workload UE (index from 0).
    The first digit where imsi(base) diverges from imsi(0) is a `9` the workload never reaches, so
    including it makes `pkill -f -i <prefix>` and log scrapes hit only storm UEs. (Computed, not
    `imsi(base)[:-len(str(base))]`, which stripped one digit too many and also matched workload UEs.)"""
    lo, hi = ue.imsi(0), ue.imsi(base)
    i = 0
    while i < len(hi) and i < len(lo) and hi[i] == lo[i]:
        i += 1
    return hi[:i + 1]

NS = "f5g"
SCHEMA = "f5g-labels/1"
CHAOS_API = "chaos-mesh.org/v1alpha1"
REQUIRED = ("id", "level", "class", "title", "target", "schedule", "inject")


def load(path):
    with open(path) as f:
        r = yaml.safe_load(f)
    missing = [k for k in REQUIRED if k not in r]
    if missing:
        raise ValueError(f"{path}: recipe is missing {', '.join(missing)}")
    if os.path.splitext(os.path.basename(path))[0] != r["id"]:
        raise ValueError(f"{path}: file name must equal the recipe id ({r['id']})")
    if len(r["inject"]) != 1 or next(iter(r["inject"])) not in ("chaos", "steps"):
        raise ValueError(f"{path}: inject must have exactly one of chaos, steps (got {sorted(r['inject'])})")
    if "steps" in r["inject"]:
        st = r["inject"]["steps"]
        if not st.get("apply") or "revert" not in st:
            raise ValueError(f"{path}: steps needs apply and revert")
        for step in st["apply"] + st["revert"]:
            if len(step) != 1 or next(iter(step)) not in ACTIONS:
                raise ValueError(f"{path}: unknown step {step} (supported: {', '.join(ACTIONS)})")
    r.setdefault("magnitude", {})
    r.setdefault("expected", {})
    r["schedule"].setdefault("duration_s", 0)
    r["schedule"].setdefault("observe_s", 120)
    return r


def _kc(*args, input=None, check=True):
    p = subprocess.run(["kubectl", *args], input=input, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {p.stderr.strip()[:300]}")
    return p.stdout


def _pods(nf):
    """[(name, uid, node, ready)] of the target's pods."""
    out = json.loads(_kc("get", "pods", "-n", NS, "-l", f"app={nf}", "-o", "json"))
    res = []
    for p in out["items"]:
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))
        if p["metadata"].get("deletionTimestamp"):
            ready = False
        res.append((p["metadata"]["name"], p["metadata"]["uid"], p["spec"].get("nodeName"), ready))
    return res


def _mongo(js):
    return _kc("exec", "-n", NS, "deploy/db", "--", "mongo", "free5gc", "--quiet", "--eval", js).strip()


def _cert_files(nf, kind, out):
    """A key/cert for `nf` with the testbed's SANs that peers must reject: `expired` is signed by
    the testbed CA but expired yesterday; `untrusted` is valid but signed by a CA nobody trusts."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = dt.datetime.now(dt.timezone.utc)
    name = lambda cn: x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "f5g-testbed"),  # noqa: E731
                                 x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if kind == "expired":
        with open(os.path.join(PKI, "root.pem"), "rb") as f:
            issuer = x509.load_pem_x509_certificate(f.read()).subject
        with open(os.path.join(PKI, "root.key"), "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), None)
        nb, na = now - dt.timedelta(days=30), now - dt.timedelta(days=1)
    elif kind == "untrusted":
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        issuer = name("rogue root CA")
        nb, na = now - dt.timedelta(days=1), now + dt.timedelta(days=365)
    else:
        raise ValueError(f"unknown cert kind {kind}")
    cert = (x509.CertificateBuilder().subject_name(name(nf.upper())).issuer_name(issuer)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(nb).not_valid_after(na)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(nf),
                                                        x509.DNSName(f"{nf}.{NS}.svc.cluster.local")]), False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                  ExtendedKeyUsageOID.CLIENT_AUTH]), False)
            .sign(ca_key, hashes.SHA256()))
    with open(os.path.join(out, f"{nf}.pem"), "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(out, f"{nf}.key"), "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    return {"subject": nf.upper(), "issuer": issuer.rfc4514_string(), "not_before": nb.isoformat(),
            "not_after": na.isoformat()}


def _restart(nf):
    _kc("delete", "pods", "-n", NS, "-l", f"app={nf}", "--wait=false")


def _act_mongo(nf, js):
    return {"output": _mongo(js)[-500:]}


def _act_cert(nf, kind):
    secret = f"f5g-pki-override-{nf}"
    if kind == "restore":
        _kc("delete", "secret", secret, "-n", NS, "--ignore-not-found")
        _restart(nf)
        return {"restarted": True}
    with tempfile.TemporaryDirectory() as d:
        info = _cert_files(nf, kind, d)
        _kc("create", "secret", "generic", secret, "-n", NS, f"--from-file={d}")
    _restart(nf)   # the NF reads its cert at start-up; a new pod mounts the override
    return {"restarted": True, "cert": info}


def _storm_plan(arg):
    """Deterministic (gnb, imsi) placement, recomputed identically by apply and by storm_stop."""
    gnbs = ue.gnbs()
    base = arg.get("index_base", STORM_INDEX_BASE)
    return [(gnbs[k % len(gnbs)], ue.imsi(base + k)) for k in range(arg["count"])]


def _act_storm(nf, arg):
    """Start `count` extra UEs across the running gNB pods. mode `auth_reject` (default) uses
    unprovisioned IMSIs that fail authentication; mode `valid` provisions them first so they
    register and set up sessions."""
    mode, tempo = arg.get("mode", "auth_reject"), arg.get("tempo_ms", 0)
    plan = _storm_plan(arg)
    if mode == "valid":
        _provision([u for _, u in plan])
    by_g = {}
    for g, u in plan:
        by_g.setdefault(g, []).append(u)

    def one(g):
        if tempo:
            for u in by_g[g]:
                ue.start(u, g)
                time.sleep(tempo / 1000)
        else:
            ue.start_many(g, by_g[g])   # whole burst in one exec
    ts = [threading.Thread(target=one, args=(g,)) for g in by_g]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return {"started": len(plan), "gnbs": len(by_g), "mode": mode}


def _act_storm_stop(nf, arg):
    """Kill every storm nr-ue in one pkill per pod (matched by the high-index IMSI prefix), and,
    for a `valid` storm, delete the subscribers it provisioned."""
    base = arg.get("index_base", STORM_INDEX_BASE)
    prefix = _storm_prefix(base)
    for g in ue.gnbs():
        ue.dexec("sh", "-c", f"pkill -f -- '-i {prefix}' ; true", gnb=g)
    if arg.get("mode") == "valid":
        _deprovision([u for _, u in _storm_plan(arg)])
    return {"restarted": False}


def _deprovision(imsis):
    """Delete the subscriber records a valid/paging storm provisioned (every webconsole collection)."""
    ids = ",".join(f'"{u}"' for u in imsis)
    _mongo("var ids=[%s];"
           "['subscriptionData.authenticationData.authenticationSubscription',"
           "'subscriptionData.identityData','subscriptionData.provisionedData.amData',"
           "'subscriptionData.provisionedData.smData',"
           "'subscriptionData.provisionedData.smfSelectionSubscriptionData',"
           "'policyData.ues.amData','policyData.ues.smData']"
           ".forEach(function(c){db.getCollection(c).deleteMany({ueId:{$in:ids}});});"
           "print('deprovisioned '+ids.length);" % ids)


# ---- paging storm (level A): a burst of idle UEs paged in a loop ----------------------------
_PAGING = {}   # index_base -> {"stop": Event, "thread": Thread}; links apply to its loop for revert
_TUN_UP = re.compile(r"\[(imsi-\d+)\].*?uesimtun\d+,\s*([\d.]+)\]")


def _storm_imsi_ips(prefix):
    """{imsi: ip} for the storm UEs, scraped from the gNB pods' logs (UERANSIM's `TUN interface
    [uesimtunN, <ip>] is up` line, which we prefix with `[imsi-...]`). Only storm UEs match the
    high-index prefix, so the workload's UEs are never paged."""
    out = {}
    for g in ue.gnbs():
        r = subprocess.run(["kubectl", "logs", "-n", NS, "-l", f"app=gnb-{g}", "--tail=-1"],
                           capture_output=True, text=True)
        for imsi, ip in _TUN_UP.findall(r.stdout + r.stderr):
            if imsi.startswith(prefix):
                out[imsi] = ip
    return out


def _act_paging_storm(nf, arg):
    """Provision and start `count` valid UEs, wait for their PDU sessions, then run a background
    loop that releases each to CM-IDLE and sends it downlink data, forcing the AMF to page it. This
    floods the paging path (AMF paging, SMF N1N2MessageTransfer, UPF buffering) and the UEs' service
    requests -- a different control-plane path from the registration storms."""
    base = arg.get("index_base", STORM_INDEX_BASE)
    tempo = arg.get("tempo_ms", 0)
    plan = _storm_plan(arg)
    _provision([u for _, u in plan])
    by_g = {}
    for g, u in plan:
        by_g.setdefault(g, []).append(u)
    for g in by_g:
        ue.start_many(g, by_g[g])

    prefix = _storm_prefix(base)
    imsi_ip = {}
    for _ in range(30):                      # let the burst register and bring TUNs up
        time.sleep(1.0)
        imsi_ip = _storm_imsi_ips(prefix)
        if len(imsi_ip) >= 0.8 * len(plan):
            break

    stop = threading.Event()

    def loop():
        idx = ue.NgapIndex()
        while not stop.is_set():
            for imsi, ip in list(imsi_ip.items()):
                if stop.is_set():
                    break
                ue.release_to_idle(imsi, idx)    # -> CM-IDLE (False if not resolvable yet; skipped)
                ue.dn_ping(ip, count=1, timeout=1)   # downlink to the idle UE -> paging
                if tempo:
                    stop.wait(tempo / 1000)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    _PAGING[base] = {"stop": stop, "thread": t}
    return {"started": len(plan), "paged": len(imsi_ip), "mode": "paging"}


def _act_paging_storm_stop(nf, arg):
    """Stop the paging loop, kill the storm UEs, and delete their subscribers."""
    base = arg.get("index_base", STORM_INDEX_BASE)
    st = _PAGING.pop(base, None)
    if st:
        st["stop"].set()
        st["thread"].join(timeout=10)
    prefix = _storm_prefix(base)
    for g in ue.gnbs():
        ue.dexec("sh", "-c", f"pkill -f -- '-i {prefix}' ; true", gnb=g)
    _deprovision([u for _, u in _storm_plan(arg)])
    return {"restarted": False}


# ---- subscriber provisioning (level-A valid storm): the webconsole REST API, like `tb` ----
class _PortForward:
    def __init__(self, port=15050):
        self.port = port

    def __enter__(self):
        self.p = subprocess.Popen(["kubectl", "port-forward", "-n", NS, "deploy/webui", f"{self.port}:5000"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            if _webui_token(self.port):
                return self
            time.sleep(0.5)
        raise RuntimeError("webui port-forward did not come up")

    def __exit__(self, *exc):
        self.p.terminate()
        self.p.wait()


def _webui_token(port):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/login", method="POST",
                                     data=json.dumps({"username": "admin", "password": "free5gc"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r).get("access_token")
    except Exception:  # noqa: BLE001
        return None


def _provision(imsis):
    plmn = "20893"
    tmpl = json.load(open(os.path.join(HERE, "config", "subscriber.json")))
    with _PortForward() as pf:
        tok = _webui_token(pf.port)
        for u in imsis:
            idx = ue.index(u)
            body = json.loads(json.dumps(tmpl))
            body["ueId"] = u
            body["AuthenticationSubscription"]["sequenceNumber"] = "000000000023"
            body["AccessAndMobilitySubscriptionData"]["gpsis"] = [f"msisdn-09{idx:08d}"]
            req = urllib.request.Request(f"http://127.0.0.1:{pf.port}/api/subscriber/{u}/{plmn}", method="POST",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Token": tok})
            urllib.request.urlopen(req, timeout=10).close()


ACTIONS = {"mongo": _act_mongo, "cert": _act_cert, "storm": _act_storm, "storm_stop": _act_storm_stop,
           "paging_storm": _act_paging_storm, "paging_storm_stop": _act_paging_storm_stop}


def chaos_object(recipe, run_id):
    if "chaos" not in recipe["inject"]:
        return None
    c = recipe["inject"]["chaos"]
    name = f"{recipe['id'].lower()}-{run_id}".replace("_", "-")[:63].rstrip("-")
    spec = dict(c["spec"])
    spec["selector"] = {"namespaces": [NS], "labelSelectors": {"app": recipe["target"]["nf"]}}
    dur = recipe["schedule"]["duration_s"]
    if dur:   # Chaos Mesh also withdraws it by itself, in case `tb` dies before it deletes the object
        spec.setdefault("duration", f"{dur}s")
    return {"apiVersion": CHAOS_API, "kind": c["kind"],
            "metadata": {"name": name, "namespace": NS, "labels": {"f5g/run": run_id, "f5g/fault": recipe["id"]}},
            "spec": spec}


class Injection(threading.Thread):
    """Waits until `t0 + onset_s`, injects, holds or observes, withdraws; never raises."""

    def __init__(self, recipe, run_id, t0_ns, log):
        super().__init__(daemon=True)
        self.r, self.run_id, self.t0_ns, self.log = recipe, run_id, t0_ns, log
        self.obj = chaos_object(recipe, run_id)
        self.steps = recipe["inject"].get("steps")
        self.rec = {"injected_ns": None, "removed_ns": None, "recovered_ns": None, "pods": [], "error": None,
                    "events": [], "steps": []}
        self._applied = self._reverted = False
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        s = self.r["schedule"]
        nf = self.r["target"]["nf"]
        try:
            if self._cancel.wait(max(0.0, self.t0_ns / 1e9 + s["onset_s"] - time.time())):
                self.rec["error"] = "cancelled before onset (workload ended early)"
                return
            before = _pods(nf)
            self.rec["pods"] = [{"name": n, "uid": u, "node": node} for n, u, node, _ in before]
            self.rec["injected_ns"] = time.time_ns()
            self._apply()
            self.log(f"fault {self.r['id']}: injected ("
                     + (f"{self.obj['kind']} {self.obj['metadata']['name']})" if self.obj
                        else f"{len(self.steps['apply'])} step(s))"))
            if self.obj:
                self._await_events(10)
            must_restart = self.obj is not None and self.obj["kind"] == "PodChaos" \
                and self.obj["spec"].get("action") == "pod-kill"
            if s["duration_s"]:
                self._cancel.wait(s["duration_s"])
                old = {u for _, u, _, _ in _pods(nf)}
                restarted = self._withdraw()
                self.rec["removed_ns"] = time.time_ns()
                self.log(f"fault {self.r['id']}: removed after {s['duration_s']}s")
                if restarted:   # the revert itself replaced the target's pods: wait for the new one
                    must_restart, before = True, [(None, u, None, None) for u in old]
            else:
                self.rec["removed_ns"] = self.rec["injected_ns"]
            self._await_recovery(nf, {u for _, u, _, _ in before} if must_restart else None, s["observe_s"])
        except Exception as e:  # noqa: BLE001 — a failed injection is recorded, the run goes on
            self.rec["error"] = str(e)
            self.log(f"fault {self.r['id']}: FAILED: {e}")
        finally:
            try:
                self._withdraw()
            except Exception as e:  # noqa: BLE001
                self.rec["error"] = self.rec["error"] or f"revert failed: {e}"
                self.log(f"fault {self.r['id']}: REVERT FAILED: {e}")

    def _apply(self):
        self._applied = True
        if self.obj:
            _kc("apply", "-f", "-", input=json.dumps(self.obj))
        else:
            self._run_steps("apply")

    def _withdraw(self):
        """Undo the fault once; True if that restarted the target's pods."""
        if self.obj:
            self._delete()
            return False
        if not self._applied or self._reverted:
            return False
        self._reverted = True
        return any(r.get("restarted") for r in self._run_steps("revert"))

    def _run_steps(self, phase):
        out = []
        for step in self.steps[phase]:
            (action, arg), = step.items()
            res = ACTIONS[action](self.r["target"]["nf"], arg)
            out.append(res)
            self.rec["steps"].append({"phase": phase, "action": action, "ts_ns": time.time_ns(), **res})
        return out

    def _await_recovery(self, nf, old_uids, timeout):
        """Recovered = a Ready target pod exists again (after a kill or restart: a new pod) and it
        is working."""
        deadline = time.time() + timeout
        while time.time() < deadline and not self._cancel.is_set():
            pods = _pods(nf)
            fresh = [n for n, uid, _, ready in pods if ready and (old_uids is None or uid not in old_uids)]
            if fresh and self._working(fresh[0]):
                self.rec["recovered_ns"] = time.time_ns()
                self.rec["pods_after"] = [{"name": n, "uid": u, "node": node} for n, u, node, _ in pods]
                self.log(f"fault {self.r['id']}: target Ready again after "
                         f"{(self.rec['recovered_ns'] - self.rec['removed_ns']) / 1e9:.1f}s")
                return
            time.sleep(0.5)
        self.log(f"fault {self.r['id']}: target not recovered within {timeout}s")

    def _working(self, pod):
        """The recipe's `recovery_log` regex (e.g. the SMF's PFCP re-association) has appeared in
        the pod's log; without one, pod Ready is taken as recovered."""
        pattern = self.r["schedule"].get("recovery_log")
        if not pattern:
            return True
        out = _kc("logs", "-n", NS, pod, "--tail=-1", check=False)
        return re.search(pattern, out) is not None

    def _await_events(self, timeout):
        """Chaos Mesh's records exist only while the fault is active (it clears them on recovery),
        so read them right after injection; a fault with no record is flagged."""
        deadline = time.time() + timeout
        while time.time() < deadline and not self.rec["events"]:
            self._collect_events()
            if not self.rec["events"]:
                time.sleep(0.5)
        if not self.rec["events"]:
            self.log(f"fault {self.r['id']}: no Chaos Mesh record of the injection within {timeout}s")

    def _collect_events(self):
        """Chaos Mesh's own record of what it did to which container (best effort)."""
        out = _kc("get", self.obj["kind"].lower(), self.obj["metadata"]["name"], "-n", NS, "-o", "json", check=False)
        try:
            st = json.loads(out).get("status", {})
        except ValueError:
            return
        events = [{"target": c.get("id"), **ev}
                  for c in st.get("experiment", {}).get("containerRecords", []) or []
                  for ev in c.get("events", []) or []]
        if events:
            self.rec["events"] = events

    def _delete(self):
        if not self.obj:
            return
        _kc("delete", self.obj["kind"].lower(), self.obj["metadata"]["name"], "-n", NS,
            "--ignore-not-found", "--wait=false", check=False)


def write_labels(run_dir, run_id, workload_start_ns, workload_end_ns, recipe=None, injection=None):
    fault = None
    if recipe is not None:
        rec = injection.rec if injection else {}
        fault = {k: recipe[k] for k in ("id", "level", "class", "title", "magnitude", "schedule", "expected")}
        fault["target"] = {"nf": recipe["target"]["nf"], "pods": rec.get("pods", []),
                           "pods_after": rec.get("pods_after", [])}
        fault.update({k: rec.get(k) for k in ("injected_ns", "removed_ns", "recovered_ns")})
        if "chaos" in recipe["inject"]:
            fault["injector"] = {"kind": "chaos-mesh", "object": injection.obj if injection else None,
                                 "events": rec.get("events", []), "error": rec.get("error")}
        else:
            fault["injector"] = {"kind": "steps", "steps": recipe["inject"]["steps"],
                                 "log": rec.get("steps", []), "error": rec.get("error")}
    labels = {"schema": SCHEMA, "run_id": run_id,
              "workload": {"start_ns": workload_start_ns, "end_ns": workload_end_ns}, "fault": fault}
    with open(os.path.join(run_dir, "labels.json"), "w") as f:
        json.dump(labels, f, indent=2)
    return labels
