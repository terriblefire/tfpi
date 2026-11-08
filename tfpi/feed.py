"""Published firmware: which builds are on ghcr.io, is there a newer one, fetch it.

Builds are pushed by hand (tf4060v2's Gitea "publish" workflow, scripts/ghcr_publish.py)
to one OCI repository, ghcr.io/terriblefire/tf4060-firmware, with tags per board:

  <board>-<build>    one build: the release zip as the single layer
  <board>-release    the newest release build
  <board>-dev        the newest dev (non-release) build
  <board>-catalog    catalog.json, every published build, newest first

The Pi reads only the catalog (one manifest and one small blob, anonymous pull) and
fetches a zip blob when the user asks for it. The catalog is pushed after the build,
so an entry never points at a blob that isn't there. Nothing here flashes: a fetched
zip goes to the normal review page.

The installed build is found by content (the FPGA image and CPLD JED hashes the
flasher records), so a renamed zip or one uploaded by hand still matches.
"""

import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

REPO = os.environ.get("TFPI_FIRMWARE_REPO", "ghcr.io/terriblefire/tf4060-firmware")
DEFAULT_BOARD = "tf4060r5"
CHECK_EVERY = 12 * 3600
TIMEOUT = 30
ZIP_MAX = 64 * 1024 * 1024
CATALOG_MAX = 4 * 1024 * 1024
RECENT = 10
# release builds are named [<tag>_]<YYYY-MM-DD>_<commit> (tf4060v2 release.yml)
BUILD_NAME = re.compile(r"^(?:(?P<version>.+?)_)?(?P<date>\d{4}-\d{2}-\d{2})_(?P<commit>[0-9a-f]{7,40})$")
MANIFEST_TYPES = ", ".join(["application/vnd.oci.image.manifest.v1+json",
                            "application/vnd.docker.distribution.manifest.v2+json"])


NOT_PUBLISHED = "no firmware has been published yet (or the package isn't public)"


class FeedError(RuntimeError):
    pass


def board():
    return config.board() or DEFAULT_BOARD


def cache_file():
    return config.STATE_DIR / "firmware_feed.json"


# --- registry (read-only, anonymous) -------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _split(repo):
    """'ghcr.io/owner/name' -> ('https://ghcr.io', 'owner/name'). An explicit
    http:// (a local test registry) is kept."""
    scheme = "https"
    if "://" in repo:
        scheme, repo = repo.split("://", 1)
    host, _, path = repo.partition("/")
    if not path:
        raise FeedError(f"bad repository {repo!r}")
    return f"{scheme}://{host}", path


class Registry:
    def __init__(self, repo=None):
        self.base, self.path = _split(repo or REPO)
        self.token = None

    def _auth(self, challenge):
        """Bearer challenge -> anonymous pull token (public packages)."""
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge or ""))
        if not challenge or not challenge.lower().startswith("bearer") or "realm" not in fields:
            raise FeedError(NOT_PUBLISHED)
        query = {"scope": f"repository:{self.path}:pull"}
        if "service" in fields:
            query["service"] = fields["service"]
        url = fields["realm"] + "?" + urllib.parse.urlencode(query)
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as reply:
                data = json.loads(reply.read())
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (401, 403):
                # ghcr refuses an anonymous token for a package that doesn't exist or isn't public
                raise FeedError(NOT_PUBLISHED) from error
            raise FeedError(f"registry login failed: HTTP {error.code}") from error
        except (OSError, ValueError) as error:
            raise FeedError(f"registry login failed: {error}") from error
        self.token = data.get("token") or data.get("access_token")
        if not self.token:
            raise FeedError("registry login gave no token")

    def open(self, path, accept=None):
        """GET base/v2/<repo>/<path>. Redirects (blob storage) are followed without
        the registry token, which the storage host would refuse."""
        url = f"{self.base}/v2/{self.path}/{path}"
        for _ in range(2):
            headers = {"User-Agent": "tfpi-firmware-feed"}
            if accept:
                headers["Accept"] = accept
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            try:
                return _opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 401 and not self.token:
                    self._auth(error.headers.get("WWW-Authenticate"))
                    continue
                if error.code in (301, 302, 303, 307, 308) and error.headers.get("Location"):
                    target = urllib.parse.urljoin(url, error.headers["Location"])
                    req = urllib.request.Request(target, headers={"User-Agent": "tfpi-firmware-feed"})
                    try:
                        return urllib.request.urlopen(req, timeout=TIMEOUT)
                    except OSError as inner:
                        raise FeedError(f"download failed: {inner}") from inner
                if error.code == 404:
                    raise FeedError(f"not found on {self.base}: {path}") from error
                raise FeedError(f"registry: HTTP {error.code} for {path}") from error
            except OSError as error:
                raise FeedError(f"can't reach {self.base}: {error}") from error
        raise FeedError("registry login failed")

    def manifest(self, tag):
        with self.open(f"manifests/{tag}", accept=MANIFEST_TYPES) as reply:
            try:
                return json.loads(reply.read(1024 * 1024))
            except ValueError as error:
                raise FeedError(f"bad manifest for {tag}") from error

    def blob(self, digest, out, limit):
        """Stream a blob into the open file `out`, checking size and digest."""
        algo, _, want = digest.partition(":")
        if algo != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", want):
            raise FeedError(f"unsupported digest {digest!r}")
        hasher, total = hashlib.sha256(), 0
        with self.open(f"blobs/{digest}") as reply:
            while True:
                chunk = reply.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise FeedError("download is larger than expected")
                hasher.update(chunk)
                out.write(chunk)
        if hasher.hexdigest() != want:
            raise FeedError("download is damaged (checksum mismatch)")
        return total


# --- catalog ---------------------------------------------------------------------------

def fetch_catalog(registry=None):
    registry = registry or Registry()
    manifest = registry.manifest(f"{board()}-catalog")
    layers = manifest.get("layers") or []
    if not layers:
        raise FeedError("catalog manifest has no content")
    buf = io.BytesIO()
    registry.blob(layers[0]["digest"], buf, CATALOG_MAX)
    try:
        catalog = json.loads(buf.getvalue())
    except ValueError as error:
        raise FeedError("catalog is not valid JSON") from error
    builds = [_describe(b) for b in catalog.get("builds", []) if _valid(b)]
    return {"board": catalog.get("board", board()), "builds": builds}


def _describe(build):
    """Version, build date and short commit for display; taken from the build name when
    the catalog doesn't say (a tag build v0.1 is named v0.1_<date>_<commit>)."""
    m = BUILD_NAME.match(build["name"])
    parts = m.groupdict() if m else {}
    build["version"] = build.get("version") or parts.get("version")
    build["date"] = build.get("date") or parts.get("date") or str(build.get("published", ""))[:10]
    build["short"] = (build.get("commit") or parts.get("commit") or "")[:7]
    return build


def _valid(build):
    zip_ = build.get("zip") if isinstance(build, dict) else None
    return (isinstance(zip_, dict) and isinstance(build.get("name"), str)
            and build.get("channel") in ("release", "dev")
            and str(zip_.get("digest", "")).startswith("sha256:")
            and str(zip_.get("filename", "")).lower().endswith(".zip"))


def check():
    """Fetch the catalog, cache and return it (with an error instead on failure;
    the last good list is kept)."""
    result = {"time": time.time(), "error": None, "repo": REPO, "board": board()}
    try:
        result.update(fetch_catalog())
    except FeedError as error:
        result["error"] = str(error)
        result["builds"] = (cached() or {}).get("builds", [])
    config.write_json(cache_file(), result)
    return result


def cached():
    return config.read_json(cache_file(), None)


def stale():
    last = cached()
    return last is None or time.time() - last.get("time", 0) > CHECK_EVERY


def developer():
    return bool(config.read_json(config.WEB_CONF, {}).get("developer"))


def installed_build(builds, state):
    """The catalog entry whose images are what this tool last flashed."""
    fpga, cpld = state.get("fpga", {}).get("sha"), state.get("cpld", {}).get("sha")
    if not fpga:
        return None
    for build in builds:
        if build.get("fpga_sha") == fpga and (not cpld or not build.get("cpld_sha")
                                              or build.get("cpld_sha") == cpld):
            return build
    return None


def view(feed, state, dev):
    """What the main page shows: newest visible build, whether to offer it, recent list."""
    feed = feed or {}
    builds = feed.get("builds") or []
    visible = builds if dev else [b for b in builds if b["channel"] == "release"]
    latest = visible[0] if visible else None
    mine = installed_build(builds, state)
    out = {"checked": bool(feed), "time": feed.get("time"), "error": feed.get("error"), "developer": dev,
           "latest": latest, "installed": mine, "available": False, "installed_newer": False,
           "recent": visible[:RECENT] if dev else []}
    if latest and mine is not latest:
        if mine is not None and builds.index(mine) < builds.index(latest):
            out["installed_newer"] = True          # e.g. a dev build, developer mode off
        else:
            out["available"] = True
    return out


def find(name, dev):
    """A build from the cached catalog by name (dev builds only in developer mode)."""
    for build in (cached() or {}).get("builds", []):
        if build["name"] == name and (dev or build["channel"] == "release"):
            return build
    return None


def download(build, folder):
    """Fetch the build's zip into `folder`; return its path."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(build["zip"]["filename"]).name)
    if not name.lower().endswith(".zip") or name.startswith("."):
        raise FeedError("bad file name in the catalog")
    target = Path(folder) / name
    size = int(build["zip"].get("size") or ZIP_MAX)
    try:
        with open(target, "wb") as out:
            Registry().blob(build["zip"]["digest"], out, min(size, ZIP_MAX))
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def main(argv=None):
    feed = check()
    print(json.dumps(feed, indent=2))
    return 1 if feed.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
