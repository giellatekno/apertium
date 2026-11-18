#!/bin/sh
# Container entrypoint: optionally fetch the language pairs, then run APy.
#
# Environment (all optional):
#   PAIRS             Language pairs to fetch, e.g. "sme-nob sme-smj". Each is
#                     fetched from $PAIRS_URL/<pair>.tar.zst (an archive from
#                     deploy/pairs/build.sh) and unpacked into /usr/share/apertium
#                     before APy starts. On Cloudflare PAIRS_URL is
#                     http://pairs.internal, served by the Worker from R2;
#                     locally it can be file:///some/dir. Unset: use whatever is
#                     already in /usr/share/apertium (e.g. a mounted host directory).
#   PAIRS_URL         Where to fetch PAIRS from (required with PAIRS).
#   APY_LLM_ENDPOINT  Overrides `endpoint` in the [LLM] section of the config.
#   APY_CONFIG        Config file (default /etc/apertium-apy/apy.conf).
#
# Arguments are passed on to APy, after `-C <config>`.
set -eu

APERTIUM_DIR=/usr/share/apertium
config=${APY_CONFIG:-/etc/apertium-apy/apy.conf}

if [ -n "${PAIRS:-}" ]; then
    : "${PAIRS_URL:?PAIRS_URL must be set when PAIRS is}"
    start=$(date +%s)
    for pair in $PAIRS; do
        curl -fsS --retry 5 --retry-all-errors --retry-delay 1 "$PAIRS_URL/$pair.tar.zst" \
            | zstd -dc \
            | tar -x -C "$APERTIUM_DIR" --no-same-owner --no-same-permissions
    done
    # Some pairs also install experimental modes that APy would list as extra
    # pairs: apertium-sme-nob ships sme-nob_rtx.mode (the same pipeline with
    # apertium-recursive in place of the chunk transfer, giving worse output),
    # which shows up as a target language "nob_rtx" in the frontend.
    rm -f "$APERTIUM_DIR"/modes/*_rtx.mode

    echo "start.sh: fetched and unpacked in $(($(date +%s) - start)) s:" \
        "$(cat "$APERTIUM_DIR"/.manifests/*.txt | sed -n 's/^pair //p' | tr '\n' ' ')"

    # The compiled pair data must be readable by this image's tools. A
    # mismatch is usually harmless, but if a pair starts failing, rebuild the
    # image so that the versions match again.
    for manifest in "$APERTIUM_DIR"/.manifests/*.txt; do
        pair=$(basename "$manifest" .txt)
        sed -n 's/^tool //p' "$manifest" | while read -r pkg built_with; do
            have=$(dpkg-query -W -f '${Version}' "$pkg" 2>/dev/null || echo missing)
            if [ "$have" != "$built_with" ]; then
                echo "start.sh: WARNING: $pair was packaged with $pkg $built_with, this image has $have"
            fi
        done
    done
fi

if [ -n "${APY_LLM_ENDPOINT:-}" ]; then
    sed "s|^endpoint *=.*|endpoint = $APY_LLM_ENDPOINT|" "$config" > /tmp/apy.conf
    config=/tmp/apy.conf
fi

exec python3 /opt/apy/app/apy.py -C "$config" "$@"
