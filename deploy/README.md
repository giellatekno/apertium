# Deployment: APy + /translateLLM

How to build and run our APy image, and how to host it on Cloudflare. The code is the
`apertium-apy` submodule (our fork, with the `/translateLLM` endpoint). This directory has only the
deployment-specific parts:

| File | What |
|---|---|
| `apy/Containerfile` | Two-stage build: build the `apertium-apy` submodule; copy only the runtime parts into `debian:trixie-slim`. No language pairs |
| `apy/apy.conf` | Runtime config (`[APY]` + `[LLM]`). A copy is baked in; mount your own over it |
| `apy/start.sh` | The image's entrypoint: fetches the language pairs in `PAIRS` from `PAIRS_URL` if set, then runs APy |
| `html-tools/config.ts` | Our frontend config, used instead of the fork's `config.ts` when building |
| `pairs/build.sh` | Builds the archive of one language pair (`<package version>.tar.zst`) inside the APy image |
| `cloudflare/` | The Cloudflare Worker (serves the frontend, runs APy in a container), and `manage.py` to deploy and manage it. See [Hosting on Cloudflare](#hosting-on-cloudflare-prototype) |
| `cloudflare/manage.py` | Deploys, inspects, restarts and deletes instances; manages their R2 buckets |
| `cloudflare/instances.toml` | The Cloudflare accounts (test, prod) and Apertium instances (`jorgal`, `tolkimine`) |

## Build

From the repo root (the build context is the whole repo, minus what `.dockerignore` excludes):

```sh
git submodule update --init
podman build -t apertium-apy-llm -f deploy/apy/Containerfile .
```

The image is built from the submodule's **working tree**, so uncommitted edits in `apertium-apy/`
end up in it. For a deployable image, build from a clean checkout of a pushed commit
(`git -C apertium-apy status` should be clean).

Build args, all optional:

| Arg | Default | Notes |
|---|---|---|
| `DEBIAN_RELEASE` | `trixie` | Use the same release as the VM |
| `APERTIUM_CADENCE` | `nightly` | `nightly` or `release`; the same Apertium repo the VM installs pairs from |

## Run with podman

```sh
podman run -d --name apertium-apy \
  -p 2737:2737 \
  -v /usr/share/apertium:/usr/share/apertium:ro \
  -v /etc/apertium-apy:/etc/apertium-apy:ro,Z \
  --env-file /etc/apertium-apy.env \
  apertium-apy-llm
```

Then `podman restart apertium-apy` after you change the config, or after you install or upgrade language pairs.

Instead of mounting the pairs, you can give the container pair archives from `pairs/build.sh`: put
them in a directory as `<pair>.tar.zst`, and run with
`-v DIR:/pairs:ro -e PAIRS="sme-nob" -e PAIRS_URL=file:///pairs` (an `http://` URL works too).

### What to provide at runtime

| Host (VM) | Container | Required | Why |
|---|---|---|---|
| `/usr/share/apertium` | `/usr/share/apertium` (ro) | this or `PAIRS` | Language pair data and `.mode` files from the `apertium-*` packages. **Use the same path:** mode files contain absolute paths like `/usr/share/apertium/apertium-sme-nob/sme-nob.automorf.hfst`. APy finds every `*.mode` under it on startup, so new pairs show up after a restart. |
| `/etc/apertium-apy/` (containing `apy.conf`) | `/etc/apertium-apy/` (ro) | no (a copy of `deploy/apy/apy.conf` is baked in) | `[APY]` server options and `[LLM]` endpoint, model and prompt. A `prompt_file = llm-prompt.txt` next to it works too. The container runs as an unprivileged user, so the files must be world-readable (`chmod 644`). |
| env `APY_LLM_API_KEY` | env | for `/translateLLM` | The gateway key. Without it APy logs an error and leaves `/translateLLM` out; everything else works. |
| env `PAIRS`, `PAIRS_URL` | env | this or the mount | Pairs to fetch (e.g. `sme-nob sme-smj`) from `$PAIRS_URL/<pair>.tar.zst` (archives from `pairs/build.sh`), unpacked into `/usr/share/apertium` on startup (`start.sh`). |
| env `APY_LLM_ENDPOINT` | env | no | Overrides `endpoint` in `[LLM]`. Used on Cloudflare, where it points to the Worker's `llm.internal` proxy. |

Example `/etc/apertium-apy.env` (`chmod 600`; keep it outside the mounted directory so the key isn't visible inside the container as a file):

```
APY_LLM_API_KEY=xxxxxxxx
```

(`podman secret create apy-llm-key -` with `--secret apy-llm-key,type=env,target=APY_LLM_API_KEY` works too, if you prefer.)

**Matching tool versions:** the image installs the Apertium tools (lttoolbox, cg3, hfst, …) from the Apertium apt repo. Pairs are compiled data that must be readable by those tools. With a mount, build the image with the same `DEBIAN_RELEASE` and `APERTIUM_CADENCE` as the host, and rebuild it when you upgrade pairs. A pair archive is built inside the image itself, so it matches; it records the tool versions, and `start.sh` logs a warning if the image's differ. If a pair suddenly returns errors, rebuild the image.

## Hosting on Cloudflare (prototype)

**Status:** written and checked locally (`manage.py deploy … --dry-run`, the container fetching
pair archives, the built frontend talking to APy through `/apy`), but **not yet deployed**.
`wrangler dev` can't run the container on a machine where `docker` is rootless podman (`crun:
cannot set memory swappiness with cgroupv2`); it needs real Docker. `manage.py` doesn't use
Docker at all: it builds with podman and pushes the image itself.

Each **instance** (in `cloudflare/instances.toml`, by codename: `jorgal` for sme-nob, `tolkimine` for fin-est both ways) is one Worker, named after it, using
[Cloudflare Containers](https://developers.cloudflare.com/containers/) (Workers Paid plan):

```
browser ──▶ Worker <instance>.<subdomain>.workers.dev
             ├─ /*             static assets: the frontend build
             ├─ /apy/*         Durable Object "apy-0" ──▶ APy container (port 2737)
             └─ /admin/restart restart APy (bearer token)

APy container: no internet access. Its only way out is two made-up hostnames, which the
Durable Object intercepts and the Worker handles:
  http://pairs.internal/<pair>.tar.zst ──▶ R2 bucket apertium-pairs: the pair's current version
  http://llm.internal/…          ──▶ UiT LLM gateway, with the API key added by the Worker
```

- **No secrets in the container.** APy gets a dummy key. The Worker's `LlmProxy` allows only
  the one LLM request APy makes (`LLM_ENDPOINT` in `wrangler.jsonc`) and adds the real key
  from a Worker secret. A compromised container can't read the key or reach anything else.
- **Language pairs come from R2**, not the image: on start, `start.sh` downloads and unpacks the
  current version of each of the instance's pairs (~22 MB for sme-nob; about a second locally).
  One bucket per account, `apertium-pairs`, is shared by all instances. Updating a pair needs no
  new image and no deploy.
- **One container per instance**, 1 vCPU / 2 GiB / 4 GB disk (`APY_RESOURCES` in `src/index.ts`).
  It stops after 6 hours without requests (`IDLE_TIMEOUT_MS`, the maximum); the next request starts
  it again, which takes a few seconds. Memory and disk are billed while it runs, CPU only when used.
- **The frontend is static and needs no APy to load.** Its build bakes in the pair list and
  language names, which it fetches from APy. `manage.py deploy` builds it against a *local* APy
  running the same image and pair archives, so the build doesn't need the deployed one, and the
  language menus always match the deployed pairs. In the browser it calls `/apy` on whatever domain
  serves it (see `html-tools/config.ts`), so the build doesn't depend on the domain either.
- Cloudflare's current container API (`scheduling_policy: "durable_object"`) is in **beta**.

### manage.py

Everything goes through `deploy/cloudflare/manage.py` (Python 3.11+, standard library only). Each
command takes an instance and an account; `test` is "UiT / Anders Lorentsen", `prod` is
"Giellatekno" (the group account). The same in both accounts: `deploy`, `pairs rollback`,
`pairs delete` and `pairs prune` ask for confirmation (`--yes` skips it); `delete` asks you to
type the instance name, `pairs delete-bucket` to type "yes, delete the bucket"; the rest
(`restart`, `pairs update`, `pairs create-bucket`, and anything read-only) doesn't ask.

```sh
cd deploy/cloudflare
./manage.py list                          # instances, and where they're deployed
./manage.py deploy  jorgal test           # build and deploy (--dry-run to try)
./manage.py status  jorgal test           # deployed build, container, its pairs, request stats
./manage.py logs    jorgal test           # live logs
./manage.py restart jorgal test           # restart APy
./manage.py delete  jorgal test           # Worker, container, images (--with-bucket: also the pairs bucket)

./manage.py pairs create-bucket test      # the account's apertium-pairs bucket (deploy also creates it)
./manage.py pairs delete-bucket test      # with all pairs; you must type "yes, delete the bucket"
./manage.py pairs list-available          # all pairs in the Apertium apt repo, newest versions
./manage.py pairs list     test           # pairs in R2: versions, current, newer ones available
./manage.py pairs update   test [PAIR…]   # upload newer versions, restart the instances using them
./manage.py pairs rollback test PAIR [VERSION]
./manage.py pairs delete   test PAIR VERSION
./manage.py pairs prune    test [--keep N]

./manage.py usage test [--month 2026-11]  # per instance: requests, container hours, peak memory/CPU;
                                          # estimated cost for the month (and projected)

./manage.py images list  test             # APy images in the registry, which are deployed
./manage.py images prune test             # delete the ones no deployed instance uses
```

`./manage.py <command> --help` lists the options. Requirements: Node.js, podman (or `ENGINE=docker`),
and a login: `npx wrangler login` (once, covers both
accounts). `manage.py` uses that login for `wrangler` and for Cloudflare's API (R2 listings,
stats), and picks the account by setting `CLOUDFLARE_ACCOUNT_ID`. For manual `wrangler` commands,
set it yourself (the IDs are in `instances.toml`), or `wrangler` asks which account to use.

**`usage`** reads Cloudflare's analytics, which go back about a month, and estimates costs at list
prices: container memory, CPU and disk are billed for the time a container runs, at the size in
`APY_RESOURCES` (`src/index.ts`). It counts only Apertium's usage: other projects' containers and
buckets in the same account are left out, although they share the plan's included amounts. The
actual invoice is in the dashboard (Manage Account → Billing); `wrangler`'s login can't read it.

**Secrets:** `deploy/cloudflare/secrets.<account>.env` (gitignored, `chmod 600`) holds the Worker's
secrets for that account. The first `deploy` asks for the LLM gateway key and generates the
`ADMIN_TOKEN`:

```
APY_LLM_API_KEY=<the UiT gateway key>
ADMIN_TOKEN=<long random string>
```

They're set on every deploy (`wrangler deploy --secrets-file`); `wrangler` won't deploy a *new*
Worker without its required secrets, and `wrangler secret put` can't set them before it exists.
`manage.py` also uses `ADMIN_TOKEN` for restarts.

### What `deploy` does

1. Builds the APy image locally (`apertium-apy-llm`).
2. Checks that the instance's pairs exist in the Apertium apt repo (asking inside the image, which
   uses the same repo `pairs/build.sh` installs from), before uploading or deploying anything.
3. Creates the `apertium-pairs` R2 bucket, if it doesn't exist yet.
4. Brings each of the instance's pairs to the newest version (below): builds the archive with
   `pairs/build.sh`, uploads it and makes it current, if R2 doesn't have it yet.
5. Starts the image locally with those pairs and builds the frontend against it (the fork's
   committed `HEAD` with `html-tools/config.ts`), plus `build-info.json` recording the instance,
   pair versions and commits.
6. Pushes the APy image from step 1 to the account's Cloudflare registry
   (`<instance>-apy`), noting the digest the registry received, and records it in `build-info.json`.
7. Writes `.build/<instance>/wrangler.json`: `wrangler.jsonc` with the instance's Worker name,
   pairs (`PAIRS`), frontend, and that image pinned by digest.
8. `wrangler deploy`: deploys the Worker, the frontend and the container with that image.
9. Restarts APy, so it runs the new image and pairs, and restarts other deployed instances whose
   pairs got a new version.

**A new image can take a few minutes to go live.** After a deploy that changes the APy image,
check the live site for the change. If it isn't there within about five minutes, run
`./manage.py restart <instance> <account>`. Once (2026-11-18), a restart right after the deploy
started the old image again, while a restart five minutes later started the new one. Cloudflare
was probably still distributing the new image. The same deploy was live at once on
jorgal.uit.no, so this doesn't always happen.

`manage.py` pushes the image itself rather than letting `wrangler` build and push it: so the
deployed image is exactly the one the pairs and frontend were built and tested with, and because
`wrangler` takes the image digest from the local engine. With podman behind the `docker` command,
the pushed image gets a different digest, and `wrangler deploy` fails with "The image is invalid or
does not exist in this account".

It refuses to run if a submodule has uncommitted changes (`--allow-dirty` overrides). Archives
are cached in `deploy/cloudflare/.build/pairs/`, build output in `.build/<instance>/`. Check a
live site's contents at `/build-info.json`, or with `status`.

### Pair versions and rollback

R2 holds `pairs/<pair>/<version>.tar.zst` for every version uploaded, and `pairs/<pair>/current`
naming the one in use. A pair is always at the newest version in the apt repo, unless it's rolled
back:

- `deploy` and `pairs update` upload a version that R2 doesn't have yet and make it current. So a
  new release goes live with the next deploy or `pairs update`, which also restarts the instances
  using it. Instances don't need a redeploy for a new pair *version*; their language menus don't
  change.
- `pairs rollback test sme-nob` points `current` back to the previous version (or a given one) and
  restarts the instances using it. While the version it rolled back from is the newest, deploys
  and updates leave the rollback alone (and warn). When a newer version is released, it replaces
  the rollback; `pairs rollback test sme-nob <newest>` rolls forward by hand.
- Old versions stay in R2 for rollbacks until `pairs prune` (keeps the current and 2 others per
  pair) or `pairs delete`.

Adding a pair to an instance, or removing one: edit `instances.toml` and deploy (the frontend's
language menus are built from the pairs).

**A new instance:** add it to `instances.toml` and deploy it. It shares the bucket and pair versions with the other instances.

### Serving an instance under another address

To show an instance at an address we don't control in Cloudflare, reverse-proxy it to the Worker
in nginx; users stay on that address. Jorgal is served this way at `https://gtweb.uit.no/jorgal/`
and `https://jorgal.uit.no/`: the setup and nginx config are under "gtweb.uit.no" in the
[top-level README](../README.md#gtwebuitno). The frontend calls `apy` next to the page
(`html-tools/config.ts`), so it works under any path. A whole hostname needs its own certificate.

## LLM models

`/translateLLM` goes through UiT's Azure OpenAI gateway. The `model` setting in `[LLM]` is the
Azure **deployment name**, which goes into the URL. It's chosen by whoever set up the deployment,
and here it doesn't match the model it runs. As of 2026-11-17 the gateway has these deployments:

| Deployment (`model =`) | Actual model | Price per 1M tokens, in / out (Azure list price) | Notes |
|---|---|---|---|
| `gpt-35-turbo` | `gpt-4.1-mini-2025-04-14` | ~$0.40 / $1.60 | **Default.** The name is a leftover: UiT upgraded the model behind it |
| `gpt-4` | `gpt-4o-2024-11-20` | ~$2.50 / $10.00 | About 6× the price. Same leftover-name situation |

No other deployments exist (tried `gpt-4o`, `gpt-4o-mini`, `gpt-4.1*`, `gpt-5*` and `o4-mini`;
all returned `DeploymentNotFound`). The response's `llmModel` field and APy's log show which model
actually answered.

**Why the cheapest is the default:** post-editing is a light task. The LLM only smooths
Apertium's output; it doesn't translate from scratch. The old setup already gave good enough
results with this deployment. It's also faster, and each `/translateLLM` call costs one request.

To list the deployments again (the key is `APY_LLM_API_KEY`):

```sh
curl -s -H "api-key: $APY_LLM_API_KEY" \
  'https://gw-uit.intark.uh-it.no/azure-openai/openai/deployments?api-version=2022-12-01' \
  | python3 -c 'import json,sys; [print(d["id"], "->", d["model"]) for d in json.load(sys.stdin)["data"]]'
```

If UiT adds a reasoning model (`gpt-5*`, `o*`), note that `llm_postedit.py` always sends
`temperature`, and those models accept only the default (1). Such a model would need code changes.

## Not baked in vs. baked in

- **Baked in:** `langNames.db`, the localised language names (`/listLanguageNames`). Despite being built by the Makefile, it comes only from the TSV files in `language_names/` (scraped CLDR/SIL names). It does not look at installed pairs, so new pairs don't require rebuilding it.
- **Baked in:** `lid.release.ftz`, APy's own fastText language-identification model (`/identifyLang`). It knows sme, nob, nno and ~35 other languages. cld2 is not included: it is only used when fastText isn't, and it doesn't know Sámi.
- **Not included:** the language pairs (mounted, or fetched via `PAIRS`/`PAIRS_URL`). Also not included: the upstream test pair `apertium-eng-spa` and `giella-core`, which the upstream Dockerfile installs.

## Workarounds in the Containerfile (upstream issues)

- `fasttext-wheel==0.9.2` (pinned in upstream's `Pipfile.lock`) doesn't compile with GCC 14, so it is built with `CPPFLAGS='-include cstdint'`.
- That fasttext version breaks with NumPy 2 (also pinned): `/identifyLang` returned HTTP 500. The build applies fastText's own later fix (`np.array(..., copy=False)` → `np.asarray(...)`).
- Upstream's config file parser (`apply_config`) crashes on `[APY]` options whose CLI default is `None` (`fasttext_model`, `ssl_cert`, `missing_freqs`, …). Those go on the command line instead. That's why `--fasttext-model` is in `CMD`. To add one, override the whole command (`start.sh` adds `-C <config>` itself):
  `podman run … apertium-apy-llm --fasttext-model /opt/apy/app/lid.release.ftz --ssl-cert … /usr/share/apertium`

## Updating to a newer upstream / changing the LLM code

Done in the submodule; see the root [`README.md`](../README.md). Then rebuild the image and test it.
