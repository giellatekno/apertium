#!/bin/sh
# Container entrypoint: optionally fetch the language pairs, then run APy.
#
# Environment (all optional):
#   PAIRS_URL         URL of a pairs archive (.tar.zst, see deploy/pairs/build.sh),
#                     unpacked into /usr/share/apertium before APy starts. On
#                     Cloudflare this is http://pairs.internal/current, served by
#                     the Worker from R2. Unset: use whatever is already in
#                     /usr/share/apertium (e.g. a host directory mounted there).
#   APY_LLM_ENDPOINT  Overrides `endpoint` in the [LLM] section of the config.
#   APY_CONFIG        Config file (default /etc/apertium-apy/apy.conf).
#
# Arguments are passed on to APy, after `-C <config>`.
set -eu

APERTIUM_DIR=/usr/share/apertium
config=${APY_CONFIG:-/etc/apertium-apy/apy.conf}

if [ -n "${PAIRS_URL:-}" ]; then
    echo "start.sh: fetching language pairs from $PAIRS_URL"
    start=$(date +%s)
    curl -fsS --retry 5 --retry-all-errors --retry-delay 1 "$PAIRS_URL" \
        | zstd -dc \
        | tar -x -C "$APERTIUM_DIR" --no-same-owner --no-same-permissions
    echo "start.sh: unpacked in $(($(date +%s) - start)) s: $(sed -n 's/^pair //p' "$APERTIUM_DIR/pairs-manifest.txt" | tr '\n' ' ')"

    # The compiled pair data must be readable by this image's tools. A
    # mismatch is usually harmless, but if a pair starts failing, rebuild the
    # image (or the archive) so that the versions match again.
    sed -n 's/^tool //p' "$APERTIUM_DIR/pairs-manifest.txt" | while read -r pkg built_with; do
        have=$(dpkg-query -W -f '${Version}' "$pkg" 2>/dev/null || echo missing)
        if [ "$have" != "$built_with" ]; then
            echo "start.sh: WARNING: pairs were built with $pkg $built_with, this image has $have"
        fi
    done
fi

if [ -n "${APY_LLM_ENDPOINT:-}" ]; then
    sed "s|^endpoint *=.*|endpoint = $APY_LLM_ENDPOINT|" "$config" > /tmp/apy.conf
    config=/tmp/apy.conf
fi

exec python3 /opt/apy/app/apy.py -C "$config" "$@"
