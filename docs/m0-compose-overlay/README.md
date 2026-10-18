# M0 overlay for free5gc-compose (historical)

These files were added on top of a clone of https://github.com/free5gc/free5gc-compose at
commit `92a2202` during M0 (see `../M0-findings.md`). They are kept for the record; the
testbed used since M1b is `../../testbed/` (k3d), and `tb-compose` there is the M1 one.

- `docker-compose-tracing.yaml`: Jaeger + Beyla (Beyla shown unusable on kernel 7.0).
- `otel-instrument.sh`: per-NF eBPF auto-instrumentation sidecars (load, but cannot stitch h2c).
- `docker-compose-otel.yaml`: the first run of the native-OTel `free5gc/<nf>:otel` images.
- `scripts/up-clean.sh`, `scripts/provision-subscriber.sh`: scripted clean bring-up and
  subscriber provisioning, the precursor of `tb up`'s gates.
