"""Which Codex model answers a Claude model request.

Claude Code names a Claude model in every request body. A Codex backend has
never heard of that name, so codex mode rewrites the ``model`` field before
the request leaves the router.

The map holds two names. ``main`` answers a normal request. ``small`` answers
the cheap background work Claude Code sends to Haiku — titles, summaries and
the like — so the big model is not spent on them.

The defaults come from this box's own Codex session history on 2026-08-09:
461 recorded turns, split between ``gpt-5.6-terra`` and ``gpt-5.6-sol``. Model
names change often, so :func:`reconcile` corrects the map against whatever
CLIProxyAPI reports it can serve.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from claude_swap import paths
from claude_swap.settings import atomic_write_json

DEFAULT_MAIN = "gpt-5.6-terra"
DEFAULT_SMALL = "gpt-5.6-sol"

#: Substrings that mark Claude Code's cheap background model.
_SMALL_MARKERS = ("haiku",)

#: Preferred substrings when the configured name is gone and one must be
#: guessed, best first.
_MAIN_HINTS = ("codex", "gpt-5", "gpt")
_SMALL_HINTS = ("mini", "small", "sol")


@dataclass(frozen=True)
class ModelMap:
    """The two Codex model names codex mode sends upstream."""

    main: str = DEFAULT_MAIN
    small: str = DEFAULT_SMALL

    def pick(self, requested: object) -> str:
        """The Codex model for a requested Claude model name."""
        if not isinstance(requested, str) or not requested:
            return self.main
        lowered = requested.lower()
        if any(marker in lowered for marker in _SMALL_MARKERS):
            return self.small
        return self.main

    def to_dict(self) -> dict:
        return {"main": self.main, "small": self.small}

    @classmethod
    def from_dict(cls, data: object) -> "ModelMap":
        if not isinstance(data, dict):
            return cls()
        main = data.get("main")
        small = data.get("small")
        return cls(
            main=main if isinstance(main, str) and main else DEFAULT_MAIN,
            small=small if isinstance(small, str) and small else DEFAULT_SMALL,
        )


def map_path() -> Path:
    return paths.get_router_root() / "models.json"


def read_map(path: Path | None = None) -> ModelMap:
    """The configured map. A missing or broken file gives the defaults."""
    target = path or map_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return ModelMap()
    try:
        return ModelMap.from_dict(json.loads(raw))
    except json.JSONDecodeError:
        return ModelMap()


def write_map(model_map: ModelMap, path: Path | None = None) -> None:
    """Replace the map file atomically."""
    atomic_write_json(path or map_path(), model_map.to_dict())


def _best(available: list[str], hints: tuple[str, ...]) -> str | None:
    """A served model matching one of the hints, or None.

    None rather than the first entry in the list. CLIProxyAPI can serve
    several provider families at once, so an arbitrary pick can be a Claude
    model — and answering a Codex request from a Claude credential spends the
    quota this whole path exists to save.
    """
    for hint in hints:
        for name in available:
            if hint in name.lower():
                return name
    return None


def reconcile(model_map: ModelMap, available: list[str]) -> ModelMap:
    """Correct a map against the model list the backend reports.

    A name the backend does not serve is replaced by the closest thing it
    does serve. An empty list changes nothing: an unanswered model list is no
    evidence that the configured names are wrong.
    """
    if not available:
        return model_map
    known = {name.lower() for name in available}
    main = model_map.main
    small = model_map.small
    if main.lower() not in known:
        main = _best(available, _MAIN_HINTS) or main
    if small.lower() not in known:
        small = _best(available, _SMALL_HINTS) or main
    if (main, small) == (model_map.main, model_map.small):
        return model_map
    return ModelMap(main=main, small=small)
