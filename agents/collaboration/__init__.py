"""Three-role collaboration model for coding tasks."""

from .roles import ROLE_SPECS, RoleSpec, build_agent_descriptions, get_available_agent_types, get_role_spec

__all__ = ["ROLE_SPECS", "RoleSpec", "build_agent_descriptions", "get_available_agent_types", "get_role_spec"]
