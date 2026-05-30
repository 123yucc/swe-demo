"""Long-term memory subsystem: HTTP client to the MemGovern experience_server."""

from src.memory.custom_route import (
    format_custom_rules_for_prompt,
    load_custom_rules,
    match_rule,
    select_matching_rules,
)
from src.memory.launcher import ensure_running
from src.memory.retrieve import (
    Experience,
    ExperienceDetail,
    ExperienceSearchHit,
    append_custom_recommendations_log,
    append_recommendations_log,
    browse_experience,
    fetch_detail,
    format_experiences_for_prompt,
    health_check,
    retrieve_experiences,
    search_experiences,
    search_ids,
    wait_until_ready,
)

__all__ = [
    "Experience",
    "ExperienceDetail",
    "ExperienceSearchHit",
    "append_custom_recommendations_log",
    "append_recommendations_log",
    "browse_experience",
    "ensure_running",
    "fetch_detail",
    "format_custom_rules_for_prompt",
    "format_experiences_for_prompt",
    "health_check",
    "load_custom_rules",
    "match_rule",
    "retrieve_experiences",
    "search_experiences",
    "search_ids",
    "select_matching_rules",
    "wait_until_ready",
]
