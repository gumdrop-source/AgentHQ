#!/usr/bin/env bash
# Phase 20 — initialise the systemd-creds vault
#
# Strategy: prefer TPM2-backed encryption; fall back to host-key.
# Either way the vault lives at /etc/agents/credentials/, owned root:root 0700.
# Individual creds are written via the `agenthq-cred` helper (TODO).
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
require_root

method_file=/etc/agents/credentials/.method

# Detect TPM2 — minimum firmware ≥ 7.86 to avoid CVE-2025-2884
tpm2_ok=0
if [[ -e /dev/tpm0 ]] && command -v tpm2_getcap >/dev/null; then
    if tpm2_getcap properties-fixed 2>/dev/null | grep -q TPM2_PT_MANUFACTURER; then
        # TODO: parse TPM2_PT_FIRMWARE_VERSION_1, gate on >= 7.86
        tpm2_ok=1
    fi
fi

if (( tpm2_ok )); then
    log "TPM2 detected — using TPM-backed systemd-creds"
    echo "tpm2" > "$method_file"
else
    log "No usable TPM2 — using host-key systemd-creds"
    echo "host" > "$method_file"
fi
chmod 0600 "$method_file"

# TODO: drop a smoke-test that encrypts + decrypts a sentinel value to prove the chain works
# TODO: install /usr/local/bin/agenthq-cred wrapper:
#   agenthq-cred set <name>      reads stdin → encrypts → writes /etc/agents/credentials/<name>.cred
#   agenthq-cred list            lists credential names (no values)
#   agenthq-cred remove <name>   deletes a cred
# Tools/units reference creds by name via systemd LoadCredentialEncrypted=

# Install agenthq-cred helper
log "Installing agenthq-cred → /usr/local/bin"
install -m 0755 "$(agenthq_root)/bin/agenthq-cred" /usr/local/bin/agenthq-cred

# Smoke-test: encrypt + decrypt a sentinel value, then remove it
log "Smoke-testing vault round-trip"
sentinel="agenthq-vault-smoketest-$(date +%s)"
echo "$sentinel" | /usr/local/bin/agenthq-cred set _smoketest >/dev/null
got="$(/usr/local/bin/agenthq-cred show _smoketest)"
[[ "$got" == "$sentinel" ]] || die "vault smoke-test failed (got: $got)"
/usr/local/bin/agenthq-cred remove _smoketest >/dev/null
log "Vault round-trip OK"

log "Phase 20 complete (vault initialised, no secrets stored — use agenthq-cred to inject)"
