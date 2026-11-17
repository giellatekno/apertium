# Deployment: APy + /translateLLM

How to build and run our APy image. The code is the `apertium-apy` submodule (our fork, with the
`/translateLLM` endpoint). This directory has only the deployment-specific parts:

| File | What |
|---|---|
| `apy/Containerfile` | Two-stage build: build the `apertium-apy` submodule; copy only the runtime parts into `debian:trixie-slim` |
| `apy/apy.conf` | Runtime config (`[APY]` + `[LLM]`). A copy is baked in; mount your own over it |

## Build

From the repo root (the build context is the whole repo, minus what `.containerignore` excludes):

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

## Run

```sh
podman run -d --name apertium-apy \
  -p 2737:2737 \
  -v /usr/share/apertium:/usr/share/apertium:ro \
  -v /etc/apertium-apy:/etc/apertium-apy:ro,Z \
  --env-file /etc/apertium-apy.env \
  apertium-apy-llm
```

Then `podman restart apertium-apy` after you change the config, or after you install or upgrade language pairs.

### What to provide at runtime

| Host (VM) | Container | Required | Why |
|---|---|---|---|
| `/usr/share/apertium` | `/usr/share/apertium` (ro) | yes | Language pair data and `.mode` files from the `apertium-*` packages. **Use the same path:** mode files contain absolute paths like `/usr/share/apertium/apertium-sme-nob/sme-nob.automorf.hfst`. APy finds every `*.mode` under it on startup, so new pairs show up after a restart. |
| `/etc/apertium-apy/` (containing `apy.conf`) | `/etc/apertium-apy/` (ro) | no (a copy of `deploy/apy/apy.conf` is baked in) | `[APY]` server options and `[LLM]` endpoint, model and prompt. A `prompt_file = llm-prompt.txt` next to it works too. The container runs as an unprivileged user, so the files must be world-readable (`chmod 644`). |
| env `APY_LLM_API_KEY` | env | for `/translateLLM` | The gateway key. Without it APy logs an error and leaves `/translateLLM` out; everything else works. |

Example `/etc/apertium-apy.env` (`chmod 600`; keep it outside the mounted directory so the key isn't visible inside the container as a file):

```
APY_LLM_API_KEY=xxxxxxxx
```

(`podman secret create apy-llm-key -` with `--secret apy-llm-key,type=env,target=APY_LLM_API_KEY` works too, if you prefer.)

**Matching tool versions:** the image installs the Apertium tools (lttoolbox, cg3, hfst, …) from the Apertium apt repo. The pairs on the VM are compiled data that must be readable by those tools, so build the image with the same `DEBIAN_RELEASE` and `APERTIUM_CADENCE` as the VM, and rebuild it when you upgrade pairs. If a pair suddenly returns errors after an upgrade, rebuild the image first.

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
- **Not included:** the language pairs (mounted). Also not included: the upstream test pair `apertium-eng-spa` and `giella-core`, which the upstream Dockerfile installs.

## Workarounds in the Containerfile (upstream issues)

- `fasttext-wheel==0.9.2` (pinned in upstream's `Pipfile.lock`) doesn't compile with GCC 14, so it is built with `CPPFLAGS='-include cstdint'`.
- That fasttext version breaks with NumPy 2 (also pinned): `/identifyLang` returned HTTP 500. The build applies fastText's own later fix (`np.array(..., copy=False)` → `np.asarray(...)`).
- Upstream's config file parser (`apply_config`) crashes on `[APY]` options whose CLI default is `None` (`fasttext_model`, `ssl_cert`, `missing_freqs`, …). Those go on the command line instead. That's why `--fasttext-model` is in `CMD`. To add one, override the whole command:
  `podman run … apertium-apy-llm -C /etc/apertium-apy/apy.conf --fasttext-model /opt/apy/app/lid.release.ftz --ssl-cert … /usr/share/apertium`

## Updating to a newer upstream / changing the LLM code

Done in the submodule; see the root [`README.md`](../README.md). Then rebuild the image and test it.
