#!/usr/bin/env python3
"""Fetch verified project snapshots and assemble a version-aware shared graph."""

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import zipfile
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

GRAPHIFY_VERSION = "0.9.118"
ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


def load_projects(path):
    config = json.loads(path.read_text())
    projects = config["projects"]
    names = [project["name"] for project in projects]
    if not names or len(set(names)) != len(names):
        raise ValueError("Registry must contain distinct projects")
    for project in projects:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", project["name"]) or project["name"] in (".", ".."):
            raise ValueError("Invalid project name")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", project["repository"]):
            raise ValueError("Invalid repository name")
    return projects


def validate_snapshot(directory, project, allow_local=False):
    raw = (directory / "graph.json").read_bytes()
    if raw.startswith(b"version https://git-lfs.github.com/spec/"):
        raise ValueError(f"{directory}: LFS pointer instead of graph")
    graph = json.loads(raw)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema_version") != 1 or manifest.get("repository") != project["repository"]:
        raise ValueError(f"{directory}: snapshot repository/schema mismatch")
    if manifest.get("graphify_version") != GRAPHIFY_VERSION or manifest.get("scope") != "code-only":
        raise ValueError(f"{directory}: incompatible extractor version/scope")
    if manifest.get("graph_sha256") != hashlib.sha256(raw).hexdigest():
        raise ValueError(f"{directory}: graph checksum mismatch")
    if not allow_local and not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_commit", "")):
        raise ValueError(f"{directory}: published snapshot requires source_commit")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest.get("source_fingerprint", "")):
        raise ValueError(f"{directory}: missing source fingerprint")
    nodes, links = graph.get("nodes"), graph.get("links")
    if not isinstance(nodes, list) or not nodes or not isinstance(links, list):
        raise ValueError(f"{directory}: empty or invalid graph")
    ids = {node["id"] for node in nodes}
    if len(ids) != len(nodes) or any(link.get("source") not in ids or link.get("target") not in ids for link in links):
        raise ValueError(f"{directory}: duplicate IDs or dangling endpoints")
    if graph.get("directed") is not True or graph.get("multigraph") is not True:
        raise ValueError(f"{directory}: graph must preserve direction and parallel links")
    if manifest.get("nodes") != len(nodes) or manifest.get("links") != len(links):
        raise ValueError(f"{directory}: incorrect graph counts")
    coverage = manifest.get("coverage", {})
    files = coverage.get("files", 0)
    represented = coverage.get("represented", -1)
    missing = coverage.get("missing", [])
    if not isinstance(files, int) or files <= 0 or not isinstance(represented, int) or represented / files < 0.95 or represented + len(missing) != files:
        raise ValueError(f"{directory}: insufficient or inconsistent source coverage")
    return graph, manifest


class GitHubRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if urlsplit(new_url).scheme != "https":
            raise ValueError("GitHub artifact redirects must use HTTPS")
        return super().redirect_request(request, response, code, message, headers, new_url)


def gh_api(endpoint, binary=False, paginate=False):
    """Access GitHub directly; GH_HOST/GH_TOKEN are configuration, not a CLI dependency."""
    host = os.environ.get("GH_HOST", "github.com")
    if not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", host):
        raise ValueError("GH_HOST must be a GitHub hostname")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("Set GH_TOKEN or GITHUB_TOKEN with repository and Actions read access")
    base = "https://api.github.com/" if host == "github.com" else f"https://{host}/api/v3/"
    current = urljoin(base, endpoint)
    if not current.startswith(base):
        raise ValueError("GitHub API endpoint must be relative to the configured API")
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), GitHubRedirectHandler())
    pages = []
    while current:
        request = Request(current, headers={"Accept": "application/vnd.github+json", "User-Agent": "knowledge-graph-sync", "X-GitHub-Api-Version": "2022-11-28"})
        # urllib excludes unredirected headers when following the signed artifact
        # storage URL, so the GitHub token cannot leak to the storage host.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
        try:
            with opener.open(request, timeout=60) as response:
                content = response.read()
                link = response.headers.get("Link", "")
        except HTTPError as error:
            raise ValueError(f"GitHub API request for {endpoint} failed: HTTP {error.code}; check token access") from None
        except URLError as error:
            raise ValueError(f"GitHub API request failed: {error.reason}") from None
        if binary:
            return content
        data = json.loads(content)
        if not paginate:
            return data
        pages.append(data)
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        current = urljoin(current, match.group(1)) if match else None
        if current and not current.startswith(base):
            raise ValueError("Pagination link left the configured GitHub API")
    return pages


def unpack_snapshot(payload, destination):
    # Read only the two artifact members; never extract paths supplied by a ZIP.
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in ("graph.json", "manifest.json"):
            if archive.namelist().count(name) != 1:
                raise ValueError(f"Artifact must contain exactly one {name} at its root")
            member = archive.getinfo(name)
            if member.file_size > 250_000_000:
                raise ValueError("Snapshot artifact exceeds supported size")
            (destination / name).write_bytes(archive.read(name))


def fetch(projects, destination, runs=None):
    """Fetch latest successful push artifacts; explicit run IDs support PR snapshots."""
    runs = runs or {}
    for project in projects:
        repo = project["repository"]
        api = f"repos/{repo}/actions"
        if project["name"] in runs:
            run = gh_api(f"{api}/runs/{runs[project['name']]}")
        else:
            page = gh_api(f"{api}/workflows/{project['workflow']}/runs?branch={project['branch']}&event=push&status=success&per_page=1")
            if not page["workflow_runs"]:
                raise ValueError(f"{repo}: no successful published graph run; enable project CI first")
            run = page["workflow_runs"][0]
        if run.get("conclusion") != "success" or run.get("status") != "completed":
            raise ValueError(f"{repo}: graph run has not completed successfully")
        workflow = gh_api(f"{api}/workflows/{project['workflow']}")
        if run.get("workflow_id") != workflow["id"]:
            raise ValueError(f"{repo}: run belongs to a different workflow")
        pages = gh_api(f"{api}/runs/{run['id']}/artifacts?per_page=100", paginate=True)
        artifacts = [artifact for page in pages for artifact in page["artifacts"] if artifact["name"] == project["artifact_prefix"] + run["head_sha"] and not artifact["expired"]]
        if len(artifacts) != 1:
            raise ValueError(f"{repo}: expected one unexpired graph artifact for {run['head_sha']}")
        artifact = artifacts[0]
        with tempfile.TemporaryDirectory(prefix="snapshot-download-") as directory:
            staging = Path(directory)
            unpack_snapshot(gh_api(f"{api}/artifacts/{artifact['id']}/zip", binary=True), staging)
            _, manifest = validate_snapshot(staging, project)
            if manifest["source_commit"] != run["head_sha"]:
                raise ValueError(f"{repo}: artifact source does not match CI run")
            manifest["publication"] = {"run_id": run["id"], "artifact_id": artifact["id"], "created_at": artifact["created_at"], "event": run["event"], "branch": run["head_branch"]}
            target = destination / project["name"]
            target.mkdir(parents=True, exist_ok=True)
            (target / "graph.json").write_bytes((staging / "graph.json").read_bytes())
            (target / "manifest.json").write_text(canonical(manifest))
        print(f"Fetched {repo}@{run['head_sha']}")


def assemble(projects, snapshots, output, replacements=None, allow_local=False):
    if importlib.metadata.version("graphifyy") != GRAPHIFY_VERSION:
        raise ValueError(f"Install graphifyy=={GRAPHIFY_VERSION}")
    import networkx as nx
    from graphify.build import prefix_graph_for_global
    from graphify.cross_repo_calls import link_cross_repo_member_calls
    from graphify.cross_repo_types import link_shared_type_declarations
    from graphify.export import to_json

    replacements = replacements or {}
    unknown = set(replacements) - {project["name"] for project in projects}
    if unknown:
        raise ValueError(f"Unregistered replacement projects: {sorted(unknown)}")
    merged = nx.MultiDiGraph()
    manifests = {}
    offset = 0
    hyperedges = []
    for project in sorted(projects, key=lambda project: project["name"]):
        name = project["name"]
        directory = replacements.get(name, snapshots / name)
        data, manifest = validate_snapshot(directory, project, allow_local)
        graph = nx.node_link_graph(data, edges="links")
        graph.graph["hyperedges"] = data.get("hyperedges", [])
        prefixed = prefix_graph_for_global(graph, name, community_offset=offset)
        # Shared external stubs are global; other symbols retain their project identity.
        merged.add_nodes_from(prefixed.nodes(data=True))
        for source, target, attrs in prefixed.edges(data=True):
            merged.add_edge(source, target, **attrs)
        hyperedges.extend(prefixed.graph.get("hyperedges", []))
        communities = [attrs["community"] for _, attrs in prefixed.nodes(data=True) if isinstance(attrs.get("community"), int)]
        offset = max(communities, default=offset - 1) + 1
        manifests[name] = {**manifest, "view": "replacement" if name in replacements else "baseline"}
    # Graphify's cross-repo resolvers use an undirected view. Discover new edges there,
    # then carry their explicit caller->callee endpoints into the directed multigraph.
    resolver_view = nx.Graph()
    resolver_view.add_nodes_from(merged.nodes(data=True))
    for source, target, attrs in merged.edges(data=True):
        if not resolver_view.has_edge(source, target) or attrs.get("relation") == "method":
            resolver_view.add_edge(source, target, **attrs)
    type_links = link_shared_type_declarations(resolver_view)
    call_links = link_cross_repo_member_calls(resolver_view)
    for source, target, attrs in resolver_view.edges(data=True):
        if attrs.get("context") != "cross_repo":
            continue
        source, target = attrs.get("_src", source), attrs.get("_tgt", target)
        if not any(row.get("relation") == attrs.get("relation") for row in merged.get_edge_data(source, target, default={}).values()):
            merged.add_edge(source, target, **attrs)
    merged.graph["hyperedges"] = hyperedges
    groups = {}
    for node, attrs in merged.nodes(data=True):
        if isinstance(attrs.get("community"), int):
            groups.setdefault(attrs["community"], []).append(node)
    identity = {name: {key: manifest.get(key) for key in ("repository", "source_commit", "source_fingerprint", "graph_sha256", "view")} for name, manifest in manifests.items()}
    snapshot_id = hashlib.sha256(canonical(identity).encode()).hexdigest()
    merged.graph["snapshot_id"] = snapshot_id
    with tempfile.TemporaryDirectory(prefix="shared-graph-") as directory:
        staging = Path(directory)
        to_json(merged, groups, str(staging / "graph.json"), force=True, built_at_commit="")
        raw = (staging / "graph.json").read_bytes()
        data = json.loads(raw)
        ids = {node["id"] for node in data["nodes"]}
        if any(link["source"] not in ids or link["target"] not in ids for link in data["links"]):
            raise ValueError("Assembled graph has dangling endpoints")
        manifest = {"schema_version": 1, "snapshot_id": snapshot_id, "graphify_version": GRAPHIFY_VERSION, "view": "pr" if replacements else "baseline", "local": allow_local, "projects": manifests, "nodes": len(data["nodes"]), "links": len(data["links"]), "graph_sha256": hashlib.sha256(raw).hexdigest(), "cross_repo": {"shared_type_links": type_links, "resolved_calls": call_links}, "tokens": {"input": 0, "output": 0}}
        report = ["# Shared knowledge graph", "", f"Snapshot: `{snapshot_id}`", f"View: {manifest['view']} · local: {allow_local}", f"{manifest['nodes']} nodes · {manifest['links']} directed relationships", "", "## Selected projects", "", "| Project | Source commit | View |", "|---|---|---|"]
        report.extend(f"| {name} | {project.get('source_commit', 'local index')} | {project['view']} |" for name, project in manifests.items())
        report.extend(["", "## Cross-project resolution", f"Shared type links: {type_links}; resolved member calls: {call_links}.", "These are inferred links, not runtime evidence or dependency-version verification.", "", "## Token cost", "Input: 0 · Output: 0 (code-only AST extraction and deterministic assembly)", ""])
        # Validate everything before touching the published graph. Manifest checksum
        # allows consumers to reject an interrupted graph/manifest pair update.
        output.mkdir(parents=True, exist_ok=True)
        for filename, content in (("graph.json", raw), ("manifest.json", canonical(manifest).encode()), ("GRAPH_REPORT.md", "\n".join(report).encode())):
            temporary = output / (filename + ".tmp")
            temporary.write_bytes(content)
            os.replace(temporary, output / filename)
    print(f"Shared graph: {manifest['nodes']} nodes, {manifest['links']} links, {len(projects)} projects")
    return manifest


def mappings(values):
    result = {}
    for value in values:
        name, separator, target = value.partition("=")
        if not separator or not target or name in result:
            raise ValueError("Mappings must be unique PROJECT=VALUE entries")
        result[name] = target
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "assemble", "sync"])
    parser.add_argument("--registry", type=Path, default=ROOT / "config/projects.json")
    parser.add_argument("--snapshots", type=Path, default=ROOT / ".snapshots")
    parser.add_argument("--output", type=Path, default=ROOT / "graphify-out")
    parser.add_argument("--replace", action="append", default=[], metavar="PROJECT=DIRECTORY")
    parser.add_argument("--run", action="append", default=[], metavar="PROJECT=RUN_ID", help="Fetch an explicit successful PR/main run")
    parser.add_argument("--allow-local", action="store_true", help="Accept index snapshots without a source commit for local experiments")
    args = parser.parse_args()
    projects = load_projects(args.registry)
    runs = mappings(args.run)
    if set(runs) - {project["name"] for project in projects}:
        raise ValueError("Run override names an unregistered project")
    if runs and args.command == "sync":
        raise ValueError("Fetch PR runs separately, then assemble with --replace; never overwrite the baseline with a PR")
    if args.command in ("fetch", "sync"):
        if args.allow_local:
            raise ValueError("Remote publication cannot use --allow-local")
        fetch(projects, args.snapshots, runs)
    if args.command in ("assemble", "sync"):
        assemble(projects, args.snapshots, args.output, {name: Path(path) for name, path in mappings(args.replace).items()}, args.allow_local)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Shared graph error: {error}", file=sys.stderr)
        sys.exit(1)
