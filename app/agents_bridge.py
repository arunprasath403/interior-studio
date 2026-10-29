"""Connects the web app to the Bedrock agents."""
from __future__ import annotations

from engine.agents import AgentError, plan_from_brief


def plan_brief(llm, brief: str) -> dict:
    """Plan from free text. AgentError messages are written for the user, so they pass through unchanged."""
    try:
        return plan_from_brief(llm, brief)
    except AgentError as err:
        raise RuntimeError(str(err)) from err
