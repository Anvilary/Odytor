"""Configuration names: ODYTOR_* is canonical, GIT_PLUS_* is a temporary fallback for existing deployments.

Both spellings of a setting are accepted. When both are present with different values the setting is ambiguous and
startup fails (nothing is guessed, in particular nothing less restrictive is silently chosen). Standard library only,
because the runner imports it too. The fallback is removed before the first release.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

NEW_PREFIX = "ODYTOR_"
LEGACY_PREFIX = "GIT_PLUS_"


class EnvConflictError(ValueError):
    """The same setting is configured under both names with different values."""


def lookup(suffix: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Value of ODYTOR_<suffix>, falling back to GIT_PLUS_<suffix>; None when neither is set."""
    env = os.environ if environ is None else environ
    new, legacy = env.get(NEW_PREFIX + suffix), env.get(LEGACY_PREFIX + suffix)
    if new is not None and legacy is not None and new != legacy:
        raise EnvConflictError(f"{NEW_PREFIX}{suffix} and {LEGACY_PREFIX}{suffix} are both set with different values; "
                               f"keep {NEW_PREFIX}{suffix} and remove {LEGACY_PREFIX}{suffix}.")
    return new if new is not None else legacy


def with_prefix(environ: Mapping[str, str], prefix_suffix: str) -> dict[str, str]:
    """Every setting whose suffix starts with prefix_suffix, keyed by its canonical ODYTOR_ name (conflicts raise)."""
    found: dict[str, str] = {}
    for name in environ:
        for prefix in (NEW_PREFIX, LEGACY_PREFIX):
            if name.startswith(prefix + prefix_suffix):
                suffix = name[len(prefix):]
                found.setdefault(NEW_PREFIX + suffix, lookup(suffix, environ))  # type: ignore[arg-type]
    return found


def legacy_names_in_use(environ: Mapping[str, str] | None = None) -> list[str]:
    """GIT_PLUS_* names that are set (and not shadowed by an identical ODYTOR_ twin), for a deprecation note."""
    env = os.environ if environ is None else environ
    return sorted(name for name in env if name.startswith(LEGACY_PREFIX)
                  and NEW_PREFIX + name[len(LEGACY_PREFIX):] not in env)


def default_state_dir(state: str, new: str, legacy: str, leaf: str = "") -> str:
    """<state>/<new>[/<leaf>], unless an installation made under the old name already holds data there."""
    fresh, old = os.path.join(state, new, leaf), os.path.join(state, legacy, leaf)
    return old if os.path.isdir(old) and not os.path.isdir(fresh) else fresh
