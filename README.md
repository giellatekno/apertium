# Giellatekno Apertium setup

Everything Giellatekno runs on top of [Apertium](https://www.apertium.org/): our forks of the
upstream server and web frontend, plus what we need to build and deploy them.

> [!IMPORTANT]
> **Missing Northern Sámi strings (to do).** The frontend's "Post-cleanup with AI" toggle, its
> explanation popup and the consent popup have UI text only in English and Bokmål
> (`apertium-html-tools/src/strings/eng.json` and `nob.json`, the `LLM_Post_Editing*` keys). In
> `sme.json` they're missing, so Sámi users see English. A native speaker should translate them.
> Add the same keys to `sme.json`, keeping the `{{source}}`, `{{target}}` and `{{model}}`
> placeholders as they are.

| Path | What | Upstream |
|---|---|---|
| `apertium-apy/` | Submodule: [giellatekno/apertium-apy](https://github.com/giellatekno/apertium-apy), branch `giellatekno-main`. APy, the HTTP API server, plus our `/translateLLM` endpoint (Apertium translation followed by LLM post-editing). | [apertium/apertium-apy](https://github.com/apertium/apertium-apy) |
| `apertium-html-tools/` | Submodule: [giellatekno/apertium-html-tools](https://github.com/giellatekno/apertium-html-tools), branch `giellatekno-main`. The web frontend (despite the name, it's the client UI), plus our optional "Post-cleanup with AI" toggle, which uses `/translateLLM`. | [apertium/apertium-html-tools](https://github.com/apertium/apertium-html-tools) |
| `deploy/` | Container build and runtime config. See `deploy/README.md`. | – |

Language pairs (e.g. `apertium-sme-smj`) are **not** part of this repo. The server installs them
from the Apertium apt repository and mounts them into the container.

## Principle: keep the forks' changes from upstream small

The forks contain only feature code, such as the `/translateLLM` handler and changes to the
frontend. Everything specific to our deployment lives here instead: the Containerfile, config,
workarounds for upstream build problems, and docs. That keeps merges from upstream easy.

## Branches in the forks

- `master`: a clean mirror of upstream. Never commit to it.
- `giellatekno-main`: our work. It's the fork's default branch and the branch the submodule tracks.
- Ignore the other `giellatekno*` branches. They are very old branches that upstream has, and
  GitHub copied them into the forks. The plain `giellatekno` name was already taken by one of
  them, which is why ours is called `giellatekno-main`.
- **Merge upstream in, don't rebase.** The umbrella repo records exact submodule commits. If you
  rebase and force-push, the commits that older umbrella versions point to drop off every
  branch, and checking out those versions can break.

## Getting started

```sh
git clone --recurse-submodules git@github.com:giellatekno/apertium.git
cd apertium

# Recommended settings for this clone
git config submodule.recurse true          # pull/checkout also update the submodules
git config push.recurseSubmodules check    # refuse to push a pointer to an unpushed submodule commit

# .gitmodules doesn't store the upstream remotes, so add them in each new clone
git -C apertium-apy        remote add upstream https://github.com/apertium/apertium-apy.git
git -C apertium-html-tools remote add upstream https://github.com/apertium/apertium-html-tools.git
```

## Working in a submodule

After `git submodule update`, a submodule is checked out at a bare commit, not on a branch.
**Switch to the branch before committing**, or your commits won't be on any branch:

```sh
cd apertium-apy
git switch giellatekno-main
# edit, commit, push
cd ..
git add apertium-apy && git commit -m "Bump apertium-apy"   # record the new commit
git push
```

## Updating to a newer upstream

```sh
cd apertium-apy
git fetch upstream
git switch giellatekno-main
git merge upstream/master        # resolve conflicts; our changes to upstream files are small
# rebuild the image, test, then:
git push
cd .. && git add apertium-apy && git commit -m "Merge upstream into apertium-apy" && git push
```

## Deploying

See [`deploy/README.md`](deploy/README.md).
