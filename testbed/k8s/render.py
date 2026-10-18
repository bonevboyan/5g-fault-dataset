"""Render the testbed's Kubernetes manifests (namespace f5g + f5g-telemetry) as YAML text.

Parameters come from `tb up`: number of gNBs, run id, storage host, and whether in-NF tracing
is on (tracing=False unsets the OTLP endpoint, which makes the OTel SDK bootstrap a no-op; used
for the observer-overhead A/B). The rendered YAML is saved into the run directory, so every run
records exactly what was deployed.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
NS, TNS = "f5g", "f5g-telemetry"
CP_NFS = ["nrf", "amf", "ausf", "udm", "udr", "pcf", "nssf", "smf", "chf"]
PORTS = {"sbi": 8000, "metrics": 9091}


def doc(obj):
    return json.dumps(obj)            # JSON is valid YAML; one object per document


def headless(name, ports, ns=NS):
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns},
            "spec": {"clusterIP": "None", "publishNotReadyAddresses": True, "selector": {"app": name},
                     "ports": [{"name": n, "port": p, "protocol": proto} for n, p, proto in ports]}}


def deployment(name, pod_spec, labels=None, ns=NS):
    labels = {"app": name, **(labels or {})}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns,
                                                                         "labels": labels},
            "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": labels,
                                               "annotations": pod_spec.pop("annotations", {})},
                                  "spec": pod_spec}}}


POD_IP_ENV = {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}
SCRATCH = [{"name": "cert", "emptyDir": {}}, {"name": "config", "emptyDir": {}},
           {"name": "pki", "secret": {"secretName": "f5g-pki"}},
           {"name": "cfg", "configMap": {"name": "f5g-config"}}]
SCRATCH_MOUNTS = [{"name": "cert", "mountPath": "/free5gc/cert"},
                  {"name": "config", "mountPath": "/free5gc/config"},
                  {"name": "pki", "mountPath": "/pki", "readOnly": True},
                  {"name": "cfg", "mountPath": "/cfg", "readOnly": True}]


def sbi_env(name, tracing):
    env = [POD_IP_ENV, {"name": "GIN_MODE", "value": "release"}, {"name": "OTEL_SERVICE_NAME", "value": name},
           {"name": "SBI_TLS_CERT", "value": f"/free5gc/cert/{name}.pem"},
           {"name": "SBI_TLS_KEY", "value": f"/free5gc/cert/{name}.key"},
           {"name": "SBI_TLS_CA", "value": "/free5gc/cert/root.pem"}]
    if tracing:
        env += [{"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": f"otel-gateway.{TNS}:4317"},
                {"name": "OTEL_EXPORTER_OTLP_INSECURE", "value": "true"}]
    return env


def nf(name, tracing):
    args = "-u ./config/uerouting.yaml" if name == "smf" else ""
    extra = "cp /cfg/uerouting.yaml ./config/ && " if name == "smf" else ""
    # POD_IP is only known at start-up; the NRF needs a writable cert dir (it re-issues nrf.pem)
    cmd = (f"cp /pki/* ./cert/ && sed \"s/POD_IP/$POD_IP/g\" /cfg/{name}cfg.yaml > ./config/{name}cfg.yaml && "
           f"{extra}exec ./{name} -c ./config/{name}cfg.yaml {args}")
    env = sbi_env(name, tracing)
    # probe the plain-http metrics endpoint (same process): a TCP probe on the TLS SBI port makes
    # every NF log "TLS handshake error ... EOF" at ERRO level every period
    probe = {"httpGet": {"path": "/metrics", "port": PORTS["metrics"]}, "periodSeconds": 5, "failureThreshold": 3}
    spec = {"nodeSelector": {"role": "core"}, "volumes": SCRATCH,
            "annotations": {"f5g/scrape": "true", "f5g/metrics-port": str(PORTS["metrics"])},
            "containers": [{"name": name, "image": f"free5gc/{name}:otel2", "imagePullPolicy": "Never",
                            "workingDir": "/free5gc", "command": ["sh", "-c", cmd], "env": env,
                            "volumeMounts": SCRATCH_MOUNTS,
                            "readinessProbe": probe, "livenessProbe": {**probe, "initialDelaySeconds": 10}}]}
    ports = [("sbi", PORTS["sbi"], "TCP"), ("metrics", PORTS["metrics"], "TCP")]
    if name == "amf":
        ports.append(("ngap", 38412, "SCTP"))
    if name == "smf":
        ports.append(("pfcp", 8805, "UDP"))
    return [headless(name, ports), deployment(name, spec, {"tier": "core"})]


def upf():
    # the UPF resolves its own NodeID (`upf`) at start-up; wait until the headless Service's DNS
    # record for this pod exists, instead of crash-looping once on every fresh deploy
    cmd = ("until getent hosts upf >/dev/null; do sleep 0.5; done; "
           "cp /cfg/upf-iptables.sh ./upf-iptables.sh && sed \"s/POD_IP/$POD_IP/g\" /cfg/upfcfg.yaml > "
           "./config/upfcfg.yaml && bash ./upf-iptables.sh && exec ./upf -c ./config/upfcfg.yaml")
    spec = {"nodeSelector": {"role": "core"}, "volumes": SCRATCH,
            "containers": [{"name": "upf", "image": "free5gc/upf:v4.2.3", "imagePullPolicy": "Never",
                            "workingDir": "/free5gc", "command": ["bash", "-c", cmd], "env": [POD_IP_ENV],
                            "securityContext": {"privileged": True}, "volumeMounts": SCRATCH_MOUNTS},
                           # DN host just past N6: shares the UPF's netns, so it can originate
                           # downlink traffic to UE IPs (drives buffering + paging for idle UEs)
                           {"name": "dn", "image": "busybox:1.37", "imagePullPolicy": "Never",
                            "command": ["sleep", "infinity"]}]}
    return [headless("upf", [("pfcp", 8805, "UDP"), ("gtpu", 2152, "UDP")]),
            deployment("upf", spec, {"tier": "core"})]


def mongo():
    spec = {"nodeSelector": {"role": "core"}, "volumes": [{"name": "data", "emptyDir": {}}],
            "containers": [{"name": "mongo", "image": "mongo:4.4", "imagePullPolicy": "Never",
                            "args": ["--bind_ip_all"], "volumeMounts": [{"name": "data", "mountPath": "/data/db"}],
                            "readinessProbe": {"exec": {"command": ["mongo", "--quiet", "--eval",
                                                                    "db.adminCommand('ping').ok"]},
                                               "periodSeconds": 2}}]}
    return [headless("db", [("mongo", 27017, "TCP")]), deployment("db", spec, {"tier": "core"})]


def webui(tracing):
    cmd = ("cp /pki/* ./cert/ && sed \"s/POD_IP/$POD_IP/g\" /cfg/webuicfg.yaml > ./config/webuicfg.yaml && "
           "exec ./webui -c ./config/webuicfg.yaml")
    spec = {"nodeSelector": {"role": "core"}, "volumes": SCRATCH,
            "containers": [{"name": "webui", "image": "free5gc/webui:otel2", "imagePullPolicy": "Never",
                            "workingDir": "/free5gc", "command": ["sh", "-c", cmd],
                            "env": sbi_env("webui", tracing),   # an SBI client too (registers as AF)
                            "volumeMounts": SCRATCH_MOUNTS,
                            "readinessProbe": {"tcpSocket": {"port": 5000}, "periodSeconds": 2}}]}
    return [headless("webui", [("http", 5000, "TCP")]), deployment("webui", spec, {"tier": "core"})]


def gnb(i, gnbcfg, uecfg):
    """One UERANSIM pod per gNB; its UEs run as nr-ue processes in the same pod (RLS over lo)."""
    name = f"gnb-{i}"
    nci = f"0x{(i + 1) << 4:09x}"                     # 36-bit NCI; gNB id = NCI >> 4 (idLength 32)
    cfgmap = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{name}-cfg", "namespace": NS},
              "data": {"gnbcfg.yaml": gnbcfg.replace("NCI", nci).replace("TAC", str(i + 1)), "uecfg.yaml": uecfg}}
    # IPv6 off: the kernel's link-local chatter (RS/MLD) on a UE TUN is uplink data, which makes
    # an idle UE send a Service Request by itself within ~0.3 s of every release
    cmd = ("sysctl -qw net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1 && "
           "sed \"s/POD_IP/$POD_IP/g\" /cfg/gnbcfg.yaml > ./config/gnbcfg.yaml && cp /cfg/uecfg.yaml ./config/ "
           "&& exec ./nr-gnb -c ./config/gnbcfg.yaml")
    spec = {"nodeSelector": {"role": "ran"},
            "volumes": [{"name": "cfg", "configMap": {"name": f"{name}-cfg"}},
                        {"name": "config", "emptyDir": {}}, {"name": "tun", "hostPath": {"path": "/dev/net/tun"}}],
            "containers": [{"name": "ueransim", "image": "free5gc/ueransim:latest", "imagePullPolicy": "Never",
                            "workingDir": "/ueransim", "command": ["sh", "-c", cmd], "env": [POD_IP_ENV],
                            "securityContext": {"privileged": True},
                            "volumeMounts": [{"name": "cfg", "mountPath": "/cfg", "readOnly": True},
                                             {"name": "config", "mountPath": "/ueransim/config"},
                                             {"name": "tun", "mountPath": "/dev/net/tun"}]}]}
    return [cfgmap, deployment(name, spec, {"tier": "ran", "gnb": str(i)})]


def telemetry(run_id, store_host):
    """Collectors: a per-node agent (pod logs, kubelet cAdvisor) and one gateway (OTLP traces,
    K8s events, NF metrics, cluster state). Both push to the storage tier outside the cluster."""
    with open(os.path.join(HERE, "otel-agent.yaml")) as f:
        agent_cfg = f.read()
    with open(os.path.join(HERE, "otel-gateway.yaml")) as f:
        gw_cfg = f.read()
    env = [{"name": "RUN_ID", "value": run_id}, {"name": "STORE_HOST", "value": store_host},
           {"name": "NODE_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.hostIP"}}},
           {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}]
    sa = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "otel", "namespace": TNS}}
    role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "f5g-otel"},
            "rules": [{"apiGroups": [""], "resources": ["pods", "nodes", "nodes/stats", "nodes/metrics",
                                                         "nodes/proxy", "namespaces", "events", "services",
                                                         "endpoints", "replicationcontrollers",
                                                         "resourcequotas", "persistentvolumes",
                                                         "persistentvolumeclaims"],
                       "verbs": ["get", "list", "watch"]},
                      {"apiGroups": ["apps"], "resources": ["deployments", "replicasets", "daemonsets",
                                                            "statefulsets"], "verbs": ["get", "list", "watch"]},
                      {"apiGroups": ["batch"], "resources": ["jobs", "cronjobs"], "verbs": ["get", "list", "watch"]},
                      {"apiGroups": ["autoscaling"], "resources": ["horizontalpodautoscalers"],
                       "verbs": ["get", "list", "watch"]},
                      {"apiGroups": ["events.k8s.io"], "resources": ["events"], "verbs": ["get", "list", "watch"]},
                      {"nonResourceURLs": ["/metrics", "/metrics/cadvisor"], "verbs": ["get"]}]}
    bind = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
            "metadata": {"name": "f5g-otel"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "f5g-otel"},
            "subjects": [{"kind": "ServiceAccount", "name": "otel", "namespace": TNS}]}
    cms = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": n, "namespace": TNS},
            "data": {"config.yaml": c}} for n, c in (("otel-agent", agent_cfg), ("otel-gateway", gw_cfg))]
    col = lambda cm: {"name": "otelcol", "image": "otel/opentelemetry-collector-contrib:0.120.0",
                      "imagePullPolicy": "Never", "args": ["--config=/conf/config.yaml"], "env": env,
                      "volumeMounts": [{"name": "conf", "mountPath": "/conf"}]
                      + ([{"name": "pods", "mountPath": "/var/log/pods", "readOnly": True}] if cm == "otel-agent" else [])}
    agent = {"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"name": "otel-agent", "namespace": TNS},
             "spec": {"selector": {"matchLabels": {"app": "otel-agent"}},
                      "template": {"metadata": {"labels": {"app": "otel-agent"}},
                                   "spec": {"serviceAccountName": "otel",
                                            "tolerations": [{"operator": "Exists"}],
                                            "volumes": [{"name": "conf", "configMap": {"name": "otel-agent"}},
                                                        {"name": "pods", "hostPath": {"path": "/var/log/pods"}}],
                                            "containers": [{**col("otel-agent"),
                                                            "securityContext": {"runAsUser": 0}}]}}}}
    gw_spec = {"serviceAccountName": "otel", "nodeSelector": {"role": "telemetry"},
               "volumes": [{"name": "conf", "configMap": {"name": "otel-gateway"}}], "containers": [col("otel-gateway")]}
    gw = deployment("otel-gateway", gw_spec, ns=TNS)
    gw_svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "otel-gateway", "namespace": TNS},
              "spec": {"selector": {"app": "otel-gateway"}, "ports": [{"name": "otlp", "port": 4317}]}}
    nodeexp = deployment("node-exporter", {
        "nodeSelector": {"role": "telemetry"}, "hostPID": True,
        "annotations": {"f5g/scrape": "true", "f5g/metrics-port": "9100"},
        "volumes": [{"name": "root", "hostPath": {"path": "/"}}],
        "containers": [{"name": "node-exporter", "image": "prom/node-exporter:v1.8.2", "imagePullPolicy": "Never",
                        "args": ["--path.rootfs=/host"], "volumeMounts": [{"name": "root", "mountPath": "/host",
                                                                           "readOnly": True}]}]}, ns=TNS)
    return [sa, role, bind, *cms, agent, gw, gw_svc, nodeexp]


def render(gnbs, run_id, store_host, tracing=True):
    """{component: yaml}. Components are applied stage by stage by `tb up`."""
    with open(os.path.join(HERE, "gnbcfg.tmpl.yaml")) as f:
        gnbcfg = f.read()
    with open(os.path.join(HERE, "uecfg.tmpl.yaml")) as f:
        uecfg = f.read()
    y = lambda objs: "\n---\n".join(map(doc, objs))
    out = {"telemetry": y([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": TNS}},
                           *telemetry(run_id, store_host)]),
           "db": y(mongo()), "webui": y(webui(tracing)), "upf": y(upf())}
    for n in CP_NFS:
        out[n] = y(nf(n, tracing))
    for i in range(gnbs):
        out[f"gnb-{i}"] = y(gnb(i, gnbcfg, uecfg))
    return out
