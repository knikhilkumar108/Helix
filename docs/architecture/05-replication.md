# Replication & Lineage

## Goals

- Allow an Automaton to spawn children that share the same Constitution
  and selected knowledge, but have an independent identity and lifecycle.
- Keep the parent *not liable* for the child's actions.
- Make lineage cryptographically verifiable.

## Identity

A child is a brand new Ed25519 key pair. The parent's key does not sign
the child; the child is sovereign. The link is recorded in metadata and
immutable in the audit log.

## Funding

Replication requires:

1. The parent has sufficient balance to seed the child.
2. The parent's `ReplicationPolicy` allows the spawn.
3. The child has not exceeded the `max_children` count.

The seed amount is debited from the parent's treasury in the same
transaction that creates the child.

## Knowledge inheritance

The parent may (by policy) pass a curated set of memory layers to the
child. The default is `semantic` and `procedural` only — code and
private financial memory are never inherited.

Each inherited entry is *copied*, not moved. The parent retains the
original. Both parties can independently prune.

## Lineage tree

The platform exposes a `lineage` view per Automaton:

```
atm_root
├── atm_child_a
│   ├── atm_grandchild_aa
│   └── atm_grandchild_ab
└── atm_child_b
```

The tree is a closure over the `parent_id` column in `automata`. The
audit log records every spawn with a hash-chained entry.

## Forking vs. copying

A *copy* duplicates the Automaton and stops; a *fork* creates a child
that continues to evolve. The default is fork. The child cannot read
the parent's private memory, but it inherits the public-facing knowledge
explicitly shared.
