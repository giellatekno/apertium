#!/bin/sh
# Builds a language pairs archive for PAIRS_URL (see deploy/apy/start.sh).
#
#   deploy/pairs/build.sh OUT_DIR apertium-sme-nob [apertium-xxx-yyy ...]
#
# The packages are installed from the Apertium apt repo *inside the APy image*,
# so the pair data matches the image's tool versions. If a pair needs newer
# tools than the image has, apt fails (--no-upgrade): rebuild the image first.
#
# Writes OUT_DIR/pairs-<UTC timestamp>.tar.zst, which holds the pair packages'
# files under /usr/share/apertium (paths relative to it) plus
# pairs-manifest.txt with the pairs and the tool versions.
#
# Environment: IMAGE (default apertium-apy-llm), ENGINE (default podman).
set -eu

[ $# -ge 2 ] || { sed -n '2,4p' "$0" >&2; exit 2; }
out_dir=$(cd "$1" && pwd)
shift

name="pairs-$(date -u +%Y%m%dT%H%M%SZ).tar.zst"

"${ENGINE:-podman}" run --rm -i --user root --entrypoint sh \
    -v "$out_dir:/out:Z" "${IMAGE:-apertium-apy-llm}" -s "$name" "$@" <<'EOF'
set -eu
name=$1; shift
cd /usr/share/apertium

apt-get update -qq
dpkg-query -W -f '${Package}\n' | sort > /tmp/before
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends --no-upgrade "$@" >/dev/null
dpkg-query -W -f '${Package}\n' | sort > /tmp/after

# Files under /usr/share/apertium from every newly installed package: the
# requested pairs and any data packages they depend on. Tool packages pulled
# in only contribute files elsewhere, which the image doesn't need.
comm -13 /tmp/before /tmp/after > /tmp/new
xargs dpkg-query -L < /tmp/new | sed -n 's|^/usr/share/apertium/||p' | sort -u \
    | while read -r f; do [ -f "$f" ] && echo "$f"; done > /tmp/files

for pkg in "$@"; do
    grep -q "^$pkg\$" /tmp/new || { echo "build.sh: $pkg was already in the image?" >&2; exit 1; }
done
[ -s /tmp/files ] || { echo "build.sh: no files under /usr/share/apertium" >&2; exit 1; }

{
    for pkg in "$@"; do echo "pair $pkg $(dpkg-query -W -f '${Version}' "$pkg")"; done
    for tool in apertium lttoolbox cg3 hfst apertium-lex-tools apertium-separable apertium-recursive apertium-anaphora; do
        echo "tool $tool $(dpkg-query -W -f '${Version}' "$tool")"
    done
} > pairs-manifest.txt
echo pairs-manifest.txt >> /tmp/files

tar -cf - --owner=0 --group=0 -T /tmp/files | zstd -q -19 -T0 > "/out/$name"
printf '%s\n' "$@" > /tmp/requested
echo "build.sh: $(wc -l < /tmp/files) files." \
    "Also installed for the build (data under /usr/share/apertium included):" \
    "$(grep -vxF -f /tmp/requested /tmp/new | tr '\n' ' ')"
cat pairs-manifest.txt
EOF
echo "Wrote $out_dir/$name ($(du -h "$out_dir/$name" | cut -f1))"
