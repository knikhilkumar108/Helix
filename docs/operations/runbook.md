# Operations Runbook

## Deploy

```bash
# 1. Provision infrastructure
cd infra/terraform && terraform init && terraform apply

# 2. Configure kubectl
aws eks update-kubeconfig --name automata-prod

# 3. Install the chart
helm upgrade --install automata ./infra/helm \
  -n automata --create-namespace \
  -f ./infra/helm/values.prod.yaml
```

## Upgrade

```bash
helm upgrade automata ./infra/helm \
  -n automata \
  --set image.tag=$RELEASE_SHA
```

Migrations run automatically via the `automata-migrate` Job in the chart.
They are forward-only and gated by a Postgres advisory lock.

## Roll back

```bash
helm history automata -n automata
helm rollback automata <REVISION> -n automata
```

## Investigate an incident

```bash
# 1. List the Automata in the failed state
automata list | jq '.[] | select(.state=="suspended")'

# 2. Pull the audit chain for one Automaton
automata audit --automaton-id <AID> --limit 500

# 3. Verify the chain
automata verify-audit

# 4. Get the worker logs
kubectl logs -l app=automata-worker --tail=200

# 5. Inspect the treasury
automata balance <AID>
automata ledger <AID> --limit 100

# 6. Pause if needed
automata pause <AID>
```

## Common incidents

### Automaton stuck in `suspended`

Cause: balance dropped to zero.
Action: fund it.

```bash
automata fund <AID> --amount-micro 1000000
automata resume <AID>
```

### Action failure rate spikes

Cause: tool rot or external API change.
Action: pause the Automaton, inspect recent errors, fix or replace the
plugin, resume.

### Audit chain broken

Cause: tampering or DB corruption.
Action: open a Sev-1. Compare the last valid snapshot with a copy of the
audit log from the read replica. If the read replica agrees with the
primary up to a point, the break is post-snapshot. Restore from snapshot,
replay from there.

## Disaster recovery

- **Database**: daily snapshot, 14-day retention, point-in-time recovery.
- **Object store**: cross-region replication for snapshots and audit
  exports.
- **Audit log**: hash chain is verifiable from any starting point. The
  service re-exports the chain to an external notary (configurable) for
  independent verification.
