# corelink — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
A late-bound handle on `proc_lib.core` for the engine modules, so a name replaced on `core` (the base directory, the clock, the lock, the signal sender) is the one every module uses.

## Rationale / Key decisions
- **`core` imports the engine modules, so they cannot import it at load time.** The handle imports `core` on first attribute access; reads and writes go straight to it.
- **The registry directory and the other replaceable names stay properties of `core`**, as they have always been (`core.spec.md`).

## Rejected alternatives
- **A copy of the names in each module.** Rejected: a replacement on `core` would not reach it.

## Acceptance criteria
- GIVEN `proc_lib.core.BASE` replaced WHEN any engine module builds a path THEN it uses the replacement (`conftest.py`, every test).

## State
None.

## Methods
- `core` — the handle; `core.<name>` reads `proc_lib.core.<name>`, `core.<name> = v` sets it.

## Invariants
- Imports nothing from the package at load time; standard library only.

## Formulas
n/a

## I/O contracts
Those of `specs/proc_lib/core.spec.md`; nothing is added or changed by the split.

## Names
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| core | const | |

## Dependencies
- Python standard library.

## Test cases
- GIVEN the suite's `monkeypatch.setattr(core, "BASE", tmp)` THEN every module reads the temporary registry (`conftest.py`).
