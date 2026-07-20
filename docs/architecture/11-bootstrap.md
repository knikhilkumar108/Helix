# Self-Bootstrap

The agent's first-run experience. When an operator says "I
want an agent called alice that helps me with research", the
bootstrap is what runs. It validates the inputs, creates the
agent, seeds the default state, and records the event.

## Why a separate module

The platform's `AutomatonRegistry.create()` is a thin
constructor: it takes a name and a prompt, generates a
keypair, and stores the agent. That's a useful primitive,
but it's not enough for a *real* first run:

- It doesn't validate inputs. A one-word prompt is
  technically accepted but produces a useless agent.
- It doesn't seed defaults. A new agent has no memory, no
  skills, no understanding of what it is.
- It doesn't record *why* the agent was created.

The bootstrap adds a policy layer on top of `create()` that
handles these. The registry stays a thin primitive; the
bootstrap is where the platform encodes "what every new
agent should have".

## What the bootstrap does

1. **Validates the inputs.**
   - `name`: 1-64 chars, non-empty after stripping.
   - `genesis_prompt`: at least 8 chars.
   - `initial_balance`: non-negative.

2. **Creates the agent.** Calls `registry.create()` with
   the validated inputs.

3. **Seeds default skills.** The default set:
   - `fs.read`, `fs.write` (sandboxed filesystem)
   - `memory.read`, `memory.write` (the platform's memory)
   - `time.now` (wall clock)
   - `messaging.send`, `messaging.claim` (inter-agent)

   A real platform would consult a policy here. For now,
   the default set is hardcoded.

4. **Seeds initial memory.** A short intro note:

   > "I am a Helix agent. My genesis prompt describes what
   > I should do. My wallet holds USDC that I earn by
   > working and spend on LLM calls. If my balance hits
   > zero, I die. I should look at my inbox, decide if
   > there's work, and act on the most important thing
   > first."

   The agent reads this on its first tick. Without it,
   the agent has no idea who it is.

5. **Records a `bootstrap_completed` event.** The agent's
   audit log shows when and how it was created.

## What the bootstrap does NOT do

- **It does not choose the LLM.** The runtime's
  `build_llm_loop()` does that from env vars.
- **It does not fund the wallet.** The operator does that
  via `POST /v1/treasury/{aid}/fund`.
- **It does not start the loop.** The runtime's
  `build_default_loop()` and `build_llm_loop()` are
  separate from the bootstrap.
- **It does not talk to the agent.** The agent only
  exists after the bootstrap completes.

## Architecture

```
services/bootstrap/bootstrap.py   — BootstrapService, BootstrapRequest, BootstrapResult
services/control_plane/api.py     — wires BootstrapService into the app
services/control_plane/routes/automata.py — uses bootstrap if configured
```

## Wire-up

The control plane's `app.state.bootstrap` is `None` by
default. Operators wire a real `BootstrapService` in
production:

```python
app.state.bootstrap = BootstrapService(
    registry=app.state.registry,
    skills=SqliteSkillStore(...),
    memory=SqliteMemoryStore(...),
)
```

The route `POST /v1/automata` checks for the bootstrap and
uses it if present; otherwise it falls back to a plain
`registry.create()`. This means dev environments work
without configuring a bootstrap, and production can swap in
a real one without changing the route.

## Skip-seed option

The bootstrap supports `skip_seed=True` for cases where the
operator wants a minimal agent with no default skills or
memory. The agent's first observation will be empty, and
the operator is responsible for enabling skills and
seeding memory before the agent's first tick.

## Future improvements

- **Interactive prompts.** Today the bootstrap is
  programmatic: the operator passes the inputs. An
  interactive mode would prompt the operator for missing
  fields (model choice, starting balance, etc.) and
  validate as it goes.
- **Genesis prompt templates.** A library of common
  prompts ("be a customer service agent", "be a research
  assistant", "be a code reviewer") that the operator
  can pick from.
- **Skill recommendations based on the prompt.** A
  "research assistant" prompt would auto-enable web
  search; a "code reviewer" prompt would auto-enable
  `shell.exec`. This is a small NLP task.
- **Resource quotas at bootstrap.** Set the per-tick and
  per-day budget based on the prompt's expected workload.
