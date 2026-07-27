from __future__ import annotations

from pot.config import Settings, SkillsConfig
from pot.skills import discover, load


def test_every_skill_module_is_discovered():
    found = discover()
    assert {"lights", "timers", "research", "basics"} <= set(found)


def test_skills_expose_tools_and_prompts():
    for skill in discover().values():
        assert skill.tools, f"{skill.name} has no tools"
        assert skill.description


def test_disabled_skill_is_not_loaded():
    settings = Settings(skills=SkillsConfig(disabled=["research"]))
    names = {skill.name for skill in load(settings)}
    assert "research" not in names
    assert "lights" in names


def test_enabled_list_restricts_to_those_skills():
    settings = Settings(skills=SkillsConfig(enabled=["lights"]))
    assert [skill.name for skill in load(settings)] == ["lights"]


def test_tool_names_are_unique_across_skills():
    names = [tool.name for skill in discover().values() for tool in skill.tools]
    assert len(names) == len(set(names)), names
