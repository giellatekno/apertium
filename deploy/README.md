# Deployment: APy + /translateLLM

How to build and run our APy image, and how to host it on Cloudflare. The code is the
`apertium-apy` submodule (our fork, with the `/translateLLM` endpoint). This directory has only the
deployment-specific parts:

| File | What |
|---|---|
| `apy/Containerfile` | Two-stage build: build the `apertium-apy` submodule; copy only the runtime parts into `debian:trixie-slim`. No language pairs |
| `apy/apy.conf` | Runtime config (`[APY]` + `[LLM]`). A copy is baked in; mount your own over it |
| `apy/start.sh` | The image's entrypoint: fetches the language pairs from `PAIRS_URL` if set, then runs APy |
| `deploy.sh` | Deploys everything to Cloudflare: builds the image and the frontend, uploads the pairs, runs `wrangler deploy`, restarts APy |
| `html-tools/config.ts` | Our frontend config, used instead of the fork's `config.ts` when building |
| `pairs/build.sh` | Builds a language pairs archive (`pairs-<timestamp>.tar.zst`) inside the APy image |
| `pairs/upload.sh` | Uploads an archive to R2 and makes it the current one |
| `cloudflare/` | The Cloudflare Worker: serves the frontend, runs APy in a container. See [Hosting on Cloudflare](#hosting-on-cloudflare-prototype) |

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

Instead of mounting the pairs, you can give the container a pairs archive: `-e PAIRS_URL=http://…/pairs-….tar.zst`
(see `pairs/build.sh`; any HTTP server works, e.g. `python3 -m http.server` in the archive's directory).

### What to provide at runtime

| Host (VM) | Container | Required | Why |
|---|---|---|---|
| `/usr/share/apertium` | `/usr/share/apertium` (ro) | this or `PAIRS_URL` | Language pair data and `.mode` files from the `apertium-*` packages. **Use the same path:** mode files contain absolute paths like `/usr/share/apertium/apertium-sme-nob/sme-nob.automorf.hfst`. APy finds every `*.mode` under it on startup, so new pairs show up after a restart. |
| `/etc/apertium-apy/` (containing `apy.conf`) | `/etc/apertium-apy/` (ro) | no (a copy of `deploy/apy/apy.conf` is baked in) | `[APY]` server options and `[LLM]` endpoint, model and prompt. A `prompt_file = llm-prompt.txt` next to it works too. The container runs as an unprivileged user, so the files must be world-readable (`chmod 644`). |
| env `APY_LLM_API_KEY` | env | for `/translateLLM` | The gateway key. Without it APy logs an error and leaves `/translateLLM` out; everything else works. |
| env `PAIRS_URL` | env | this or the mount | A pairs archive from `pairs/build.sh`, unpacked into `/usr/share/apertium` on startup (`start.sh`). |
| env `APY_LLM_ENDPOINT` | env | no | Overrides `endpoint` in `[LLM]`. Used on Cloudflare, where it points to the Worker's `llm.internal` proxy. |

Example `/etc/apertium-apy.env` (`chmod 600`; keep it outside the mounted directory so the key isn't visible inside the container as a file):

```
APY_LLM_API_KEY=xxxxxxxx
```

(`podman secret create apy-llm-key -` with `--secret apy-llm-key,type=env,target=APY_LLM_API_KEY` works too, if you prefer.)

**Matching tool versions:** the image installs the Apertium tools (lttoolbox, cg3, hfst, …) from the Apertium apt repo. Pairs are compiled data that must be readable by those tools. With a mount, build the image with the same `DEBIAN_RELEASE` and `APERTIUM_CADENCE` as the host, and rebuild it when you upgrade pairs. A pairs archive is built inside the image itself, so it matches; it records the tool versions, and `start.sh` logs a warning if the image's differ. If a pair suddenly returns errors, rebuild the image and then the archive.

## Hosting on Cloudflare (prototype)

**Status:** written and checked locally (types, `DRY_RUN=1 deploy/deploy.sh`, the container
fetching an archive, the built frontend talking to APy through `/apy`), but **not yet deployed**. `wrangler dev` can't run the container on a
machine where `docker` is rootless podman (`crun: cannot set memory swappiness with cgroupv2`);
it needs real Docker. Deploying only builds and pushes the image, which works with podman.

One Worker (`cloudflare/`) serves everything, using
[Cloudflare Containers](https://developers.cloudflare.com/containers/) (Workers Paid plan):

```
browser ──▶ Worker
             ├─ /*             static assets: the frontend build (cloudflare/dist/)
             ├─ /apy/*         Durable Object "apy-0" ──▶ APy container (port 2737)
             └─ /admin/restart restart APy (bearer token)

APy container: no internet access. Its only way out is two made-up hostnames, which the
Durable Object intercepts and the Worker handles:
  http://pairs.internal/current  ──▶ R2 bucket apertium-pairs: the archive named in pairs/current
  http://llm.internal/…          ──▶ UiT LLM gateway, with the API key added by the Worker
```

- **No secrets in the container.** APy gets a dummy key. The Worker's `LlmProxy` allows only
  the one LLM request APy makes (`LLM_ENDPOINT` in `wrangler.jsonc`) and adds the real key
  from a Worker secret. A compromised container can't read the key or reach anything else.
- **Language pairs come from R2**, not the image: on start, `start.sh` downloads and unpacks the
  current archive (~22 MB for sme-nob; about a second locally). Updating pairs needs no new image,
  only an upload and a restart.
- **One instance**, 1 vCPU / 2 GiB / 4 GB disk (`APY_RESOURCES` in `src/index.ts`). It stops after
  6 hours without requests (`IDLE_TIMEOUT_MS`, the maximum); the next request starts it again,
  which takes a few seconds. Memory and disk are billed while it runs, CPU only when used.
- **The frontend is static and needs no APy to load.** Its build bakes in the pair list and
  language names, which it fetches from APy. `deploy.sh` builds it against a *local* APy running
  the same image and pairs archive, so the build doesn't need the deployed one, and the language
  menus always match the deployed pairs. In the browser it calls `/apy` on whatever domain serves
  it (see `html-tools/config.ts`), so the build doesn't depend on the domain either.
- Cloudflare's current container API (`scheduling_policy: "durable_object"`) is in **beta**.

### First-time setup

```sh
cd deploy/cloudflare
npm install
npx wrangler login
npx wrangler r2 bucket create apertium-pairs
npx wrangler secret put APY_LLM_API_KEY        # the UiT gateway key
npx wrangler secret put ADMIN_TOKEN            # any long random string, e.g. `openssl rand -hex 32`
```

Then build a pairs archive and deploy (below). The first container requests may fail for a few
minutes after the first deploy.

### Deploying

Every deploy, whether it's new code, new pairs or both:

```sh
podman build -t apertium-apy-llm -f deploy/apy/Containerfile .   # build.sh uses this image
mkdir -p /tmp/pairs
deploy/pairs/build.sh /tmp/pairs apertium-sme-nob                 # list every pair to serve
ADMIN_TOKEN=… deploy/deploy.sh /tmp/pairs/pairs-<timestamp>.tar.zst
```

To redeploy without changing the pairs, pass the same archive again: it doesn't need rebuilding.
`deploy.sh`:

1. builds the APy image locally and starts it with the archive;
2. builds the frontend (the fork's committed `HEAD` with `html-tools/config.ts`) against it, into
   `cloudflare/dist/`, plus `build-info.json` recording the pairs and commits;
3. uploads the archive to R2 and makes it the current one;
4. runs `wrangler deploy`, which builds the image with Docker, pushes it if it changed, and deploys
   the Worker and the frontend;
5. restarts APy (`POST /admin/restart`), so it runs the new image and pairs.

It refuses to run if a submodule has uncommitted changes (`ALLOW_DIRTY=1` overrides).
`DRY_RUN=1` does steps 1–2 and `wrangler deploy --dry-run`. Check a live site's contents at
`/build-info.json`.

Each archive holds all pairs. To roll back, deploy with an older archive. Deploying an archive
without `deploy.sh` (`pairs/upload.sh` and a restart) works too, but then the frontend's language
menus may not match the pairs.

**Docker's DNS:** Docker builds run in a long-lived `buildx_buildkit_default` container, which
keeps the DNS servers of the network it started on. If a build fails with `lookup
registry-1.docker.io … server misbehaving` after you change networks, run
`docker restart buildx_buildkit_default`.

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
- **Not included:** the language pairs (mounted, or fetched from `PAIRS_URL`). Also not included: the upstream test pair `apertium-eng-spa` and `giella-core`, which the upstream Dockerfile installs.

## Workarounds in the Containerfile (upstream issues)

- `fasttext-wheel==0.9.2` (pinned in upstream's `Pipfile.lock`) doesn't compile with GCC 14, so it is built with `CPPFLAGS='-include cstdint'`.
- That fasttext version breaks with NumPy 2 (also pinned): `/identifyLang` returned HTTP 500. The build applies fastText's own later fix (`np.array(..., copy=False)` → `np.asarray(...)`).
- Upstream's config file parser (`apply_config`) crashes on `[APY]` options whose CLI default is `None` (`fasttext_model`, `ssl_cert`, `missing_freqs`, …). Those go on the command line instead. That's why `--fasttext-model` is in `CMD`. To add one, override the whole command (`start.sh` adds `-C <config>` itself):
  `podman run … apertium-apy-llm --fasttext-model /opt/apy/app/lid.release.ftz --ssl-cert … /usr/share/apertium`

## Updating to a newer upstream / changing the LLM code

Done in the submodule; see the root [`README.md`](../README.md). Then rebuild the image and test it.
