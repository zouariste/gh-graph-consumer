---
name: graph
description: Query the shared knowledge graph this repo assembles and publishes, via scripts/graph-query.
---

# Query the shared graph

This repo has no source of its own to graph — it assembles a shared graph from
the producer's published artifact. Query the latest published shared graph with
the wrapper, which fetches it only when it changed, verifies the checksum, caches
it, then queries:

```bash
scripts/graph-query "who defines greet"
scripts/graph-query --explain "sample-lib::Greeter"
scripts/graph-query --path "sample-lib::App" "sample-lib::Greeter"
```

## Notes

- IDs are project-prefixed (e.g. `sample-lib::…`); use a full ID when a name is
  ambiguous.
- Cite source file/line and the edge relation. Cross-project inferred calls are
  static evidence, not runtime proof.
- Needs `gh auth login -h github.com` once and a graphify binary (`GRAPHIFY_BIN`,
  a local `.venv-graphify`, or `graphify` on PATH).
