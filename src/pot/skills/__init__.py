"""Skill discovery: import every module under `pot.skills` and collect its `SKILL`."""

from __future__ import annotations

import importlib
import logging
import pkgutil

from pot.config import Settings
from pot.skills.base import Skill

log = logging.getLogger(__name__)

__all__ = ["Skill", "discover", "load"]


def discover() -> dict[str, Skill]:
    """Every skill in the package, whether enabled or not."""
    found: dict[str, Skill] = {}
    for info in pkgutil.iter_modules(__path__, prefix=f"{__name__}."):
        if info.name.rsplit(".", 1)[-1] in {"base"}:
            continue
        try:
            module = importlib.import_module(info.name)
        except Exception:
            log.exception("skill module %s failed to import", info.name)
            continue
        skill = getattr(module, "SKILL", None)
        if isinstance(skill, Skill):
            found[skill.name] = skill
        else:
            log.debug("%s exports no SKILL, ignoring", info.name)
    return found


def load(settings: Settings) -> list[Skill]:
    """Enabled skills, with their setup hooks run."""
    available = discover()
    wanted = settings.skills.enabled or list(available)
    skills = []
    for name in wanted:
        skill = available.get(name)
        if skill is None:
            log.warning("skill %r is enabled in config but was not found", name)
            continue
        if name in settings.skills.disabled:
            continue
        if skill.setup is not None:
            skill.setup(settings)
        skills.append(skill)
    return skills
