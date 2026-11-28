# Contributing to proc-lib

## Setup

```bash
git clone git@github.com:Abrak98/proc-jobs.git
cd proc-jobs
uv sync
```

No runtime dependencies to install — `proc` is stdlib-only by design; `uv sync` creates
`.venv` with the `dev` dependency group (`pytest` and `ruff`) at the versions `uv.lock` pins.

## Running the tests

```bash
uv run pytest -q
```

The suite isolates the registry to a fresh tmp directory per test (`PROC_HOME` /
monkeypatched `core.BASE`) and drives real short-lived processes — no personal or external
infra is needed, and it never touches a live `~/.local/state/proc/`. It requires Linux (or another
`/proc`-filesystem OS): the PID-reuse guard reads `/proc/<pid>/stat` directly.

Lint before sending a change:

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

## Making a change

1. Open an issue first for anything beyond a trivial fix — `proc` is a small, deliberately
   "dumb" supervisor (see `VISION.md` Non-goals); a short discussion up front avoids a PR
   built on scope the maintainer wouldn't take.
2. Add or update a test for the behavior you're changing.
3. If the change touches the engine (`src/proc_lib/core.py` and its modules), `cli.py`, or
   `heavy_job_lock.py`, update the matching spec under `specs/proc_lib/` in the same PR — every source file under `src/`
   declares a `# spec: <path>` header, and the spec is the permanent contract, not the code
   comments.
4. `uv run pytest -q` and `uv run ruff check src/ tests/` must both be clean before you open the PR.

## Pull requests

`main` is protected: nothing is pushed to it directly, every change arrives as a pull request
from a branch in your fork, and it is merged by squash so history stays linear. A pull request
merges only when these checks pass: `secrets`, `denylist`, `gates`, `floor (3.12)` and
`floor (3.13)`. The `denylist` check needs an encrypted secret that GitHub withholds from forks,
so it shows red on a fork's pull request; the maintainer re-runs the same scan locally and merges
it himself.

## Reporting a security issue

Don't open a public issue for a vulnerability — see [SECURITY.md](SECURITY.md).

## Code style

- No comments explaining *what* code does — names should carry that. A comment earns its place
  only when it explains a non-obvious *why* (a workaround, an invariant, a constraint from
  outside the code) — see the existing docstrings in `core.py` for the standard this repo holds
  itself to.
- Match the existing test-naming convention: `test_<behavior>_<condition>` describing the
  observable outcome, not the implementation.

## Keeping the repository clean

Every push and pull request runs the `public-guard` workflow: `gitleaks` over the full history
and `scripts/denylist_check.py`, which scans commit messages, author emails, file names and added
lines for words the maintainer keeps in an encrypted secret. The workflow fails when the secret is
unavailable, so a pull request from a fork is merged by the maintainer only. A maintainer can run
the same scan before pushing with a local deny-list file:
`python3 scripts/denylist_check.py --denylist-file <file> -- <git revision arguments>`.
