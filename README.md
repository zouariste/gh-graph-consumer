# gh-graph-consumer

Assembles a shared knowledge graph by pulling the producer repo's published
graph artifact — all over the GitHub API, with no shared filesystem between the
repos.

On push, on a weekly schedule, on a `repository_dispatch`, or on demand, GitHub
Actions runs `scripts/shared_graph.py sync`, which:

1. finds the producer's **latest successful run** of `knowledge-graph.yml` on
   `main` via the GitHub REST API,
2. downloads its artifact `graphify-sample-lib-<sha>`, reading only the two
   expected files,
3. re-validates it (schema, pinned extractor version, SHA-256 checksum, a 40-hex
   source commit, ≥95% file coverage, directed multigraph) and checks the
   artifact's commit matches the run,
4. stamps provenance and assembles the graph,
5. uploads **`graphify-shared-baseline`** (`graph.json` + `manifest.json` +
   `GRAPH_REPORT.md`).

## Required secret

`GRAPH_READ_TOKEN` — a token that can read the **producer** repo's Actions.
On public GitHub the built-in `GITHUB_TOKEN` is scoped to this repo only and
**cannot** read another repo's artifacts, so this is mandatory.

- Classic token: scopes `repo` + `read:actions`.
- Fine-grained token: `Actions: Read` + `Contents: Read` on the producer repo.

```bash
gh secret set GRAPH_READ_TOKEN -R <owner>/gh-graph-consumer
```

## The only link between the repos

`config/projects.json` names the producer — repo, branch, workflow file, and
artifact prefix. Point it at a different producer and the rest just works.

## Layout

| Path | Purpose |
|------|---------|
| `scripts/shared_graph.py` | fetch + validate + assemble |
| `config/projects.json` | which producer(s) to pull from |
| `.github/workflows/update-knowledge-graph.yml` | the assemble pipeline |
