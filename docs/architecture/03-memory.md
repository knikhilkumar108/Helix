# Memory Architecture

Each Automaton has ten memory layers, each tuned to a different access
pattern and lifetime.

| Layer              | Latency  | Persistence  | Access        | Purpose                                  |
|--------------------|----------|--------------|---------------|------------------------------------------|
| working            | ns       | process      | dict          | Current tick's scratchpad                 |
| short_term         | µs       | process+TTL  | FIFO          | Recent events, tick state                 |
| long_term          | ms       | Postgres     | BM25          | Curated, important knowledge              |
| semantic           | ms       | pgvector     | ANN           | Embeddings for similarity recall          |
| procedural         | ms       | Postgres     | key→script    | How-to knowledge (plans, code)            |
| financial          | ms       | Postgres     | SQL + audit   | Treasury, costs, revenue                  |
| operational        | ms       | Postgres     | SQL + events  | Health, retries, errors                   |
| code_history       | ms       | Postgres+git | diff          | Every self-edit, signed                   |
| decision_history   | ms       | Postgres     | SQL + audit   | Why we did what we did                    |
| relationship       | ms       | Postgres     | graph         | Other automata, counterparts              |

## Storage

- **Postgres** stores structured layers and the audit chain.
- **pgvector** stores semantic embeddings.
- **Redis** stores short-term and working memory; allows sub-ms access
  to recent items.
- **Object store** stores code history diffs and exported snapshots.

## Indexing

- BM25-style inverted index for lexical recall.
- ANN (HNSW) for semantic recall.
- Both indices are versioned and rebuilt incrementally.

## Pruning

Each entry has an `importance` score and a `ttl`. Pruning:

1. Removes all entries with `updated_at + ttl < now`.
2. Reduces the importance of entries that have not been retrieved in `2*ttl`.
3. Demotes entries below an importance threshold to archival storage.

## Versioning

Every write bumps a per-Automaton version counter. Read APIs can ask for
"memory as of version V" for reproducible replays.

## Distillation

Long-term memory is built by a periodic job that:

- scans short-term memory for salient patterns
- clusters them with semantic similarity
- writes a distilled summary to long-term memory
- updates the procedural memory with the resulting plan
