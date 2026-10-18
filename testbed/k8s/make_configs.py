#!/usr/bin/env python3
"""Derive the Kubernetes NF configs (k8s/config/) from the compose ones (../config/).

The compose configs address everything as <nf>.free5gc.org (container aliases). On k8s:
  * every NF binds to its own pod IP (placeholder POD_IP, filled in at container start);
  * NFs are addressed by their short headless-Service name (`amf`, `nrf`, ...). That is also
    the SAN in every SBI cert, including the one the NRF re-issues for itself (DNS:NRF);
  * the SBI is https (mTLS toward the NRF), while metrics stay plain http.
Kept as a transform, not a copy, so both testbeds share one source of truth for NF config.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "config")
DST = os.path.join(HERE, "config")
NFS = ["nrf", "amf", "ausf", "udm", "udr", "pcf", "nssf", "smf", "chf", "upf", "webui"]
HOST = re.compile(r"\b([a-z0-9]+)\.free5gc\.org\b")


def transform(text, nf, tls=True):
    out, block = [], None
    for line in text.splitlines():
        body = line.split("#", 1)[0]
        m = re.match(r"^  (\w+):\s*$", body)          # a top-level key under `configuration:`
        if m:
            block = m.group(1)
        key = body.strip().split(":", 1)[0].lstrip("- ")
        if key in ("bindingIPv4", "listenAddr", "externalAddr") and HOST.search(body):
            line = HOST.sub("POD_IP", line)
        elif key == "scheme" and block == "sbi" and tls:
            line = line.replace("http", "https", 1)
        elif key in ("nrfUri", "nrfId", "nrfAmfSet"):
            # nrfId/nrfAmfSet: the NSSF hands these NRF URIs to the AMF in NsiInformation, and the
            # AMF then discovers SMFs there. They only mattered once the NSSF knew our TAs.
            line = line.replace("http://", "https://" if tls else "http://")
        elif block == "ngapIpList" and HOST.search(body):
            line = HOST.sub("POD_IP", line)
        elif nf == "upf" and key == "addr" and HOST.search(body):   # UPF PFCP + GTP-U listen
            line = HOST.sub("POD_IP", line)
        elif nf == "chf" and key == "hostIPv4" and "chf.free5gc.org" in body:
            line = HOST.sub("POD_IP", line)
        elif nf == "webui" and key == "hostIPv4" and re.search(r":\s*webui\b", body):
            line = re.sub(r"(:\s*)webui\b", r"\1POD_IP", line)   # billing server binds its own IP
        line = HOST.sub(r"\1", line)                    # every remaining reference -> short name
        out.append(line)
    return "\n".join(out) + "\n"


def tracking_areas(text, nf, tacs):
    """One tracking area per gNB (TAC 1..tacs). The AMF must list every TAC a gNB serves (else NG
    Setup fails). The NSSF needs a taList entry per TAC for our PLMN and slices; upstream only
    has sample TAs for another PLMN, so every NSSelection logged "No TA ... in NSSF configuration".
    Paging then reaches only the gNB of the UE's own TA instead of every gNB."""
    if nf == "amf":
        head, rest = text.split("  supportTaiList:", 1)
        _, tail = rest.split("  plmnSupportList:", 1)
        tais = "".join(f"    - plmnId: {{ mcc: 208, mnc: 93 }}\n      tac: \"{t:06x}\"\n" for t in range(1, tacs + 1))
        return f"{head}  supportTaiList: # one TAI per gNB (make_configs.py --tacs)\n{tais}  plmnSupportList:{tail}"
    if nf == "nssf":
        head, tail = text.split("  taList:", 1)
        first, _, tail = tail.partition("\n")
        ours = "".join(
            f"    - tai: {{ plmnId: {{ mcc: 208, mnc: 93 }}, tac: \"{t:06x}\" }}   # gNB {t - 1} (make_configs.py --tacs)\n"
            f"      accessType: 3GPP_ACCESS\n"
            f"      supportedSnssaiList: [{{ sst: 1, sd: \"010203\" }}, {{ sst: 1, sd: \"112233\" }}]\n"
            for t in range(1, tacs + 1))
        return f"{head}  taList:{first}\n{ours}{tail}"
    return text


def main():
    tls = "--no-tls" not in sys.argv
    tacs = int(sys.argv[sys.argv.index("--tacs") + 1]) if "--tacs" in sys.argv else 1
    os.makedirs(DST, exist_ok=True)
    for nf in NFS:
        src = os.path.join(SRC, f"{nf}cfg.yaml")
        with open(src) as f:
            text = tracking_areas(transform(f.read(), nf, tls), nf, tacs)
        with open(os.path.join(DST, f"{nf}cfg.yaml"), "w") as f:
            f.write(text)
    for extra in ("uerouting.yaml", "upf-iptables.sh", "subscriber.json"):
        with open(os.path.join(SRC, extra)) as f, open(os.path.join(DST, extra), "w") as g:
            g.write(f.read())
    print(f"wrote {DST} (tls={tls}, tacs={tacs})")


if __name__ == "__main__":
    main()
