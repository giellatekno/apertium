#!/usr/bin/env python3
"""Manage Giellatekno's Apertium instances on Cloudflare.

An *instance* (instances.toml) is one Apertium deployment: a Worker named
after the instance (<instance>.<subdomain>.workers.dev) serving the frontend,
with an APy container. Language
pairs live in one R2 bucket per account (apertium-pairs), shared by all
instances: one archive per pair and package version, plus a `current` pointer
per pair. Pairs are kept at the newest version from the Apertium apt repo,
unless a pair is rolled back.

    manage.py list                               instances, and where they're deployed
    manage.py deploy  jorgal test                build and deploy an instance
    manage.py status  jorgal test  [--days 7]    what's deployed, its pairs, request stats
    manage.py logs    jorgal test                live logs (wrangler tail)
    manage.py restart jorgal test                restart APy (it then loads the current pairs)
    manage.py delete  jorgal test                delete the Worker and its container

    manage.py pairs create-bucket test           create the account's pairs bucket
    manage.py pairs delete-bucket test           delete it, with all pairs (asks you to type a phrase)
    manage.py pairs list-available               pairs in the Apertium apt repo
    manage.py pairs list     test                pairs in R2: versions, current, newest
    manage.py pairs update   test [PAIR ...]     upload newer versions, restart instances
    manage.py pairs rollback test PAIR [VERSION] point a pair back to an older version
    manage.py pairs delete   test PAIR VERSION   delete an old version
    manage.py pairs prune    test [--keep N]     delete old versions

    manage.py usage test [--month 2026-11]       requests, container use and estimated costs

    manage.py images list  test                  APy images in the account's registry
    manage.py images prune test                  delete the images no deployed instance uses

Run `manage.py <command> --help` for options. Needs Python 3.11+, Node.js
(npx), podman (or ENGINE=docker), and `npx wrangler login`. See deploy/README.md.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import functools
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent  # deploy/cloudflare
ROOT = HERE.parents[1]  # the umbrella repo
BUILD = HERE / ".build"  # build output and archive cache (gitignored)
IMAGE = "apertium-apy-llm"  # local image tag; deploy/pairs/build.sh uses it too
ENGINE = os.environ.get("ENGINE", "podman")
BUCKET = "apertium-pairs"  # must match r2_buckets in wrangler.jsonc
API = "https://api.cloudflare.com/client/v4"
YARN = ["npx", "-y", "yarn@1.22.22"]
# Package names like apertium-sme-nob; apertium-all-dev matches too but is the
# developer tools metapackage
PAIR_PACKAGE = re.compile(r"^apertium-(\w\w\w-\w\w\w) ")
NOT_PAIRS = {"all-dev"}

# Cloudflare blocks Python's default User-Agent ("Python-urllib/3.x") on
# workers.dev with HTTP 403 "error code: 1010", so every request sends this.
_opener = urllib.request.build_opener()
_opener.addheaders = [("User-Agent", "apertium-giellatekno-manage/1.0")]
urllib.request.install_opener(_opener)


# ------------------------------------------------------------------ config ---


@dataclass
class Account:
    key: str  # "test" / "prod"
    id: str
    name: str


@dataclass
class Instance:
    name: str
    description: str
    pairs: list[str]

    @property
    def worker(self) -> str:
        return self.name

    @property
    def repository(self) -> str:
        """The APy image's repository in the account's Cloudflare registry."""
        return f"{self.name}-apy"

    @property
    def build_dir(self) -> Path:
        return BUILD / self.name


def load_config() -> tuple[dict[str, Account], dict[str, Instance]]:
    with open(HERE / "instances.toml", "rb") as f:
        raw = tomllib.load(f)
    accounts = {
        key: Account(key, a["id"], a.get("name", key))
        for key, a in raw["accounts"].items()
    }
    instances = {
        name: Instance(name, i.get("description", ""), list(i["pairs"]))
        for name, i in raw["instances"].items()
    }
    return accounts, instances


# --------------------------------------------------------------- terminal ---


def info(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


class Fail(Exception):
    pass


class NotFound(Fail):
    pass


def ask(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise Fail(f"needs an answer, but stdin isn't a terminal: {prompt}")
    return input(prompt).strip()


def confirm(prompt: str, assume_yes: bool = False) -> None:
    if assume_yes:
        return
    if ask(f"{prompt} [y/N] ").lower() not in ("y", "yes"):
        raise Fail("aborted")


def confirm_account(account: Account, action: str, assume_yes: bool) -> None:
    """Asks before deploys, rollbacks and deletions, in every account."""
    confirm(f"{action} in the {account.key.upper()} account ({account.name})?", assume_yes)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# --------------------------------------------------------- subprocesses ---


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        capture: bool = False, check: bool = True, quiet: bool = False,
        input: str | None = None) -> subprocess.CompletedProcess:
    if not quiet:
        info(f"$ {' '.join(cmd)}")
    result = subprocess.run(
        cmd, cwd=cwd, env={**os.environ, **(env or {})}, text=True, input=input,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode != 0:
        detail = f"\n{result.stderr.strip()}" if capture and result.stderr else ""
        raise Fail(f"command failed ({result.returncode}): {' '.join(cmd)}{detail}")
    return result


def parse_wrangler_json(stdout: str) -> object:
    """The JSON in wrangler's output. In a terminal, some commands print progress
    lines ("├ Listing", "│") before it, so parse from the first line that starts
    a JSON array or object."""
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(("[", "{")):
            return json.loads("\n".join(lines[i:]))
    raise json.JSONDecodeError("no JSON in wrangler's output", stdout, 0)


def wrangler_json(account: "Account | None", *args: str) -> object:
    """Runs a wrangler command with --json output; retries once if there's no JSON."""
    for attempt in (1, 2):
        result = wrangler(account, *args, "--json", capture=True, quiet=True, check=False)
        try:
            if result.returncode == 0:
                return parse_wrangler_json(result.stdout)
        except json.JSONDecodeError:
            pass
        if attempt == 1:
            time.sleep(2)
    output = (result.stdout + result.stderr).strip()[-1000:] or "(no output)"
    raise Fail(f"`wrangler {' '.join(args)} --json` failed (exit {result.returncode}):\n{output}")


def ensure_node_modules() -> None:
    if not (HERE / "node_modules").is_dir():
        run(["npm", "install", "--silent"], cwd=HERE)


def wrangler(account: Account | None, *args: str, capture: bool = False, check: bool = True,
             quiet: bool = False) -> subprocess.CompletedProcess:
    ensure_node_modules()
    env = {"CLOUDFLARE_ACCOUNT_ID": account.id} if account else {}
    return run([str(HERE / "node_modules" / ".bin" / "wrangler"), *args],
               cwd=HERE, env=env, capture=capture, check=check, quiet=quiet)


# ------------------------------------------------------- Cloudflare API ---


class CloudflareAPI:
    """The REST and GraphQL APIs, with wrangler's login (`wrangler auth token`)."""

    def __init__(self, account: Account):
        self.account = account
        self._token: str | None = None

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = wrangler_json(None, "auth", "token")["token"]
        return self._token

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: object = None, raw: bool = False) -> object:
        url = f"{API}/accounts/{self.account.id}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            payload = e.read()
            if e.code == 404:
                raise NotFound(path) from None
            raise Fail(f"Cloudflare API {method} {path}: HTTP {e.code} {payload[:500]!r}") from None
        if raw:
            return payload
        decoded = json.loads(payload)
        if not decoded.get("success", True):
            raise Fail(f"Cloudflare API {method} {path}: {decoded.get('errors')}")
        return decoded

    def get(self, path: str, **kw) -> object:
        return self.request("GET", path, **kw)["result"]

    def graphql(self, query: str, variables: dict) -> dict:
        req = urllib.request.Request(
            f"{API}/graphql", data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            decoded = json.load(resp)
        if decoded.get("errors"):
            raise Fail(f"GraphQL: {decoded['errors']}")
        return decoded["data"]

    # Workers

    @functools.cached_property
    def workers(self) -> set[str]:
        return {s["id"] for s in self.get("/workers/scripts")}

    @functools.cached_property
    def subdomain(self) -> str | None:
        try:
            return self.get("/workers/subdomain").get("subdomain")
        except Fail:
            return None

    def worker_url(self, instance: Instance) -> str | None:
        return f"https://{instance.worker}.{self.subdomain}.workers.dev" if self.subdomain else None

    def containers_problem(self) -> str | None:
        """Why this account can't use Cloudflare Containers, or None if it can."""
        try:
            self.get("/containers/me")
            return None
        except Fail as e:
            # e.g. HTTP 401 "... Deploying containers requires the Workers Paid plan ..."
            match = re.search(r"Unauthorized: ([^\\\"]*)", str(e))
            return match.group(1).strip() if match else str(e)

    def container_apps(self, instance: Instance) -> list[dict]:
        apps = wrangler_json(self.account, "containers", "list")
        return [a for a in apps if a.get("name", "").startswith(f"{instance.worker}-")]

    # R2

    def bucket_exists(self) -> bool:
        return BUCKET in {b["name"] for b in self.get("/r2/buckets")["buckets"]}

    def create_bucket(self) -> None:
        self.request("POST", "/r2/buckets", body={"name": BUCKET})

    def delete_bucket(self) -> None:
        self.request("DELETE", f"/r2/buckets/{BUCKET}")

    def objects(self, prefix: str = "") -> list[dict]:
        found, cursor = [], None
        while True:
            params = {"per_page": 1000, **({"prefix": prefix} if prefix else {}),
                      **({"cursor": cursor} if cursor else {})}
            resp = self.request("GET", f"/r2/buckets/{BUCKET}/objects", params=params)
            found += resp["result"]
            result_info = resp.get("result_info") or {}
            cursor = result_info.get("cursor")
            if not cursor or not result_info.get("is_truncated"):
                return found

    def get_object(self, key: str) -> bytes:
        return self.request("GET", f"/r2/buckets/{BUCKET}/objects/{urllib.parse.quote(key, safe='/')}", raw=True)

    def delete_object(self, key: str) -> None:
        self.request("DELETE", f"/r2/buckets/{BUCKET}/objects/{urllib.parse.quote(key, safe='/')}")


# ------------------------------------------------------ the apt repo ---


def ensure_image() -> None:
    if run([ENGINE, "image", "exists", IMAGE], check=False, quiet=True).returncode != 0:
        build_image()


@functools.cache
def apt_pairs() -> dict[str, str | None]:
    """Every pair in the Apertium apt repo, with its newest version, as the APy image sees it.

    Asks inside the image because that's where deploy/pairs/build.sh installs
    pairs from: the same repo and Debian release, wherever this runs.
    """
    ensure_image()
    script = """
        apt-get update -qq >/dev/null 2>&1
        apt-cache search apertium- > /tmp/search
        echo '--- search'; cat /tmp/search
        echo '--- policy'
        sed -n 's/^\\(apertium-[a-z0-9_]*-[a-z0-9_]*\\) .*/\\1/p' /tmp/search | xargs apt-cache policy
    """
    out = run([ENGINE, "run", "--rm", "--user", "root", "--entrypoint", "sh", IMAGE, "-c", script],
              capture=True, quiet=True).stdout
    search, _, policy = out.partition("--- policy")
    pairs = {}
    for line in search.splitlines():
        m = PAIR_PACKAGE.match(line)
        if m and m.group(1) not in NOT_PAIRS:
            pairs[m.group(1)] = None
    package = None
    for line in policy.splitlines():
        if not line.startswith(" ") and line.endswith(":"):
            package = line[:-1]
        elif "Candidate:" in line and package:
            pair = package.removeprefix("apertium-")
            if pair in pairs:
                candidate = line.split("Candidate:", 1)[1].strip()
                pairs[pair] = None if candidate == "(none)" else candidate
    return dict(sorted(pairs.items()))


def check_pairs_exist(pairs: list[str]) -> None:
    available = apt_pairs()
    missing = [p for p in pairs if p not in available or not available[p]]
    if missing:
        raise Fail(f"not in the Apertium apt repo: {', '.join(missing)} "
                   f"(see `manage.py pairs list-available`)")


# ------------------------------------------------------- pairs in R2 ---


@dataclass
class PairState:
    """One pair in the account's bucket."""
    pair: str
    versions: list[str] = field(default_factory=list)  # oldest upload first
    sizes: dict[str, int] = field(default_factory=dict)
    current: str | None = None


def r2_pairs(api: CloudflareAPI) -> dict[str, PairState]:
    """pairs/<pair>/<version>.tar.zst and pairs/<pair>/current, by pair."""
    states: dict[str, PairState] = {}
    objects = sorted(api.objects(prefix="pairs/"), key=lambda o: (o.get("last_modified") or "", o["key"]))
    has_current = []
    for obj in objects:
        parts = obj["key"].split("/")
        if len(parts) != 3:
            continue
        _, pair, name = parts
        state = states.setdefault(pair, PairState(pair))
        if name.endswith(".tar.zst"):
            version = name.removesuffix(".tar.zst")
            state.versions.append(version)
            state.sizes[version] = obj.get("size", 0)
        elif name == "current":
            has_current.append(pair)
    for pair in has_current:
        states[pair].current = api.get_object(f"pairs/{pair}/current").decode().strip()
    return states


def pair_status(state: PairState | None, newest: str | None) -> str:
    if not state or not state.current:
        return "not uploaded"
    if newest is None:
        return "no longer in the apt repo"
    if state.current == newest:
        return "up to date"
    if newest in state.versions:
        return f"ROLLED BACK from {newest}"
    return f"update available: {newest}"


def local_archive(pair: str, version: str) -> Path:
    return BUILD / "pairs" / pair / f"{version}.tar.zst"


def build_pair(pair: str) -> tuple[str, Path]:
    """Builds the newest version of a pair (deploy/pairs/build.sh); returns (version, archive)."""
    out = BUILD / "pairs" / pair
    out.mkdir(parents=True, exist_ok=True)
    result = run([str(ROOT / "deploy/pairs/build.sh"), str(out), pair], capture=True,
                 env={"IMAGE": IMAGE, "ENGINE": ENGINE})
    archive = Path(result.stdout.strip().splitlines()[-1])
    info(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else f"Built {archive.name}")
    return archive.name.removesuffix(".tar.zst"), archive


def upload_pair(account: Account, pair: str, version: str, archive: Path) -> None:
    manifest = archive.with_name(f"{version}.manifest.txt")
    wrangler(account, "r2", "object", "put", f"{BUCKET}/pairs/{pair}/{version}.tar.zst",
             "--file", str(archive), "--content-type", "application/zstd", "--remote")
    if manifest.exists():
        wrangler(account, "r2", "object", "put", f"{BUCKET}/pairs/{pair}/{version}.manifest.txt",
                 "--file", str(manifest), "--content-type", "text/plain", "--remote")


def set_current(account: Account, pair: str, version: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:
        f.write(version)
        f.flush()
        wrangler(account, "r2", "object", "put", f"{BUCKET}/pairs/{pair}/current",
                 "--file", f.name, "--content-type", "text/plain", "--remote")
    info(f"{pair}: current is now {version}")


def download_pair(account: Account, pair: str, version: str) -> Path:
    archive = local_archive(pair, version)
    if not archive.exists():
        archive.parent.mkdir(parents=True, exist_ok=True)
        wrangler(account, "r2", "object", "get", f"{BUCKET}/pairs/{pair}/{version}.tar.zst",
                 "--file", str(archive), "--remote")
    return archive


def update_pair(account: Account, api: CloudflareAPI | None, states: dict[str, PairState],
                pair: str, *, upload: bool) -> tuple[str, Path, bool]:
    """Makes sure the pair's newest version is in R2 and current, unless it's rolled back.

    Returns (version to use, local archive, whether `current` changed). With
    upload=False (dry run) nothing is uploaded.
    """
    newest = apt_pairs().get(pair)
    state = states.get(pair)
    if not newest:
        raise Fail(f"{pair} is not in the Apertium apt repo")

    if state and state.current and newest in state.versions:
        if state.current != newest:
            warn(f"{pair} is rolled back to {state.current} (newest: {newest}); keeping that. "
                 f"`manage.py pairs rollback {account.key} {pair} {newest}` undoes it.")
        else:
            info(f"{pair}: {newest} (up to date)")
        return state.current, download_pair(account, pair, state.current), False

    # A version that isn't in R2 yet: newer than what's there (or the first)
    archive = local_archive(pair, newest)
    if archive.exists():
        info(f"{pair}: {newest} (new; built earlier)")
        version = newest
    else:
        info(f"{pair}: building {newest}" + (f" (R2 has {state.current})" if state and state.current else ""))
        version, archive = build_pair(pair)
        if version != newest:
            warn(f"{pair}: apt installed {version}, expected {newest}")
    if not upload:
        info(f"{pair}: dry run, not uploading {version}")
        return version, archive, False
    upload_pair(account, pair, version, archive)
    set_current(account, pair, version)
    states.setdefault(pair, PairState(pair)).versions.append(version)
    states[pair].current = version
    return version, archive, True


# ------------------------------------------------------------- building ---


def check_clean(allow_dirty: bool) -> None:
    for sub in ("apertium-apy", "apertium-html-tools"):
        dirty = run(["git", "-C", str(ROOT / sub), "status", "--porcelain"], capture=True, quiet=True).stdout
        if dirty.strip():
            if allow_dirty:
                warn(f"{sub} has uncommitted changes")
            else:
                raise Fail(f"{sub} has uncommitted changes (--allow-dirty to deploy anyway)")


def build_image() -> None:
    step("Building the APy image")
    result = run([ENGINE, "build", "-t", IMAGE, "-f", str(ROOT / "deploy/apy/Containerfile"), str(ROOT)],
                 capture=True, check=False)
    if result.returncode != 0:
        raise Fail(f"image build failed:\n{result.stdout[-3000:]}{result.stderr[-3000:]}")
    apt_pairs.cache_clear()


def push_image(account: Account, instance: Instance) -> str:
    """Pushes the local APy image to the account's Cloudflare registry; returns it pinned by digest.

    We push ourselves (instead of letting wrangler build and push) so that the
    deployed image is the one the pairs and the frontend were built with, and
    because wrangler reads the digest from the local engine: with podman behind
    the docker CLI, the pushed image gets a different digest and the deploy
    fails ("The image is invalid or does not exist in this account").
    """
    step("Pushing the APy image to Cloudflare's registry")
    creds = registry_credentials(account, push=True)
    registry = creds["registry_host"]
    repository = f"{registry}/{account.id}/{instance.repository}"
    tag = f"{repository}:{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{git_rev(ROOT)}"
    with tempfile.TemporaryDirectory() as tmp:
        auth, digest_file = Path(tmp) / "auth.json", Path(tmp) / "digest"
        # A throwaway auth file, so the temporary password isn't stored anywhere
        run([ENGINE, "login", "--authfile", str(auth), "--username", creds["username"], "--password-stdin", registry],
            input=creds["password"], capture=True, quiet=True)
        run([ENGINE, "tag", IMAGE, tag], quiet=True)
        try:
            run([ENGINE, "push", "--authfile", str(auth), "--digestfile", str(digest_file), tag], capture=True)
        finally:
            run([ENGINE, "rmi", tag], capture=True, quiet=True, check=False)
        digest = digest_file.read_text().strip()
    image = f"{repository}@{digest}"
    info(f"Pushed {image}")
    return image


def registry_credentials(account: Account, push: bool = False) -> dict:
    flags = ["--push", "--pull"] if push else ["--pull"]
    return wrangler_json(account, "containers", "registries", "credentials", "registry.cloudflare.com",
                         *flags, "--expiration-minutes", "15")


def registry_images(account: Account) -> dict[str, list[str]]:
    """Repository -> tags in the account's Cloudflare registry."""
    return {i["name"]: list(i.get("tags", [])) for i in wrangler_json(account, "containers", "images", "list")}


def registry_digest(account: Account, creds: dict, repository: str, tag: str) -> str | None:
    """The manifest digest a tag points to."""
    import base64
    auth = base64.b64encode(f"{creds['username']}:{creds['password']}".encode()).decode()
    req = urllib.request.Request(
        f"https://{creds['registry_host']}/v2/{account.id}/{repository}/manifests/{tag}", method="HEAD",
        headers={"Authorization": f"Basic {auth}", "Accept": ", ".join([
            "application/vnd.oci.image.manifest.v1+json", "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.docker.distribution.manifest.list.v2+json"])})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.headers.get("Docker-Content-Digest")
    except urllib.error.HTTPError:
        return None


def deployed_image(api: CloudflareAPI, instance: Instance) -> str | None:
    """The image the deployed instance runs (from its /build-info.json), or None."""
    url = api.worker_url(instance)
    build = fetch_json(f"{url}/build-info.json") if url and instance.worker in api.workers else None
    return (build or {}).get("image")


def git_rev(path: Path) -> str:
    return run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"], capture=True, quiet=True).stdout.strip()


def build_frontend(instance: Instance, account: Account, archives: dict[str, tuple[str, Path]],
                   url: str | None) -> Path:
    """Builds html-tools against a local APy with the given pairs, into .build/<instance>/dist."""
    step("Building the frontend against a local APy with " +
         ", ".join(f"{p} {v}" for p, (v, _) in archives.items()))
    with tempfile.TemporaryDirectory() as tmp:
        pairs_dir = Path(tmp) / "pairs"
        pairs_dir.mkdir()
        for pair, (_, archive) in archives.items():
            shutil.copy(archive, pairs_dir / f"{pair}.tar.zst")
        container = run([
            ENGINE, "run", "-d", "--rm", "-p", "127.0.0.1::2737", "-v", f"{pairs_dir}:/pairs:ro,Z",
            "-e", f"PAIRS={' '.join(archives)}", "-e", "PAIRS_URL=file:///pairs", IMAGE,
        ], capture=True, quiet=True).stdout.strip()
        try:
            port = run([ENGINE, "port", container, "2737"], capture=True, quiet=True).stdout.split()[0].rsplit(":", 1)[1]
            apy = f"http://127.0.0.1:{port}"
            info(f"Local APy serves: {' '.join(wait_for_apy(apy, container))}")

            src = Path(tmp) / "html-tools"
            src.mkdir()
            # The committed HEAD of the fork, with our config.ts
            tarball = subprocess.run(["git", "-C", str(ROOT / "apertium-html-tools"), "archive", "HEAD"],
                                     check=True, capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", str(src)], input=tarball, check=True)
            shutil.copy(ROOT / "deploy/html-tools/config.ts", src / "config.ts")
            node_modules = ROOT / "apertium-html-tools/node_modules"
            if node_modules.is_dir():
                (src / "node_modules").symlink_to(node_modules)
            else:
                run([*YARN, "install", "--frozen-lockfile", "--silent"], cwd=src)
            run([*YARN, "-s", "build", "--prod"], cwd=src, capture=True,
                env={"APY_BUILD_URL": apy, "HTML_URL": f"{url or 'https://example.invalid'}/"})

            dist = instance.build_dir / "dist"
            shutil.rmtree(dist, ignore_errors=True)
            shutil.copytree(src / "dist", dist)
        finally:
            run([ENGINE, "rm", "-f", container], capture=True, quiet=True, check=False)

    # What this deploy contains; check a live site at /build-info.json
    (dist / "build-info.json").write_text(json.dumps({
        "instance": instance.name,
        "account": account.key,
        "pairs": {pair: version for pair, (version, _) in archives.items()},
        "apertium-apy": git_rev(ROOT / "apertium-apy"),
        "apertium-html-tools": git_rev(ROOT / "apertium-html-tools"),
        "apertium": git_rev(ROOT),
        "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, indent=2) + "\n")
    info(f"Frontend built into {dist.relative_to(ROOT)}")
    return dist


def wait_for_apy(apy: str, container: str, timeout: float = 90) -> list[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{apy}/listPairs", timeout=2) as resp:
                data = json.load(resp)["responseData"]
                return [f"{p['sourceLanguage']}-{p['targetLanguage']}" for p in data]
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
            time.sleep(0.5)
    logs = run([ENGINE, "logs", container], capture=True, quiet=True, check=False)
    raise Fail(f"local APy didn't start:\n{logs.stdout}{logs.stderr}")


def strip_jsonc(text: str) -> str:
    """Removes // and /* */ comments and trailing commas outside strings."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def wrangler_config(instance: Instance, image: str | None) -> Path:
    """wrangler.jsonc with the instance's Worker name, pairs and frontend, in .build/<instance>/.

    With `image` (pushed by push_image), the container uses that image instead
    of wrangler building one from the Containerfile.
    """
    base = json.loads(strip_jsonc((HERE / "wrangler.jsonc").read_text()))
    cfg = copy.deepcopy(base)
    out_dir = instance.build_dir

    def rel(path: str) -> str:  # paths in the config are relative to the config file
        return os.path.relpath(HERE / path, out_dir)

    cfg.pop("$schema", None)
    cfg["name"] = instance.worker
    cfg["main"] = rel(base["main"])
    cfg["assets"]["directory"] = "./dist"
    for container in cfg["containers"]:
        for name, spec in container["images"].items():
            if image:
                container["images"][name] = {"image": image}
            else:
                spec["dockerfile"] = rel(spec["dockerfile"])
                spec["build_context"] = rel(spec["build_context"])
    cfg["vars"]["PAIRS"] = " ".join(instance.pairs)

    path = out_dir / "wrangler.json"
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return path


def type_check() -> None:
    step("Type-checking the Worker")
    wrangler(None, "types", capture=True, quiet=True)
    run(["npx", "tsc", "--noEmit"], cwd=HERE)


def secrets_file(account: Account, create: bool) -> Path:
    """The Worker secrets for an account (gitignored), created on request."""
    path = HERE / f"secrets.{account.key}.env"
    if path.exists():
        return path
    if not create:
        raise Fail(f"{path.relative_to(ROOT)} is missing")
    info(f"\n{path.relative_to(ROOT)} doesn't exist yet. It holds the Worker secrets for the "
         f"{account.key} account (set on every deploy).")
    if not sys.stdin.isatty():
        raise Fail(f"create {path.relative_to(ROOT)} (see deploy/README.md)")
    key = getpass.getpass("APY_LLM_API_KEY (the UiT LLM gateway key, not shown): ").strip()
    if not key:
        raise Fail("no key given")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"APY_LLM_API_KEY={key}\nADMIN_TOKEN={secrets.token_hex(32)}\n")
    info(f"Wrote {path.relative_to(ROOT)} (ADMIN_TOKEN generated)")
    return path


def admin_token(account: Account) -> str | None:
    path = HERE / f"secrets.{account.key}.env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("ADMIN_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None


def restart_apy(account: Account, api: CloudflareAPI, instance: Instance) -> bool:
    url, token = api.worker_url(instance), admin_token(account)
    if not url or not token:
        warn(f"can't restart {instance.name}: no Worker URL or no ADMIN_TOKEN in the secrets file")
        return False
    req = urllib.request.Request(f"{url}/admin/restart", method="POST",
                                 headers={"Authorization": f"Bearer {token}"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                info(f"{instance.name}: {resp.read().decode().strip()}")
                return True
        except (urllib.error.URLError, TimeoutError) as e:
            # A brand-new workers.dev URL can take a moment to answer
            if attempt == 5:
                warn(f"restarting {instance.name} failed: {e}")
                return False
            time.sleep(5)
    return False


def fetch_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError):
        return None


def restart_users(account: Account, api: CloudflareAPI, instances: dict[str, Instance],
                  pairs: set[str]) -> None:
    """Restarts the deployed instances that serve any of `pairs`."""
    users = [i for i in instances.values() if set(i.pairs) & pairs and i.worker in api.workers]
    if not users:
        info("No deployed instance uses these pairs.")
    for instance in users:
        restart_apy(account, api, instance)


# ------------------------------------------------------------- commands ---


def cmd_deploy(args, account: Account, instance: Instance, instances: dict[str, Instance]) -> None:
    dry = args.dry_run
    check_clean(args.allow_dirty)
    api = CloudflareAPI(account)
    problem = api.containers_problem()
    if problem:
        message = (f"the {account.key} account ({account.name}) can't run containers: {problem}\n"
                   f"Plans: https://dash.cloudflare.com/{account.id}/workers/plans")
        if not dry:
            raise Fail(message)
        warn(f"{message}\n(dry run: continuing; a real deploy stops here)")
    if not dry:
        confirm_account(account, f"Deploy {instance.name}", args.yes)
    secrets_path = None if dry else secrets_file(account, create=True)

    build_image()
    step(f"Checking {instance.name}'s pairs in the apt repo: {', '.join(instance.pairs)}")
    check_pairs_exist(instance.pairs)

    step(f"R2 bucket {BUCKET}")
    if api.bucket_exists():
        states = r2_pairs(api)
        info("Exists")
    elif dry:
        states = {}
        info("Doesn't exist (dry run: not creating it)")
    else:
        api.create_bucket()
        states = {}
        info("Created")

    step("Language pairs (newest version unless rolled back)")
    archives, changed = {}, set()
    for pair in instance.pairs:
        version, archive, did_change = update_pair(account, api, states, pair, upload=not dry)
        archives[pair] = (version, archive)
        if did_change:
            changed.add(pair)

    url = api.worker_url(instance)
    build_frontend(instance, account, archives, url)
    type_check()

    if dry:
        # A placeholder image: a dry run doesn't check it, and no Docker build is needed
        placeholder = f"registry.cloudflare.com/{account.id}/{instance.repository}@sha256:{'0' * 64}"
        config = wrangler_config(instance, image=placeholder)
        step("Dry run: wrangler deploy --dry-run (nothing is uploaded)")
        with tempfile.TemporaryDirectory() as tmp:
            wrangler(account, "deploy", "--config", str(config), "--dry-run", "--outdir", tmp)
        return

    image = push_image(account, instance)
    build_info = instance.build_dir / "dist" / "build-info.json"
    build_info.write_text(json.dumps({**json.loads(build_info.read_text()), "image": image}, indent=2) + "\n")
    config = wrangler_config(instance, image=image)
    step(f"Deploying the Worker {instance.worker}")
    wrangler(account, "deploy", "--config", str(config), "--secrets-file", str(secrets_path))

    step("Restarting APy (so it runs the new image and pairs)")
    api = CloudflareAPI(account)  # fresh: the Worker list and subdomain may be new
    restart_apy(account, api, instance)
    others = {name: i for name, i in instances.items() if name != instance.name}
    if changed and any(set(i.pairs) & changed for i in others.values()):
        step(f"Restarting other instances that use {', '.join(sorted(changed))}")
        restart_users(account, api, others, changed)
    url = api.worker_url(instance)
    info(f"\nDeployed {instance.name} to {account.key}: {url}\nContents: {url}/build-info.json")


def cmd_list(args, accounts: dict[str, Account], instances: dict[str, Instance]) -> None:
    apis = {key: CloudflareAPI(a) for key, a in accounts.items()}
    for instance in instances.values():
        info(f"{instance.name}: {instance.description}")
        info(f"  pairs: {', '.join(instance.pairs)}")
        for key, api in apis.items():
            try:
                where = (api.worker_url(instance) or "deployed") if instance.worker in api.workers else "not deployed"
            except Fail as e:
                where = f"? ({e})"
            info(f"  {key}: {where}")


def cmd_status(args, account: Account, instance: Instance) -> None:
    api = CloudflareAPI(account)
    url = api.worker_url(instance)
    info(f"{instance.name} in {account.key} ({account.name})")

    step("Worker")
    build = None
    if instance.worker not in api.workers:
        info(f"{instance.worker} is not deployed")
    else:
        info(f"{instance.worker}: {url}")
        build = fetch_json(f"{url}/build-info.json") if url else None
        if build:
            for key, value in build.items():
                if isinstance(value, dict):
                    value = ", ".join(f"{k} {v}" for k, v in value.items())
                info(f"  {key}: {value}")
        else:
            info("  (no /build-info.json)")
        try:
            for d in api.get(f"/workers/scripts/{instance.worker}/deployments").get("deployments", [])[:3]:
                info(f"  deployed {d.get('created_on', '?')[:19]}")
        except Fail:
            pass

    step("APy container")
    apps = api.container_apps(instance)
    for app in apps:
        info(f"{app['name']}: {app.get('state')}, {app.get('instances', 0)} running, "
             f"updated {str(app.get('updated_at', ''))[:19]}")
    if not apps:
        info("none")

    step(f"Language pairs (R2 bucket {BUCKET})")
    states = r2_pairs(api) if api.bucket_exists() else {}
    available = apt_pairs()
    for pair in instance.pairs:
        state = states.get(pair)
        info(f"{pair}: {state.current if state and state.current else '-'}  "
             f"[{pair_status(state, available.get(pair))}]")
    if build and isinstance(build.get("pairs"), dict) and sorted(build["pairs"]) != sorted(instance.pairs):
        warn(f"the deployed frontend was built for {', '.join(build['pairs'])}; "
             f"instances.toml says {', '.join(instance.pairs)}. Deploy to update it.")

    step(f"Requests, last {args.days} days")
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = """query($account: String!, $since: Time!, $script: String!) {
      viewer { accounts(filter: {accountTag: $account}) {
        workersInvocationsAdaptive(limit: 1000, orderBy: [date_ASC],
            filter: {datetime_geq: $since, scriptName: $script}) {
          dimensions { date }
          sum { requests errors subrequests }
          quantiles { cpuTimeP50 cpuTimeP99 }
    } } } }"""
    try:
        rows = api.graphql(query, {"account": account.id, "since": since, "script": instance.worker})
        rows = rows["viewer"]["accounts"][0]["workersInvocationsAdaptive"]
    except (Fail, urllib.error.URLError) as e:
        info(f"(stats unavailable: {e})")
        return
    if not rows:
        info("no requests")
        return
    info(f"{'date':<12}{'requests':>10}{'errors':>8}{'subrequests':>13}{'CPU p50':>10}{'p99':>8}")
    for r in rows:
        s, q = r["sum"], r["quantiles"]
        info(f"{r['dimensions']['date']:<12}{s['requests']:>10}{s['errors']:>8}{s['subrequests']:>13}"
             f"{q['cpuTimeP50'] / 1000:>8.1f}ms{q['cpuTimeP99'] / 1000:>6.1f}ms")
    info(f"{'total':<12}{sum(r['sum']['requests'] for r in rows):>10}{sum(r['sum']['errors'] for r in rows):>8}")
    info("(Worker requests: page loads and API calls. Subrequests include the container's"
         " R2 and LLM calls.)")


def cmd_logs(args, account: Account, instance: Instance) -> None:
    wrangler(account, "tail", instance.worker, "--format", "pretty", check=False)


def cmd_restart(args, account: Account, instance: Instance) -> None:
    if not restart_apy(account, CloudflareAPI(account), instance):
        raise Fail("restart failed")


DELETE_BUCKET_PHRASE = "yes, delete the bucket"


def delete_bucket(account: Account, api: CloudflareAPI, instances: dict[str, Instance]) -> None:
    """Deletes the pairs bucket and everything in it, after the user types DELETE_BUCKET_PHRASE."""
    deployed = [i.name for i in instances.values() if i.worker in api.workers]
    info(f"\nThis will delete the entire bucket {BUCKET} in the {account.key.upper()} account "
         f"({account.name}),\nwhich stores all the language pairs for all instances. "
         f"All running instances will stop working!")
    if deployed:
        info(f"Instances deployed in {account.key}: {', '.join(deployed)}")
    if ask(f'To go through with deletion, type "{DELETE_BUCKET_PHRASE}": ') != DELETE_BUCKET_PHRASE:
        raise Fail("aborted; the bucket was not deleted")
    objects = api.objects()
    for obj in objects:
        api.delete_object(obj["key"])
    api.delete_bucket()
    info(f"Deleted bucket {BUCKET} ({len(objects)} objects)")


def cmd_delete(args, account: Account, instance: Instance, instances: dict[str, Instance]) -> None:
    api = CloudflareAPI(account)
    has_worker = instance.worker in api.workers
    apps = api.container_apps(instance)
    others = [i.name for i in instances.values() if i.name != instance.name and i.worker in api.workers]
    with_bucket = args.with_bucket and api.bucket_exists()
    if with_bucket and others:
        raise Fail(f"--with-bucket: other instances in {account.key} still use {BUCKET}: {', '.join(others)}")

    tags = registry_images(account).get(instance.repository, [])
    targets = ([f"Worker {instance.worker} (and its Durable Object)"] if has_worker else []) + \
              [f"container application {a['name']}" for a in apps] + \
              ([f"{len(tags)} APy image tags in the registry ({instance.repository})"] if tags else []) + \
              ([f"R2 bucket {BUCKET} with all language pairs (asks separately)"] if with_bucket else [])
    if not targets:
        info(f"Nothing of {instance.name} exists in {account.key}.")
        return
    info(f"This deletes from the {account.key.upper()} account ({account.name}):")
    for t in targets:
        info(f"  - {t}")
    if not args.yes and ask(f"Type the instance name ({instance.name}) to confirm: ") != instance.name:
        raise Fail("aborted")

    if has_worker:
        wrangler(account, "delete", instance.worker, "--force")
    # Deleting the Worker may leave its container application behind
    for app in api.container_apps(instance):
        wrangler(account, "containers", "delete", app["id"])
    for tag in tags:
        delete_image_tag(account, instance.repository, tag)
    if with_bucket:
        delete_bucket(account, CloudflareAPI(account), instances)
    elif api.bucket_exists():
        info(f"Kept the shared bucket {BUCKET} (--with-bucket deletes it once no instance uses it)")


def cmd_pairs(args, accounts: dict[str, Account], instances: dict[str, Instance]) -> None:
    used_by: dict[str, list[str]] = {}
    for i in instances.values():
        for pair in i.pairs:
            used_by.setdefault(pair, []).append(i.name)

    if args.action == "list-available":
        available = apt_pairs()
        for pair, version in available.items():
            users = f"  <- {', '.join(used_by[pair])}" if pair in used_by else ""
            info(f"{pair:<10}{version or '(no candidate)'}{users}")
        info(f"({len(available)} pairs in the Apertium apt repo, as the APy image sees it)")
        return

    account = accounts[args.account]
    api = CloudflareAPI(account)
    exists = api.bucket_exists()

    if args.action == "create-bucket":
        if exists:
            info(f"Bucket {BUCKET} already exists in {account.key}.")
            return
        api.create_bucket()
        info(f"Created bucket {BUCKET} in {account.key} ({account.name}).")
        return
    if not exists:
        raise Fail(f"bucket {BUCKET} doesn't exist in {account.key} yet: "
                   f"`manage.py pairs create-bucket {account.key}` creates it (so does a deploy)")
    if args.action == "delete-bucket":
        delete_bucket(account, api, instances)
        return

    states = r2_pairs(api)

    if args.action == "list":
        available = apt_pairs()
        for pair in sorted(set(states) | set(used_by)):
            state = states.get(pair)
            users = ", ".join(used_by.get(pair, [])) or "no instance"
            info(f"{pair}  [{pair_status(state, available.get(pair))}]  used by: {users}")
            for version in reversed(state.versions if state else []):
                mark = "*" if state.current == version else " "
                info(f"   {mark} {version}  {human_size(state.sizes.get(version, 0)):>8}")
        info("(* = current)")

    elif args.action == "update":
        pairs = args.pairs or sorted(used_by)
        check_pairs_exist(pairs)
        # No confirmation, even in prod: updating only moves pairs to the newest
        # version, which is where they're meant to be anyway
        changed = set()
        for pair in pairs:
            _, _, did_change = update_pair(account, api, states, pair, upload=True)
            if did_change:
                changed.add(pair)
        if changed:
            step(f"Restarting instances that use {', '.join(sorted(changed))}")
            restart_users(account, api, instances, changed)
        else:
            info("Nothing to update.")

    elif args.action == "rollback":
        if len(args.pairs) not in (1, 2):
            raise Fail("usage: pairs rollback ACCOUNT PAIR [VERSION]")
        pair = args.pairs[0]
        state = states.get(pair)
        if not state or not state.current:
            raise Fail(f"{pair} isn't in {BUCKET}")
        if len(args.pairs) == 2:
            version = args.pairs[1]
            if version not in state.versions:
                raise Fail(f"{pair} has no version {version}: {', '.join(state.versions)}")
        else:
            older = state.versions[:state.versions.index(state.current)]
            if not older:
                raise Fail(f"{pair} has no older version than {state.current}")
            version = older[-1]
        newest = apt_pairs().get(pair)
        info(f"{pair}: {state.current} -> {version}")
        if version != newest:
            info(f"Deploys keep it there until a version newer than {newest} is released, "
                 f"or until you roll forward to {newest}.")
        confirm_account(account, f"Switch {pair} to {version}", args.yes)
        set_current(account, pair, version)
        restart_users(account, api, instances, {pair})

    elif args.action == "delete":
        if len(args.pairs) != 2:
            raise Fail("usage: pairs delete ACCOUNT PAIR VERSION")
        pair, version = args.pairs
        state = states.get(pair)
        if not state or version not in state.versions:
            raise Fail(f"{pair} {version} isn't in {BUCKET}")
        if version == state.current:
            raise Fail("that's the current version; roll back or update first")
        confirm_account(account, f"Delete {pair} {version}", args.yes)
        delete_pair_version(api, pair, version)

    elif args.action == "prune":
        doomed = []
        for pair, state in states.items():
            # Keep the current version and the newest --keep others
            others = [v for v in state.versions if v != state.current]
            doomed += [(pair, v) for v in others[:max(len(others) - args.keep, 0)]]
        if not doomed:
            info("Nothing to prune.")
            return
        for pair, version in doomed:
            info(f"  {pair} {version}")
        confirm_account(account, f"Delete these {len(doomed)} versions", args.yes)
        for pair, version in doomed:
            delete_pair_version(api, pair, version)


# ------------------------------------------------------------------ usage ---

# Cloudflare's list prices (USD), from developers.cloudflare.com, 2026-11. The
# included amounts are per account and month, on the Workers Paid plan.
PLAN_PER_MONTH = 5.00
PRICES = {
    # name: (unit price, included per month, unit)
    "container_memory": (0.0000025, 25 * 3600, "GiB-s"),       # 25 GiB-hours
    "container_cpu": (0.000020, 375 * 60, "vCPU-s"),           # 375 vCPU-minutes
    "container_disk": (0.00000007, 200 * 3600, "GB-s"),        # 200 GB-hours
    "container_egress": (0.025, 1000, "GB"),                   # North America and Europe
    "worker_requests": (0.30 / 1e6, 10e6, "requests"),
    "worker_cpu": (0.02 / 1e6, 30e6, "CPU-ms"),
    "do_requests": (0.15 / 1e6, 1e6, "requests"),
    "r2_storage": (0.015, 10, "GB-month"),
    "r2_class_a": (4.50 / 1e6, 1e6, "operations"),
    "r2_class_b": (0.36 / 1e6, 10e6, "operations"),
}
R2_CLASS_A = ("Put", "Copy", "List", "Create", "Upload", "Complete")
R2_CLASS_B = ("Get", "Head")


def container_memory_mib() -> int:
    """The container's memory, from APY_RESOURCES in src/index.ts."""
    match = re.search(r"APY_RESOURCES\s*=\s*\{[^}]*memoryMib:\s*(\d+)", (HERE / "src/index.ts").read_text())
    return int(match.group(1)) if match else 2048


# Cloudflare's analytics go back about a month ("cannot request data older than 4w4d")
ANALYTICS_RETENTION = dt.timedelta(days=31)


def month_bounds(month: str | None) -> tuple[dt.datetime, dt.datetime, float]:
    """(since, until, fraction of the month that this covers).

    The current month is covered up to now; a month that's partly older than
    Cloudflare keeps analytics for is covered from that point."""
    now = dt.datetime.now(dt.timezone.utc)
    if month:
        try:
            start = dt.datetime.strptime(month, "%Y-%m").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            raise Fail(f"--month: expected YYYY-MM, got {month!r}") from None
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    if start > now:
        raise Fail(f"{month} hasn't started yet")
    since, until = max(start, now - ANALYTICS_RETENTION), min(end, now)
    if since >= until:
        raise Fail(f"Cloudflare keeps analytics for about a month only; {month} is too old")
    return since, until, (until - since) / (end - start)


USAGE_QUERY = """query($account: String!, $since: Time!, $until: Time!) {
  viewer { accounts(filter: {accountTag: $account}) {
    workers: workersInvocationsAdaptive(limit: 10000, filter: {datetime_geq: $since, datetime_lt: $until}) {
      dimensions { scriptName } sum { requests errors cpuTimeUs } }
    assets: workersAssetsRequestsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $since, datetime_lt: $until}) {
      dimensions { hostname } sum { requests } }
    containers: containersUsageAdaptiveGroups(limit: 10000, filter: {datetime_geq: $since, datetime_lt: $until}) {
      dimensions { applicationId } sum { allocatedMemory allocatedDisk cpuTimeSec txBytes } }
    peaks: containersMetricsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $since, datetime_lt: $until}) {
      dimensions { applicationId } max { memory cpuUtilization } }
    durableObjects: durableObjectsInvocationsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $since, datetime_lt: $until}) {
      dimensions { scriptName } sum { requests } }
    r2Storage: r2StorageAdaptiveGroups(limit: 10000, filter: {datetime_geq: $since, datetime_lt: $until}) {
      dimensions { bucketName } max { payloadSize objectCount } }
    r2Operations: r2OperationsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $since, datetime_lt: $until}) {
      dimensions { bucketName actionType } sum { requests } }
  } } }"""


def cmd_usage(args, accounts: dict[str, Account], instances: dict[str, Instance]) -> None:
    account = accounts[args.account]
    api = CloudflareAPI(account)
    since, until, covered = month_bounds(args.month)
    data = api.graphql(USAGE_QUERY, {
        "account": account.id,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "until": until.strftime("%Y-%m-%dT%H:%M:%SZ")})
    data = data["viewer"]["accounts"][0]

    def by(rows: list[dict], key: str) -> dict:
        return {r["dimensions"][key]: r for r in rows}

    workers, assets = by(data["workers"], "scriptName"), by(data["assets"], "hostname")
    usage = {r["dimensions"]["applicationId"].replace("-", ""): r["sum"] for r in data["containers"]}
    peaks = {r["dimensions"]["applicationId"].replace("-", ""): r["max"] for r in data["peaks"]}
    do_requests = {r["dimensions"]["scriptName"]: r["sum"]["requests"] for r in data["durableObjects"]}
    problem = api.containers_problem()
    apps = {} if problem else {a["name"]: a["id"].replace("-", "") for a in wrangler_json(account, "containers", "list")}
    memory_mib = container_memory_mib()

    def quantities(container: dict, worker: dict | None, dos: int) -> dict[str, float]:
        return {
            "container_memory": container.get("allocatedMemory", 0) / 2**30,
            "container_cpu": container.get("cpuTimeSec", 0),
            "container_disk": container.get("allocatedDisk", 0) / 1e9,
            "container_egress": container.get("txBytes", 0) / 1e9,
            "worker_requests": (worker or {}).get("requests", 0),
            "worker_cpu": (worker or {}).get("cpuTimeUs", 0) / 1000,
            "do_requests": dos,
        }

    def cost(q: dict[str, float]) -> float:  # before the included amounts
        return sum(PRICES[k][0] * v for k, v in q.items())

    month = (args.month or since.strftime("%Y-%m"))
    period = f"{since:%Y-%m-%d %H:%M} to {until:%Y-%m-%d %H:%M} UTC"
    so_far = "whole month" if covered >= 1 else f"{period}, {covered:.0%} of the month"
    info(f"{account.key} ({account.name}), {month}: {so_far} (container memory {memory_mib} MiB)")
    if since.day != 1 or since.hour or since.minute:
        info("Note: Cloudflare keeps analytics for about a month, so this starts later than the month does.")
    if problem:
        info(f"Note: this account can't run containers yet: {problem}")

    totals: dict[str, float] = {k: 0.0 for k in PRICES}
    header = f"{'instance':<20}{'files':>8}{'API req':>9}{'errors':>8}{'running':>10}{'peak mem':>10}{'peak CPU':>10}{'cost':>9}"
    info(f"\n{header}")
    rows = []
    seen_apps = set()
    for instance in instances.values():
        app_id = next((i for n, i in apps.items() if n.startswith(f"{instance.worker}-")), None)
        seen_apps.add(app_id)
        container, peak = usage.get(app_id or "", {}), peaks.get(app_id or "", {})
        worker = workers.get(instance.worker, {}).get("sum")
        files = sum(r["sum"]["requests"] for h, r in assets.items() if h.split(".")[0] == instance.worker)
        rows.append((instance.name, files, worker, container, peak, do_requests.get(instance.worker, 0)))
    # Containers of deleted or renamed instances still cost what they used. The
    # account may also run other projects' containers: those are left out.
    names = {i: n for n, i in apps.items()}
    others = []
    for app_id, container in usage.items():
        if app_id in seen_apps:
            continue
        if app_id in names and not names[app_id].endswith("-apy"):
            others.append(names[app_id])
        else:
            label = names.get(app_id) or f"(deleted {app_id[:8]})"
            rows.append((label, 0, None, container, peaks.get(app_id, {}), 0))

    for name, files, worker, container, peak, dos in rows:
        q = quantities(container, worker, dos)
        for k, v in q.items():
            totals[k] += v
        hours = container.get("allocatedMemory", 0) / (memory_mib * 2**20) / 3600
        mem = f"{peak['memory'] / 2**20:.0f} MiB" if peak.get("memory") else "-"
        cpu = f"{peak['cpuUtilization'] * 100:.0f}%" if peak.get("cpuUtilization") is not None and peak else "-"
        w = worker or {}
        info(f"{name:<20}{files:>8}{w.get('requests', 0):>9}{w.get('errors', 0):>8}"
             f"{hours:>9.1f}h{mem:>10}{cpu:>10}{'$%.2f' % cost(q):>9}")
    info("(files: static file requests, i.e. page loads and their scripts and images; API req: /apy/*;\n"
         " running: container hours; cost: before the included amounts; deleted: containers that\n"
         " no longer exist, e.g. of renamed or deleted instances)")
    if others:
        info(f"Not counted (other projects' containers in this account): {', '.join(sorted(set(others)))}")

    # R2: the shared pairs bucket only
    stored = sum(r["max"]["payloadSize"] for r in data["r2Storage"] if r["dimensions"]["bucketName"] == BUCKET)
    for r in data["r2Operations"]:
        if r["dimensions"]["bucketName"] != BUCKET:
            continue
        action = r["dimensions"]["actionType"]
        if action.startswith(R2_CLASS_A):
            totals["r2_class_a"] += r["sum"]["requests"]
        elif action.startswith(R2_CLASS_B):
            totals["r2_class_b"] += r["sum"]["requests"]
    totals["r2_storage"] = stored / 1e9 * covered  # GB-months so far, at the current size
    info(f"\nR2 bucket {BUCKET}: {human_size(stored)} stored, "
         f"{totals['r2_class_a']:.0f} class A and {totals['r2_class_b']:.0f} class B operations")

    def bill(q: dict[str, float]) -> tuple[float, list[str]]:
        lines, total = [], PLAN_PER_MONTH
        for k, v in q.items():
            price, included, unit = PRICES[k]
            charged = max(0.0, v - included) * price
            total += charged
            if v:
                lines.append(f"  {k:<18}{v:>12,.0f} {unit:<11}{'included %s' % f'{included:,.0f}':>22}{'$%.2f' % charged:>9}")
        return total, lines

    total, lines = bill(totals)
    info(f"\nEstimate for {month}, {so_far}: the Workers Paid plan (${PLAN_PER_MONTH:.2f}) plus Apertium's usage\n"
         f"beyond what the plan includes (other projects in the account share the included amounts):")
    for line in lines:
        info(line)
    info(f"  {'total':<18}{'':>45}{'$%.2f' % total:>9}")
    if covered < 1:
        projected, _ = bill({k: v / covered for k, v in totals.items()})
        info(f"Projected for the whole month, at the same rate as {period}: ${projected:.2f}")
    if problem:
        info("(This account isn't on Workers Paid yet; the estimate is what it would cost on it.)")
    info("(List prices; the actual invoice is in the dashboard, under Manage Account -> Billing. "
         "Not included: Durable Object duration, Workers Logs, taxes.)")


def delete_image_tag(account: Account, repository: str, tag: str) -> None:
    result = wrangler(account, "containers", "images", "delete", f"{repository}:{tag}",
                      capture=True, quiet=True, check=False)
    if result.returncode == 0:
        info(f"Deleted {repository}:{tag}")
    else:
        # Tags sharing a digest go together: deleting one deletes the image
        warn(f"{repository}:{tag}: {(result.stderr or result.stdout).strip().splitlines()[-1:]}")


def cmd_images(args, accounts: dict[str, Account], instances: dict[str, Instance]) -> None:
    account = accounts[args.account]
    api = CloudflareAPI(account)
    repos = registry_images(account)
    creds = registry_credentials(account)

    keep, doomed = [], []  # (repository, tag, note)
    for instance in instances.values():
        tags = repos.get(instance.repository, [])
        if not tags:
            continue
        deployed = instance.worker in api.workers
        image = deployed_image(api, instance) if deployed else None
        in_use = image.rsplit("@", 1)[1] if image and "@" in image else None
        for tag in sorted(tags):
            digest = registry_digest(account, creds, instance.repository, tag)
            short = (digest or "?")[:19]
            if deployed and not in_use:
                keep.append((instance.repository, tag, f"{short}  kept: deployed image unknown "
                                                      f"(deployed before build-info had it; redeploy)"))
            elif digest and digest == in_use:
                keep.append((instance.repository, tag, f"{short}  deployed"))
            else:
                doomed.append((instance.repository, tag, f"{short}  " +
                               ("unused" if deployed else "instance not deployed")))
    # The account has other images too; only "-apy" ones are ours. Mention those
    # that belong to no instance (left over from a renamed or removed instance).
    others = sorted(r for r in repos if r.endswith("-apy") and r not in {i.repository for i in instances.values()})

    for repo, tag, note in keep + doomed:
        info(f"{repo}:{tag}  {note}")
    if others:
        info(f"APy repositories of no instance in instances.toml (left alone): {', '.join(others)}")
    if args.action == "list":
        return
    if not doomed:
        info("Nothing to prune.")
        return
    confirm_account(account, f"Delete the {len(doomed)} unused image tags", args.yes)
    for repo, tag, _ in doomed:
        delete_image_tag(account, repo, tag)


def delete_pair_version(api: CloudflareAPI, pair: str, version: str) -> None:
    for name in (f"{version}.tar.zst", f"{version}.manifest.txt"):
        try:
            api.delete_object(f"pairs/{pair}/{name}")
        except NotFound:
            pass
    info(f"Deleted {pair} {version}")


# ------------------------------------------------------------------ main ---


def main() -> int:
    accounts, instances = load_config()
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], epilog="See deploy/README.md.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def instance_command(name: str, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help, description=help)
        p.add_argument("instance", choices=sorted(instances), help="instance (instances.toml)")
        p.add_argument("account", choices=sorted(accounts), help="Cloudflare account")
        return p

    sub.add_parser("list", help="list instances and where they're deployed")

    p = instance_command("deploy", "build and deploy an instance: pairs (newest), image, frontend, Worker")
    p.add_argument("--dry-run", action="store_true", help="build everything, upload and deploy nothing")
    p.add_argument("--allow-dirty", action="store_true", help="deploy with uncommitted submodule changes")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    p = instance_command("status", "show what's deployed, its pairs and request stats")
    p.add_argument("--days", type=int, default=7, help="stats period (default 7)")

    instance_command("logs", "stream the Worker's live logs (wrangler tail)")
    instance_command("restart", "restart APy; it then loads the current version of its pairs")

    p = instance_command("delete", "delete an instance's Worker and container from an account")
    p.add_argument("--with-bucket", action="store_true",
                   help=f"also delete the shared {BUCKET} bucket (only if no other instance is deployed)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="don't ask (careful; deleting the bucket still asks)")

    p = sub.add_parser("pairs", help="manage the language pairs in R2",
                       description="Language pairs: one shared R2 bucket per account.")
    pairs_sub = p.add_subparsers(dest="action", required=True)
    pairs_sub.add_parser("list-available", help="pairs in the Apertium apt repo, newest versions")
    for action, help in [
        ("create-bucket", f"create the account's {BUCKET} bucket"),
        ("delete-bucket", f"delete the account's {BUCKET} bucket with all pairs (asks you to type a phrase)"),
        ("list", "pairs in R2: versions, which is current, whether newer ones exist"),
        ("update", "upload the newest version of pairs (default: all instances' pairs), restart instances"),
        ("rollback", "PAIR [VERSION]: point PAIR to VERSION (default: the previous one), restart instances"),
        ("delete", "PAIR VERSION: delete an old version"),
        ("prune", "delete old versions, keeping the current and --keep others per pair"),
    ]:
        sp = pairs_sub.add_parser(action, help=help, description=help)
        sp.add_argument("account", choices=sorted(accounts), help="Cloudflare account")
        if action in ("update", "rollback", "delete"):
            sp.add_argument("pairs", nargs="*", metavar="ARG", help="pair names (and version)")
        if action == "prune":
            sp.add_argument("--keep", type=int, default=2, help="older versions to keep per pair (default 2)")
        # create-bucket and update never ask; delete-bucket always does
        if action in ("rollback", "delete", "prune"):
            sp.add_argument("-y", "--yes", action="store_true")

    p = sub.add_parser("usage", help="requests, container use and estimated costs for an account",
                       description="Requests, container use and estimated costs for a month, per instance "
                                   "and for the account (from Cloudflare's analytics; list prices).")
    p.add_argument("account", choices=sorted(accounts), help="Cloudflare account")
    p.add_argument("--month", help="YYYY-MM (default: this month, so far, with a projection)")

    p = sub.add_parser("images", help="APy images in the account's Cloudflare registry",
                       description="APy images: each deploy pushes one to <instance>-apy in the registry.")
    images_sub = p.add_subparsers(dest="action", required=True)
    for action, help in [("list", "list the instances' image tags, and which are deployed"),
                         ("prune", "delete the tags no deployed instance uses")]:
        sp = images_sub.add_parser(action, help=help, description=help)
        sp.add_argument("account", choices=sorted(accounts), help="Cloudflare account")
        if action == "prune":
            sp.add_argument("-y", "--yes", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "list":
            cmd_list(args, accounts, instances)
        elif args.command == "pairs":
            cmd_pairs(args, accounts, instances)
        elif args.command == "images":
            cmd_images(args, accounts, instances)
        elif args.command == "usage":
            cmd_usage(args, accounts, instances)
        elif args.command in ("deploy", "delete"):
            {"deploy": cmd_deploy, "delete": cmd_delete}[args.command](
                args, accounts[args.account], instances[args.instance], instances)
        else:
            {"status": cmd_status, "logs": cmd_logs, "restart": cmd_restart}[args.command](
                args, accounts[args.account], instances[args.instance])
    except Fail as e:
        print(f"\nmanage.py: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:  # e.g. `manage.py usage test | head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
