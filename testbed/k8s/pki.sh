#!/usr/bin/env bash
# Test PKI for the SBI: one CA + one key/cert per NF (SANs = short service name + cluster FQDN).
# free5GC's NRF also signs OAuth tokens and verifies client certs with this CA (root.pem/key).
# Output: pki/ (gitignored). Idempotent: keeps an existing CA unless --force.
set -euo pipefail
cd "$(dirname "$0")"
NS=f5g
NFS="nrf amf ausf udm udr pcf nssf smf chf webui"
[[ "${1:-}" == "--force" ]] && rm -rf pki
mkdir -p pki
if [[ ! -f pki/root.key ]]; then
  openssl genrsa -out pki/root.key 2048 2>/dev/null
  openssl req -x509 -new -key pki/root.key -sha256 -days 3650 -out pki/root.pem \
    -subj "/O=f5g-testbed/CN=f5g SBI root CA" 2>/dev/null
fi
for nf in $NFS; do
  [[ -f pki/$nf.pem ]] && continue
  openssl genrsa -out pki/$nf.key 2048 2>/dev/null
  openssl req -new -key pki/$nf.key -out pki/$nf.csr -subj "/O=f5g-testbed/CN=${nf^^}" 2>/dev/null
  printf 'subjectAltName=DNS:%s,DNS:%s.%s.svc.cluster.local\nextendedKeyUsage=serverAuth,clientAuth\nkeyUsage=digitalSignature,keyEncipherment\n' \
    "$nf" "$nf" "$NS" > pki/$nf.ext
  openssl x509 -req -in pki/$nf.csr -CA pki/root.pem -CAkey pki/root.key -CAcreateserial \
    -out pki/$nf.pem -days 3650 -sha256 -extfile pki/$nf.ext 2>/dev/null
  rm pki/$nf.csr pki/$nf.ext
done
echo "PKI ready: $(ls pki | tr '\n' ' ')"
