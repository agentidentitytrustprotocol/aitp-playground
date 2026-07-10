#!/usr/bin/env bash
# Generate a throwaway local CA and server certs for the Level 2 (TLS)
# federated stack. The CA root is trusted by the playground services (via
# SSL_CERT_FILE) so real did:web resolution over https validates the chain.
#
#   ./federated/gen-ca.sh
#   docker compose -f federated/docker-compose.federated-tls.yml up --build -d
#
# Certs land in federated/certs/ (git-ignored). Test-only — never reuse these
# keys anywhere real.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/certs"
mkdir -p "$DIR"
cd "$DIR"

DOMAINS=(org-a.aitp.test org-b.aitp.test)

echo "→ root CA"
openssl genrsa -out rootCA.key 4096 >/dev/null 2>&1
openssl req -x509 -new -nodes -key rootCA.key -sha256 -days 3650 \
  -subj "/CN=AITP Federated Test CA" -out rootCA.pem >/dev/null 2>&1

for d in "${DOMAINS[@]}"; do
  echo "→ cert for $d"
  openssl genrsa -out "$d.key" 2048 >/dev/null 2>&1
  openssl req -new -key "$d.key" -subj "/CN=$d" -out "$d.csr" >/dev/null 2>&1
  cat > "$d.ext" <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
subjectAltName = @alt_names
[alt_names]
DNS.1 = $d
EOF
  openssl x509 -req -in "$d.csr" -CA rootCA.pem -CAkey rootCA.key \
    -CAcreateserial -out "$d.crt" -days 3650 -sha256 -extfile "$d.ext" >/dev/null 2>&1
  rm -f "$d.csr" "$d.ext"
done

# Concatenate our CA onto the system bundle so services trust both public CAs
# (for anything else they reach) and our test CA.
if [ -f /etc/ssl/cert.pem ]; then
  cat /etc/ssl/cert.pem rootCA.pem > ca-bundle.pem
else
  cp rootCA.pem ca-bundle.pem
fi

echo "✓ wrote $(ls "$DIR" | tr '\n' ' ')"
