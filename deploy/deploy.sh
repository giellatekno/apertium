#!/bin/sh
# Deploys everything to Cloudflare: APy image, language pairs, frontend, Worker.
#
#   deploy/deploy.sh PATH/pairs-<timestamp>.tar.zst
#
# 1. Builds the APy image locally (the same Containerfile wrangler builds).
# 2. Starts it with the pairs archive and builds the frontend against it, so
#    the frontend's language lists match the deployed pairs exactly.
# 3. Uploads the archive to R2 and makes it the current one.
# 4. `wrangler deploy`: the Worker, the frontend, and the image if it changed.
# 5. Restarts APy, so it runs the new image and pairs (needs ADMIN_TOKEN).
#
# Environment:
#   DRY_RUN=1       steps 1-2 only, then `wrangler deploy --dry-run`
#   ADMIN_TOKEN     the Worker's ADMIN_TOKEN secret, for step 5
#   WORKER_URL      e.g. https://apertium.example.org (default: from wrangler's output)
#   ALLOW_DIRTY=1   deploy even if the submodules have uncommitted changes
#   ENGINE          container engine for steps 1-2 (default podman)
set -eu

[ $# -eq 1 ] || { sed -n '2,4p' "$0" >&2; exit 2; }
archive=$(realpath "$1")
case $(basename "$archive") in
    pairs-*.tar.zst) ;;
    *) echo "deploy.sh: expected a pairs-*.tar.zst file from deploy/pairs/build.sh" >&2; exit 2 ;;
esac

root=$(cd "$(dirname "$0")/.." && pwd)
engine=${ENGINE:-podman}
image=apertium-apy-llm
say() { printf '\n==> %s\n' "$*"; }
# Runs wrangler, printing its output without docker's build progress lines;
# fails if wrangler fails.
wrangler() {
    if npx wrangler "$@" > "$tmp/wrangler.log" 2>&1; then status=0; else status=$?; fi
    grep -vE '^#[0-9]+ ' "$tmp/wrangler.log" || true
    return $status
}

# wrangler builds the image from the submodule's working tree; the frontend
# is built from html-tools' HEAD. Uncommitted changes would make the deploy
# differ from what's in git.
for sub in apertium-apy apertium-html-tools; do
    if [ -n "$(git -C "$root/$sub" status --porcelain)" ] && [ "${ALLOW_DIRTY:-}" != 1 ]; then
        echo "deploy.sh: $sub has uncommitted changes (set ALLOW_DIRTY=1 to deploy anyway)" >&2
        exit 1
    fi
done

tmp=$(mktemp -d)
container=
cleanup() {
    [ -n "$container" ] && "$engine" rm -f "$container" >/dev/null 2>&1
    rm -rf "$tmp"
}
trap cleanup EXIT INT TERM

say "1/5 Building the APy image"
"$engine" build -q -t "$image" -f "$root/deploy/apy/Containerfile" "$root" >/dev/null

say "2/5 Building the frontend against a local APy with $(basename "$archive")"
container=$("$engine" run -d --rm -p 127.0.0.1::2737 \
    -v "$archive:/tmp/pairs.tar.zst:ro,Z" -e PAIRS_URL=file:///tmp/pairs.tar.zst "$image")
port=$("$engine" port "$container" 2737 | head -1 | sed 's/.*://')
apy=http://127.0.0.1:$port
i=0
until curl -fs "$apy/listPairs" >/dev/null; do
    i=$((i + 1))
    if [ $i -gt 120 ]; then
        "$engine" logs "$container" >&2
        echo "deploy.sh: local APy didn't start" >&2
        exit 1
    fi
    sleep 0.5
done
echo "Local APy serves: $(curl -fs "$apy/listPairs" | python3 -c 'import json,sys; print(" ".join(p["sourceLanguage"] + "-" + p["targetLanguage"] for p in json.load(sys.stdin)["responseData"]))')"

# A clean copy of html-tools at HEAD, with our config.ts
src=$tmp/html-tools
mkdir "$src"
git -C "$root/apertium-html-tools" archive HEAD | tar -x -C "$src"
cp "$root/deploy/html-tools/config.ts" "$src/config.ts"
if [ -d "$root/apertium-html-tools/node_modules" ]; then
    ln -s "$root/apertium-html-tools/node_modules" "$src/node_modules"
else
    (cd "$src" && npx -y yarn@1.22.22 install --frozen-lockfile --silent)
fi
(cd "$src" && APY_BUILD_URL=$apy HTML_URL=${WORKER_URL:-https://example.invalid}/ \
    npx -y yarn@1.22.22 -s build --prod) >/dev/null

dist=$root/deploy/cloudflare/dist
rm -rf "$dist"
cp -r "$src/dist" "$dist"
# What this deploy contains, for checking a live site: GET /build-info.json
cat > "$dist/build-info.json" <<EOF
{
  "pairs": "$(basename "$archive")",
  "apertium-apy": "$(git -C "$root/apertium-apy" rev-parse --short HEAD)",
  "apertium-html-tools": "$(git -C "$root/apertium-html-tools" rev-parse --short HEAD)",
  "apertium": "$(git -C "$root" rev-parse --short HEAD)",
  "built": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
"$engine" rm -f "$container" >/dev/null
container=
echo "Frontend built into deploy/cloudflare/dist/ ($(du -sh "$dist" | cut -f1))"

cd "$root/deploy/cloudflare"
[ -d node_modules ] || npm install --silent
npx wrangler types >/dev/null
npx tsc --noEmit

if [ "${DRY_RUN:-}" = 1 ]; then
    say "DRY_RUN: skipping the upload; wrangler deploy --dry-run"
    wrangler deploy --dry-run --outdir "$tmp/worker"
    exit 0
fi

say "3/5 Uploading the pairs archive"
"$root/deploy/pairs/upload.sh" "$archive"

say "4/5 Deploying the Worker"
wrangler deploy
url=${WORKER_URL:-$(grep -oE 'https://[A-Za-z0-9.-]+\.workers\.dev' "$tmp/wrangler.log" | head -1)}

say "5/5 Restarting APy"
if [ -z "${ADMIN_TOKEN:-}" ] || [ -z "$url" ]; then
    echo "Set ADMIN_TOKEN (and WORKER_URL if needed) to restart automatically. Run:"
    echo "  curl -X POST -H \"Authorization: Bearer \$ADMIN_TOKEN\" ${url:-<worker url>}/admin/restart"
else
    curl -fsS -X POST -H "Authorization: Bearer $ADMIN_TOKEN" "$url/admin/restart"
    echo "Deployed: $url  (contents: $url/build-info.json)"
fi
