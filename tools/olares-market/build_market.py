"""Render the charts in `olares-apps/` into a static Olares Market source.

An Olares "market source" is four HTTP endpoints, not a Helm repository:

    GET  /api/v1/appstore/hash            has the catalog changed
    GET  /api/v1/appstore/info            the whole catalog, summaries only
    POST /api/v1/applications/info        the full record for a list of app ids
    GET  /api/v1/applications/<app>/chart the packaged chart

Three of them are GETs whose response depends on nothing the caller sends, so
three of them are files. That is what this script writes: a directory that
GitHub Pages can serve as-is, plus the `catalog.json` the companion Worker in
`worker/` reads to answer the one POST static hosting cannot. Neither half
holds state - both are derived from the charts in this repository.

The protocol is not documented by upstream. It was read off a working
third-party source, `aamsellem/olares-one-market`, whose Cloudflare Worker and
catalog builder are public; the field shapes, the id derivation and the two
traps below come from there.

Two traps worth stating, because both fail silently:

- **Timestamps are parsed strictly** as `2006-01-02T15:04:05.000000000Z`, nine
  digits of fraction. A three-digit millisecond stamp is not a parse error
  that names itself - the syncer skips the app.
- **`tops` is what the Go syncer iterates.** The flat `apps` dictionary alone
  yields zero parsed apps, so a catalog without a ranked `tops` list syncs as
  an empty source and looks like an authentication problem.

Run it with `--out site/`; `--pages-origin` and `--source-url` decide the URLs
the catalog points at, so a fork or a local rehearsal needs no edit here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from chart_package import package_chart

REPO_ROOT = Path(__file__).resolve().parents[2]
CHARTS_DIR = REPO_ROOT / "olares-apps"

#: The market's own protocol version, echoed back in every response. Olares
#: sends the version it wants as `?version=`; the Worker echoes the request,
#: and the static files carry this because a file cannot read a query string.
MARKET_VERSION = "1.12.6"

#: `source: 1` marks an app as belonging to a third-party source rather than
#: the official catalog. Both the summary and the sidebar entries carry it.
THIRD_PARTY_SOURCE = 1

#: Every category in the catalog needs an icon in the sidebar, and an unknown
#: category renders as a gap rather than an error. One generic icon for all of
#: them is deliberate: the alternative is a lookup table that silently goes
#: stale whenever a chart invents a category.
CATEGORY_ICON = "https://app.cdn.olares.com/icons/market/sidebar/neurology.svg"

#: Sidebar entries carry a creation date beside their `updated_at`. It is not
#: derived from anything here and nothing reads it back, so it is a constant
#: rather than another thing a rebuild can churn.
SIDEBAR_CREATED_AT = "2026-09-22T00:00:00.000000000Z"


def iso_nanos(when: dt.datetime) -> str:
    """The only timestamp format the Go syncer accepts. See the module docstring."""
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond:06d}000Z"


def app_id(name: str) -> str:
    """The id every other part of the protocol addresses an app by.

    md5 of the name, truncated - the convention the market backend's own
    sources use. It has to be stable across builds: it is what Olares stores
    against an installed app, so a derivation that changed would orphan the
    install rather than upgrade it.
    """
    return hashlib.md5(name.encode(), usedforsecurity=False).hexdigest()[:8]


def chart_changed_at(chart: Path) -> dt.datetime:
    """When this chart last changed, as git knows it.

    Not `now()`, and that matters more than it looks: `updated_at` is written
    into files that a build publishes, so a clock-derived value makes every
    rebuild a content change - a Pages deploy per CI run, and a catalog that
    claims every app was updated this morning. Falls back to the epoch when
    git cannot answer (a tarball export, a shallow clone without history).
    """
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cI", "--", str(chart)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    stamp = result.stdout.strip()
    if result.returncode != 0 or not stamp:
        return dt.datetime.fromtimestamp(0, tz=dt.UTC)
    return dt.datetime.fromisoformat(stamp)


def parse_cpu(value: object) -> str:
    """`"2200m"` -> `"2.2"`; the market stores CPU as a bare number of cores."""
    text = str(value or "0")
    if text.endswith("m"):
        return str(int(text[:-1]) / 1000)
    return text


def parse_bytes(value: object) -> str:
    """`"40Gi"` -> the byte count as a string, which is what the market stores."""
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
    text = str(value or "0")
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            return str(int(text[: -len(suffix)]) * multiplier)
    return text


class App:
    """One chart, read once and asked for each of its three renderings."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.chart: dict[str, Any] = yaml.safe_load((directory / "Chart.yaml").read_text())
        self.manifest: dict[str, Any] = yaml.safe_load(
            (directory / "OlaresManifest.yaml").read_text()
        )
        self.metadata: dict[str, Any] = self.manifest.get("metadata") or {}
        self.spec: dict[str, Any] = self.manifest.get("spec") or {}
        self.name: str = self.chart["name"]
        self.version: str = str(self.metadata.get("version") or self.chart["version"])
        self.id = app_id(self.name)
        self.updated_at = iso_nanos(chart_changed_at(directory))

    @property
    def categories(self) -> list[str]:
        return list(self.metadata.get("categories") or [])

    @property
    def chart_file(self) -> str:
        return f"{self.name}-{self.chart['version']}.tgz"

    @property
    def tags(self) -> list[str] | None:
        """The badges the Market card shows, taken from the chart's own bento block."""
        bento = self.metadata.get("bento")
        if not bento:
            return None
        return [value for value in (bento.get("family"), bento.get("badge")) if value]

    def icon(self, pages_origin: str) -> str:
        """An `icon.png` beside the chart wins; otherwise the manifest's own URL.

        Charts in this repository mostly point at an external host they
        inherited. A chart that ships its own icon gets it published here
        instead, which is the only way to stop depending on that host.
        """
        if (self.directory / "icon.png").is_file():
            return f"{pages_origin}/icons/{self.name}.png"
        return str(self.metadata.get("icon") or "")

    def summary(self, pages_origin: str) -> dict[str, Any]:
        """The catalog row: what the Market grid draws before anything is clicked."""
        categories = self.categories
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "category": categories[0] if categories else "Utilities",
            "categories": categories,
            "description": self.metadata.get("description") or "",
            "icon": self.icon(pages_origin),
            "screenshots": None,
            "tags": self.tags,
            "metadata": None,
            "source": THIRD_PARTY_SOURCE,
            "updated_at": self.updated_at,
        }

    def detail(self, pages_origin: str) -> dict[str, Any]:
        """The full record: the app page, and everything the installer reads."""
        accelerator = (self.spec.get("accelerator") or [{}])[0]
        return {
            "id": self.id,
            "appID": self.id,
            "name": self.name,
            "cfgType": self.manifest.get("olaresManifest.type") or "app",
            "chartName": self.chart_file,
            "icon": self.icon(pages_origin),
            "title": self.metadata.get("title") or self.name,
            "description": self.metadata.get("description") or "",
            "fullDescription": self.spec.get("fullDescription") or "",
            "upgradeDescription": self.spec.get("upgradeDescription") or "",
            "version": self.version,
            "versionName": str(self.spec.get("versionName") or self.chart.get("appVersion") or ""),
            "categories": self.categories,
            "promoteImage": self.spec.get("promoteImage") or [],
            "promoteVideo": self.spec.get("promoteVideo") or "",
            "subCategory": self.spec.get("subCategory") or "",
            "locale": self.spec.get("locale") or ["en-US"],
            "developer": self.spec.get("developer") or "",
            "submitter": self.spec.get("submitter") or "",
            "doc": self.spec.get("doc") or "",
            "website": self.spec.get("website") or "",
            "sourceCode": self.spec.get("sourceCode") or "",
            "license": self.spec.get("license") or [],
            "legal": self.spec.get("legal"),
            "featuredImage": "",
            # Resource figures come from the accelerator profile when the app
            # declares one, because that is where a GPU app's real numbers
            # live - `spec.requiredMemory` on such a chart is the CPU-only
            # fallback and understates it by tens of gigabytes.
            "requiredMemory": parse_bytes(
                accelerator.get("requiredMemory") or self.spec.get("requiredMemory")
            ),
            "requiredDisk": parse_bytes(
                accelerator.get("requiredDisk") or self.spec.get("requiredDisk")
            ),
            "requiredCPU": parse_cpu(
                accelerator.get("requiredCpu") or self.spec.get("requiredCpu")
            ),
            "requiredGPU": parse_bytes(
                accelerator.get("requiredGPUMemory") or self.spec.get("requiredGpu")
            ),
            "supportArch": self.spec.get("supportArch") or [],
            "supportClient": self.spec.get("supportClient") or {},
            "target": self.spec.get("target") or "",
            "onlyAdmin": bool(self.spec.get("onlyAdmin") or False),
            "permission": self.manifest.get("permission") or {},
            "middleware": self.manifest.get("middleware"),
            "options": self.manifest.get("options") or {},
            "entrances": [
                {
                    "name": entrance.get("name") or "",
                    "host": entrance.get("host") or "",
                    "port": entrance.get("port") or 0,
                    "title": entrance.get("title") or "",
                    "icon": entrance.get("icon") or "",
                    "authLevel": entrance.get("authLevel") or "private",
                    "invisible": bool(entrance.get("invisible") or False),
                    "openMethod": entrance.get("openMethod") or "",
                    "disablePreload": bool(entrance.get("disablePreload") or False),
                }
                for entrance in self.manifest.get("entrances") or []
            ],
            "i18n": {},
            "namespace": "",
            "lastCommitHash": "",
            "createTime": 0,
            "updateTime": 0,
            "count": None,
            "rating": 0,
            "versionHistory": [
                {
                    "appName": self.name,
                    "version": self.version,
                    "versionName": str(self.chart.get("appVersion") or ""),
                    "mergedAt": self.updated_at,
                    "upgradeDescription": "",
                }
            ],
            "screenshots": None,
            "tags": self.tags,
            "metadata": None,
            "updated_at": self.updated_at,
        }


def discover(charts_dir: Path) -> list[App]:
    apps = [
        App(directory)
        for directory in sorted(charts_dir.iterdir())
        if (directory / "Chart.yaml").is_file() and (directory / "OlaresManifest.yaml").is_file()
    ]
    if not apps:
        raise SystemExit(f"no charts with an OlaresManifest.yaml under {charts_dir}")
    return apps


def build_catalog(apps: Iterable[App], pages_origin: str) -> dict[str, Any]:
    """Everything derived from the charts, with no timestamp of its own.

    The hash is over this content, so two builds of one commit agree and a
    build of a changed chart does not. Olares polls `/appstore/hash` and only
    re-reads the catalog when it moves, so a hash that churned on a clock
    would re-sync every app every few minutes.
    """
    summaries = {app.id: app.summary(pages_origin) for app in apps}
    details = {app.id: app.detail(pages_origin) for app in apps}
    latest = [app.name for app in apps]
    tops = [{"appId": name, "rank": rank} for rank, name in enumerate(latest, start=1)]
    categories = sorted({category for app in apps for category in app.categories})
    payload = json.dumps(
        {"summaries": summaries, "details": details, "latest": latest, "tops": tops},
        sort_keys=True,
    )
    return {
        "hash": hashlib.md5(payload.encode(), usedforsecurity=False).hexdigest(),
        "summaries": summaries,
        "details": details,
        "latest": latest,
        "tops": tops,
        "categories": categories,
    }


def appstore_info(catalog: dict[str, Any], built_at: str) -> dict[str, Any]:
    """`GET /api/v1/appstore/info` - the catalog plus the sidebar it is drawn in.

    `pages`, `topic_lists` and `tags` are the Market's navigation, not app
    data: a category is only visible when it appears both in an app's
    `categories` and as a key in `tags`, so the three are generated from one
    list rather than written down twice.
    """
    by_category: dict[str, list[str]] = {}
    for app_key, detail in catalog["details"].items():
        for category in detail["categories"]:
            by_category.setdefault(category, []).append(app_key)

    pages: dict[str, Any] = {}
    topic_lists: dict[str, Any] = {}
    tags: dict[str, Any] = {}
    for index, category in enumerate(catalog["categories"]):
        topic = f"Featured apps in {category}"
        pages[category] = {
            "category": category,
            "content": json.dumps(
                [{"type": "Topic", "id": topic}, {"type": "Default Topic", "id": "Newest"}]
            ),
            "source": THIRD_PARTY_SOURCE,
            "updated_at": built_at,
            "createdAt": SIDEBAR_CREATED_AT,
        }
        topic_lists[topic] = {
            "name": topic,
            "type": "Category",
            "content": ",".join(by_category.get(category, [])),
            "title": {"en-US": topic},
            "source": THIRD_PARTY_SOURCE,
            "updated_at": built_at,
            "createdAt": SIDEBAR_CREATED_AT,
        }
        tags[category] = {
            "_id": f"cat_{category.lower().replace(' ', '_')}",
            "name": category,
            "title": {"en-US": category},
            "icon": CATEGORY_ICON,
            "sort": 10 + index,
            "source": THIRD_PARTY_SOURCE,
            "updated_at": built_at,
            "createdAt": SIDEBAR_CREATED_AT,
        }

    return {
        "version": MARKET_VERSION,
        "hash": catalog["hash"],
        "last_updated": built_at,
        "data": {
            "apps": catalog["summaries"],
            "recommends": {},
            "pages": pages,
            "topics": {},
            "topic_lists": topic_lists,
            "tops": catalog["tops"],
            "latest": catalog["latest"],
            "tags": tags,
        },
        "stats": {
            "appstore_data": {
                "apps": len(catalog["summaries"]),
                "pages": len(pages),
                "recommends": 0,
                "tags": len(tags),
                "topic_lists": len(topic_lists),
                "topics": 0,
            },
            "last_updated": built_at,
        },
    }


def package_charts(apps: Iterable[App], out: Path) -> None:
    """Package each chart into `charts/`, and again under the API path.

    The API path is a file called `chart` with no extension, because that is
    the URL Olares asks for: `/api/v1/applications/<app>/chart?fileName=...`.
    Static hosting ignores the query, so the copy under `charts/` - named
    with its version - is what a caller that names a specific version gets.

    `chart_package` rather than `helm package`: the rebuild has to run on a
    shared host with no helm binary, and its output is byte-reproducible,
    which helm's is not. `testing/test_market_build.py` holds the two to the
    same rendered objects.
    """
    charts = out / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    for app in apps:
        packaged = package_chart(app.directory, charts)
        if packaged.name != app.chart_file:
            raise SystemExit(f"packaged {app.name} as {packaged.name}, not {app.chart_file}")
        api_dir = out / "api" / "v1" / "applications" / app.name
        api_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(packaged, api_dir / "chart")


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n")


def render_index(apps: list[App], catalog: dict[str, Any], source_url: str) -> str:
    """The page a person gets, as opposed to the four a machine does."""
    rows = "\n".join(
        f"""    <tr>
      <td><code>{app.name}</code></td>
      <td>{app.metadata.get("title") or app.name}</td>
      <td>{app.version}</td>
      <td>{", ".join(app.categories) or "&mdash;"}</td>
      <td><a href="charts/{app.chart_file}">{app.chart_file}</a></td>
    </tr>"""
        for app in apps
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>homelab Olares apps</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 52rem; margin: 2rem auto;
         padding: 0 1rem; line-height: 1.5; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; }}
  code {{ background: #f4f4f4; padding: 0.1rem 0.3rem; border-radius: 3px; }}
</style>
</head>
<body>
<h1>homelab Olares apps</h1>
<p>An Olares Market source serving the Helm charts in
<a href="https://github.com/RonnyPfannschmidt/homelab-images">RonnyPfannschmidt/homelab-images</a>.
They are written for one machine - an Olares One with an RTX 5090M - and are
published because that is what makes them installable, not because they are
expected to suit another box.</p>
<p>Add it in Olares under <em>Market &rarr; Settings &rarr; Add source</em>:</p>
<pre><code>{source_url}</code></pre>
<p>Or install a chart directly, without adding the source at all:</p>
<pre><code>olares-cli market upload &lt;chart&gt;.tgz
olares-cli market install &lt;app&gt; -s upload</code></pre>
<h2>{len(apps)} apps</h2>
<table>
  <thead><tr><th>App</th><th>Title</th><th>Version</th><th>Categories</th><th>Chart</th></tr></thead>
  <tbody>
{rows}
  </tbody>
</table>
<p><small>Catalog hash <code>{catalog["hash"]}</code>.</small></p>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "site", help="output directory")
    parser.add_argument("--charts", type=Path, default=CHARTS_DIR, help="directory of charts")
    parser.add_argument(
        "--pages-origin",
        default="https://ronnypfannschmidt.github.io/homelab-images",
        help="where the generated tree will be served from; icon URLs point at it",
    )
    parser.add_argument(
        "--source-url",
        default="",
        help="the URL added to Olares as the market source; defaults to --pages-origin",
    )
    parser.add_argument(
        "--skip-charts",
        action="store_true",
        help="write the JSON only, leaving the chart tarballs unpackaged",
    )
    args = parser.parse_args(argv)

    pages_origin = args.pages_origin.rstrip("/")
    apps = discover(args.charts)
    catalog = build_catalog(apps, pages_origin)
    # One timestamp for the whole build, and it is the newest chart's own
    # change time rather than the clock - same reason as `chart_changed_at`.
    built_at = max(app.updated_at for app in apps)

    out: Path = args.out
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    write_json(out / "catalog.json", catalog)
    write_json(
        out / "api" / "v1" / "appstore" / "hash",
        {"hash": catalog["hash"], "last_updated": built_at, "version": MARKET_VERSION},
    )
    write_json(out / "api" / "v1" / "appstore" / "info", appstore_info(catalog, built_at))
    # The POST endpoint's payload, served here as a GET. Static hosting
    # answers POST with 405, so this file is what the Worker reads and what a
    # human can look at; it is not the endpoint itself.
    write_json(
        out / "api" / "v1" / "applications" / "info",
        {"apps": catalog["details"], "version": MARKET_VERSION},
    )

    for app in apps:
        icon = app.directory / "icon.png"
        if icon.is_file():
            (out / "icons").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(icon, out / "icons" / f"{app.name}.png")

    if not args.skip_charts:
        package_charts(apps, out)

    (out / "index.html").write_text(render_index(apps, catalog, args.source_url or pages_origin))
    # Pages otherwise runs the tree through Jekyll, which drops directories
    # whose name begins with an underscore and can rewrite what it considers
    # a template. Nothing here wants processing.
    (out / ".nojekyll").write_text("")

    print(f"{len(apps)} apps -> {out} (hash {catalog['hash']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
