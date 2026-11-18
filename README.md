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

## gtweb.uit.no

gtweb-03.uit.no (alias gtweb.uit.no), now doesn't run the containers, it
instead reverse-proxies to the corresponding Cloudflare Worker
(see "Hosting on Cloudflare" in [`deploy/README.md`](deploy/README.md)); users
stay on the addresses. Its nginx has the same proxy settings in two places:

| File on the server | Address |
|---|---|
| `/etc/nginx/default.d/jorgal` | `https://gtweb.uit.no/jorgal/` |
| `/etc/nginx/sites-available/jorgal.uit.no.conf` (symlinked from `sites-enabled/`) | `https://jorgal.uit.no/` (a DNS alias of the same server) |

The contents of the files are approximately as follows:

`/etc/nginx/default.d/jorgal`:

```nginx
location = /jorgal { return 301 /jorgal/; }   # the frontend needs the trailing slash
location /jorgal/ {
    # The trailing / replaces the /jorgal/ prefix: /jorgal/x becomes /x at the Worker
    proxy_pass https://jorgal.uit-anders.workers.dev/;   # later: https://jorgal.giellatekno.workers.dev/
    proxy_ssl_server_name on;                            # Cloudflare needs SNI
    proxy_ssl_verify on;
    proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
    proxy_http_version 1.1;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;      # a cold start plus an LLM call
    client_max_body_size 32m;     # document translation uploads
}
```

`/etc/nginx/sites-available/jorgal.uit.no.conf`:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name jorgal.uit.no;

    # Let's Encrypt HTTP-01 challenges
    location /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
    }
    location / {
        return 301 https://jorgal.uit.no$request_uri;
    }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name jorgal.uit.no;

    # Its own certificate: the server's covers only gtweb.uit.no and gtweb-03.uit.no
    ssl_certificate     /etc/letsencrypt/live/jorgal.uit.no/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/jorgal.uit.no/privkey.pem;

    location / {
        proxy_pass https://jorgal.uit-anders.workers.dev/;   # later: https://jorgal.giellatekno.workers.dev/
        proxy_ssl_server_name on;                            # Cloudflare needs SNI
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_http_version 1.1;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;      # a cold start plus an LLM call
        client_max_body_size 32m;     # document translation uploads
    }
}
```

The frontend calls `apy` next to the page, so it works both under `/jorgal/` and at the root.
**When jorgal moves to prod**, change `proxy_pass` in both files, then `sudo nginx -t && sudo
systemctl reload nginx`. The Worker sees the server's IP address rather than the users'.

## Possible future work: translation statistics

Not built yet. We'd like to know how many translations users run, for statistics only: no
translated text, no IP addresses or other user data, and not for debugging. (Neither this setup nor
the old one records translations, and we don't intend to.)

The Worker sees every `/apy/translate` and `/apy/translateLLM` request, so it could write one row
per translation to a D1 database (Cloudflare's SQLite), after answering the request
(`ctx.waitUntil`, so users wait no longer) and without changing the APy fork:

| Column | |
|---|---|
| datetime | when the translation was run |
| language pair | e.g. `sme-nob` |
| text length | length of the source text, in characters |
| post-cleaned with AI | whether it was a `/translateLLM` request |

Whether the AI step succeeded is deliberately not recorded: if it fails (e.g. the UiT LLM gateway
key stops working), the normal logs show it (`manage.py logs`, or Workers Logs in the dashboard).
`manage.py` could then show the counts, e.g. per pair and month.
