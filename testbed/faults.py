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
"""
import datetime as dt
import tempfile
import json
import os
import re
import subprocess
import threading
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PKI = os.path.join(HERE, "k8s", "pki")

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


ACTIONS = {"mongo": _act_mongo, "cert": _act_cert}


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
            self._apply()
            self.rec["injected_ns"] = time.time_ns()
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
