#!/bin/sh
# Builds the archive of one language pair, for start.sh (PAIRS/PAIRS_URL).
#
#   deploy/pairs/build.sh OUT_DIR sme-nob
#
# Installs apertium-<pair> from the Apertium apt repo *inside the APy image*,
# so the data matches the image's tool versions. If the pair needs newer tools
# than the image has, apt fails (--no-upgrade): rebuild the image first.
#
# Writes OUT_DIR/<package version>.tar.zst: the files under /usr/share/apertium
# (paths relative to it) of the pair package and any data packages it depends
# on, plus .manifests/<pair>.txt with the pair's version and the tool versions.
# That manifest is also written next to it as <package version>.manifest.txt.
# The last line of output is the archive's path.
# deploy/cloudflare/manage.py runs this when deploying.
#
# Environment: IMAGE (default apertium-apy-llm), ENGINE (default podman).
set -eu

[ $# -eq 2 ] || { sed -n '2,4p' "$0" >&2; exit 2; }
out_dir=$(cd "$1" && pwd)
pair=$2
case $pair in
    [a-z][a-z][a-z]-[a-z][a-z][a-z]) ;;
    *) echo "build.sh: not a pair name like sme-nob: $pair" >&2; exit 2 ;;
esac

"${ENGINE:-podman}" run --rm -i --user root --entrypoint sh \
    -v "$out_dir:/out:Z" "${IMAGE:-apertium-apy-llm}" -s "$pair" <<'EOF' >&2
set -eu
pair=$1
pkg=apertium-$pair
cd /usr/share/apertium

apt-get update -qq
dpkg-query -W -f '${Package}\n' | sort > /tmp/before
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends --no-upgrade "$pkg" >/dev/null
dpkg-query -W -f '${Package}\n' | sort > /tmp/after

# Files under /usr/share/apertium from every newly installed package: the pair
# and any data packages it depends on. Tool packages pulled in only
# contribute files elsewhere, which the image doesn't need.
comm -13 /tmp/before /tmp/after > /tmp/new
grep -qx "$pkg" /tmp/new || { echo "build.sh: $pkg was already in the image?" >&2; exit 1; }
xargs dpkg-query -L < /tmp/new | sed -n 's|^/usr/share/apertium/||p' | sort -u \
    | while read -r f; do [ -f "$f" ] && echo "$f"; done > /tmp/files
[ -s /tmp/files ] || { echo "build.sh: no files under /usr/share/apertium" >&2; exit 1; }

version=$(dpkg-query -W -f '${Version}' "$pkg")
mkdir -p .manifests
{
    echo "pair $pair $version"
    for tool in apertium lttoolbox cg3 hfst apertium-lex-tools apertium-separable apertium-recursive apertium-anaphora; do
        echo "tool $tool $(dpkg-query -W -f '${Version}' "$tool")"
    done
} > ".manifests/$pair.txt"
echo ".manifests/$pair.txt" >> /tmp/files

tar -cf - --owner=0 --group=0 -T /tmp/files | zstd -q -19 -T0 > "/out/$version.tar.zst"
cp ".manifests/$pair.txt" "/out/$version.manifest.txt"
echo "build.sh: $pair $version: $(wc -l < /tmp/files) files. Also installed for the build:" \
    "$(grep -vx "$pkg" /tmp/new | tr '\n' ' ')"
echo "$version" > /out/.last-version
EOF
version=$(cat "$out_dir/.last-version")
rm -f "$out_dir/.last-version"
echo "$out_dir/$version.tar.zst"
