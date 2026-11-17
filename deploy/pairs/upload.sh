#!/bin/sh
# Uploads a pairs archive (from build.sh) to R2 and makes it the current one.
#
#   deploy/pairs/upload.sh PATH/pairs-<timestamp>.tar.zst [--local]
#
# --local writes to wrangler dev's local R2 instead of the real bucket.
# Afterwards, restart APy so it fetches the new pairs (see deploy/README.md).
# Rolling back is uploading `current` again with an older name; old archives
# stay in the bucket until you delete them.
set -eu

[ $# -ge 1 ] || { sed -n '2,4p' "$0" >&2; exit 2; }
archive=$(realpath "$1")
where=${2:---remote}
name=$(basename "$archive")
case $name in
    pairs-*.tar.zst) ;;
    *) echo "upload.sh: expected a pairs-*.tar.zst file from build.sh" >&2; exit 2 ;;
esac

# Must match r2_buckets in deploy/cloudflare/wrangler.jsonc
bucket=apertium-pairs

cd "$(dirname "$0")/../cloudflare"
npx wrangler r2 object put "$bucket/pairs/$name" --file "$archive" --content-type application/zstd "$where"
printf '%s' "$name" | npx wrangler r2 object put "$bucket/pairs/current" --pipe --content-type text/plain "$where"
echo "upload.sh: pairs/current is now $name"
