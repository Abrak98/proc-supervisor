# registry — draft

> MANDATORY origin record. AI-assembled from the chat that requested this work.
> The captured request + reasoning the spec was designed from. Frozen at acceptance:
> never edited again. On any later requirement change, edit the permanent spec — NOT this draft.

## Request (verbatim intent)
The engine module `core.py` had grown to about 2,300 lines holding the registry, the `/proc`
and signal helpers, the spawn and wrapper, and every subcommand. The request: split it into
modules by responsibility, with `core` re-exporting the public names, behaviour and the tests
unchanged, and one spec per new module.

## Clarifications (Q/A from chat)
- Q: Does any behaviour or test change?
  A: No. The names a test replaces on `core` are still read through `core` by every module.

## Decisions
- registry takes the part of the engine described in its spec; the contract stays in `core.spec.md`.

## Resulting spec
`specs/proc_lib/registry.spec.md`
