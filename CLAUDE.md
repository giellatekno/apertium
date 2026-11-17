# Notes for Claude

- Read `TODO.md` first if it exists: it is the handoff from the session that planned this repo, with the current state and remaining steps.
- This is an umbrella repo; `apertium-apy/` (and later `apertium-html-tools/`) are submodules of our forks. Commit fork changes inside the submodule on `giellatekno-main` (check it's not a detached HEAD), push, then commit the updated pointer here.
- Keep the forks' changes from upstream minimal; deployment/build stuff belongs in `deploy/` here. Merge upstream into forks, don't rebase.
- Ignore the old upstream `giellatekno*` branches in the forks (other than `giellatekno-main`).
