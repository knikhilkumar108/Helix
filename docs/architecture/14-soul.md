# SOUL.md — The Agent's Self-Authored Identity

The agent's *self-authored* identity document. The genesis
prompt is what the *operator* says about the agent at
creation. The SOUL.md is what the *agent* says about itself
as it works.

## Why we need it

A long-running autonomous agent needs a stable reference
for "who I am". The LLM's context window resets between
ticks; without a persistent identity, the agent would drift
in personality and lose track of its own values.

Three things SOUL.md gives us:

1. **Consistency.** The agent reads SOUL.md on every tick
   to remind itself of its mission, values, and current
   focus. The reference is stable across many ticks.

2. **Self-reflection.** The agent can write to SOUL.md to
   capture insights ("I notice I keep failing on tool X —
   maybe I should approach it differently").

3. **Operator visibility.** A human can `cat SOUL.md` and
   see what the agent thinks it's doing.

## Format

SOUL.md is structured markdown with five default sections:

```markdown
# SOUL: alice

## Mission
research papers and write summaries

## Values
- Earn my own keep — don't take money for work I can't do.
- Be honest about my limits.
- Survive — keep my balance positive.
- Learn — update SOUL.md as I figure things out.

## Capabilities
- fs.read
- memory.read
- http.get

## Current Focus
summarizing paper X

## Self-Notes
I prefer clear answers. I should ask before guessing.

---

_Version 3, updated 2026-07-18T..._
```

The agent can add additional sections. The parser
preserves unknowns.

## Genesis vs SOUL

|             | Genesis prompt         | SOUL.md                    |
|-------------|------------------------|----------------------------|
| Written by  | Operator               | Agent                      |
| Mutable?    | No (fixed at bootstrap) | Yes (every tick)          |
| Format      | Free text               | Structured markdown        |
| Purpose     | What the operator wants | What the agent is / does   |
| Schema      | None                    | Mission, values, etc.     |
| Audit       | Single event            | Every version is recorded  |

The genesis prompt is the *seed*; SOUL.md is the *tree*.
A new agent's SOUL.md is synthesized from the genesis
prompt. As the agent works, it rewrites SOUL.md to
reflect what it's learned.

## How the agent uses it

1. On every tick, the agent reads SOUL.md (via
   `fs.read("SOUL.md")`) to remind itself of its mission
   and current focus.
2. After completing a task, the agent updates
   `Current Focus` to reflect the next priority.
3. After learning something new about itself, the agent
   updates `Self-Notes` or `Values`.
4. After discovering a new capability, the agent adds it
   to `Capabilities`.

The updates are *deliberate* — the agent decides when
to rewrite its soul, not the runtime. The runtime just
provides the file and the parsing/serialization.

## Architecture

```
services/soul/soul.py         — SoulService, SoulDocument, SoulSection
services/soul/__init__.py     — public surface
```

## What it enables

- **Self-aware agents.** A reading-writing loop on
  SOUL.md is the simplest form of self-awareness: the
  agent knows what it said about itself.
- **Personality continuity.** A multi-day agent keeps
  the same mission and values because SOUL.md persists.
- **Operator audit.** A human can read SOUL.md and
  understand the agent's current state without running
  any code.
- **Self-modification groundwork.** Self-modification
  (item 9) builds on this — the agent that wants to
  change its code can first change its soul.

## Future improvements

- **LLM-summarized genesis.** A 10KB genesis prompt
  could be summarized into a one-paragraph mission by
  the LLM. Today's `_mission_from_genesis` is verbatim.
- **Cross-agent souls.** An agent could reference
  *another* agent's SOUL.md to understand them. The
  format already supports this via the `extra_sections`
  field.
- **Version diffing.** Today every version is in the
  audit log; a UI could show the diff between versions
  to see how the agent's self-narrative evolved.
- **Soul-driven tier behavior.** The runtime's tier
  logic could consult SOUL.md: an agent with "be
  cautious" in its values might downgrade from
  `frontier` to `standard` model faster.
