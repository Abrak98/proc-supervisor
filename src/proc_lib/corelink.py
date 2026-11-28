# spec: specs/proc_lib/corelink.spec.md
"""A late-bound handle on :mod:`proc_lib.core`.

The engine modules read the few names a caller may replace (``BASE``, ``_now``,
``_with_lock``, ...) through ``core`` at call time, so a monkeypatch of the attribute on
:mod:`proc_lib.core` reaches every module. :mod:`proc_lib.core` imports those modules, so
they cannot import it at load time: this proxy imports it on first use.
"""

from __future__ import annotations

import importlib


class _CoreLink:
    """Attribute reads and writes go to :mod:`proc_lib.core`, imported when first needed."""

    def __getattr__(self, name: str):
        return getattr(importlib.import_module("proc_lib.core"), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(importlib.import_module("proc_lib.core"), name, value)


core = _CoreLink()
