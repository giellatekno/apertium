# Handoff / TODO

Moving from the old setup to this umbrella repo. In the old setup, `~/gut/apertium/apertium-apy`
was a clone of upstream, and the LLM change was applied as a `.patch` file while building the
image. Delete this file once everything below is done.

## Current state (2026-11-17)

- `giellatekno/apertium-apy` is forked. Branch `giellatekno-main` = upstream `bd53dac` plus one
  commit, `e21ef1f Add /translateLLM endpoint`, which:
  - adds `apertium_apy/handlers/translate_llm.py`, `apertium_apy/utils/llm_postedit.py` and
    `llm.example.conf`
  - adds 3 lines to `apertium_apy/apy.py`: the import, plus
    `handlers.extend(llm_handlers(args.config))` in `setup_application`
- This repo is pushed (`origin/main`). Both submodules track `giellatekno-main`.
- `giellatekno/apertium-html-tools`: branch `giellatekno-main` (the fork's default) = upstream
  `b61d748` plus the "Post-cleanup with AI" toggle (step 9). It's turned on by
  `llmPostEditing: { model: 'GPT-4.1 mini' }` in `config.ts`.
- `deploy/` is restructured (steps 4–6): the image builds from the submodule, not a patch.
  Image builds and step 7 is done: `/listPairs`, `/translate`, `/identifyLang` and `/translateLLM`
  (sme-nob, gpt-4.1-mini) all work. The old checkout `~/gut/apertium/apertium-apy` can be deleted once this repo works.

## Steps

1. [x] **Fork apertium-html-tools and add it as a submodule.**
   ```sh
   gh repo fork apertium/apertium-html-tools --org giellatekno --clone=false
   git submodule add -b master git@github.com:giellatekno/apertium-html-tools.git
   ```
   Create `giellatekno-main` there (and make it the default branch) once there are frontend
   changes, then set `branch = giellatekno-main` in `.gitmodules`. Like the apy fork, it may have
   old `giellatekno*` branches copied from upstream; check with `git ls-remote --heads`.
2. [x] **Add the `upstream` remotes** in both submodules (see README "Getting started").
3. [x] **Optional: delete the obsolete branches in the forks.** This only deletes the forks'
   copies; upstream keeps its own. For apertium-apy:
   `giellatekno giellatekno-checkerhandler giellatekno-keepuntranslated-parse-before-translating
   giellatekno-smaller-pipebuf giellatekno-xsl-pdf giellatekno-xsl-pdf-diskcache`
   (`git -C apertium-apy push origin --delete <names>`).
4. [x] **Restructure `deploy/`** to `deploy/apy/{Containerfile,apy.conf}` + `deploy/README.md`.
   Delete `apy-llm.patch` and `make-patch.sh`, which the fork replaces.
5. [x] **Change the Containerfile to build from the submodule instead of clone + patch.**
   - Builder: replace the `git init/fetch/checkout` + `COPY apy-llm.patch` + `git apply` steps
     with `COPY apertium-apy/ /src/`. `git` is then no longer needed in the builder's apt list.
   - Runtime: `COPY deploy/apy/apy.conf /etc/apertium-apy/apy.conf`.
   - Remove the `APY_REPO` / `APY_REF` build args. The submodule commit is now the pin.
   - Build from the repo root: `podman build -t apertium-apy-llm -f deploy/apy/Containerfile .`
   - Add `.containerignore`: `apertium-html-tools/`, `**/.git`, `**/__pycache__`, `deploy/**/*.md`.
   - Keep all the existing workarounds as they are: fasttext `CPPFLAGS='-include cstdint'`, the
     NumPy 2 `np.asarray` sed, and `--fasttext-model` in CMD (upstream's `apply_config` crashes
     on options whose default is `None`).
   - Downside: the image is built from the submodule's working tree, so uncommitted edits would
     end up in it. If that matters, keep the clone step instead, with
     `APY_REPO=https://github.com/giellatekno/apertium-apy.git` and
     `--build-arg APY_REF=$(git -C apertium-apy rev-parse HEAD)`. That builds only pushed commits.
6. [x] **Rewrite `deploy/README.md`**: drop the patch sections ("Updating to a newer upstream",
   the `apy-llm.patch` / `make-patch.sh` rows, the `APY_REF` / `APY_REPO` build args) and update
   the build command and paths. Keep the Run, runtime mounts, `APY_LLM_API_KEY`, matching tool
   versions, and workarounds sections.
7. [x] **Build and test the image** (`/listPairs`, `/translate`, `/translateLLM`, `/identifyLang`).
8. [x] **First commit and push** of this repo: `.gitmodules`, submodules, README, deploy.
9. [x] Frontend: add a way to use `/translateLLM` (e.g. a toggle) in the html-tools fork.
10. [ ] **Deploy to Cloudflare** with `deploy/cloudflare/manage.py` (see "Hosting on Cloudflare"
    in `deploy/README.md`). Instances (`instances.toml`): `jorgal` (sme-nob) and `tolkimine`
    (fin-est, both directions).
    - test (2026-11-18): both deployed and checked, at https://jorgal.uit-anders.workers.dev and
      https://tolkimine.uit-anders.workers.dev: cold start ~3–6 s, pairs from R2, `/translate`,
      `/translateLLM` through the key-adding proxy.
    - prod: blocked; the Giellatekno account is on Workers Free, and Containers need the **Workers
      Paid plan** (check which payment method is on file first). Its workers.dev subdomain is now
      `giellatekno`, so prod will be `jorgal.giellatekno.workers.dev`. Then
      `manage.py deploy jorgal prod` (the bucket and sme-nob are already there).
    - Afterwards: point the old domain (under uit.no) to the Worker; later perhaps GitHub Actions.
    - tolkimine: language detection doesn't know fin/est (APy's fastText model); est→fin is a much
      weaker pair than fin→est.
11. [ ] **Northern Sámi strings** for the `LLM_Post_Editing*` keys, by a native speaker (see the
    note in `README.md`).

## Open questions

- Offer `/translateLLM` to upstream apertium-apy? It's turned on only by an optional `[LLM]`
  config section. If upstream accepts it, the apy fork would only need to track upstream.
- Should any language pairs ever be submodules? Only if we start changing pairs ourselves;
  for now they come from apt.
