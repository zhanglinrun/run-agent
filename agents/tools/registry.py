"""Shared JSON Schema definitions for built-in extension tools."""

from __future__ import annotations

from typing import Any


ToolDef = dict[str, Any]


tool_definitions: list[ToolDef] = [
    {
        "name": "read_file",
        "description": "Read a workspace file with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {"file_path": {"type": "string", "minLength": 1}},
            "required": ["file_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": "Create or replace a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_file",
        "description": "Replace one exact unique string in a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "minLength": 1},
                "old_string": {"type": "string", "minLength": 1},
                "new_string": {"type": "string"},
            },
            "required": ["file_path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_files",
        "description": "List workspace files matching a glob.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "grep_search",
        "description": "Search workspace file contents with a regular expression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1},
                "path": {"type": "string"},
                "include": {"type": "string"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_shell",
        "description": "Run a command in the selected execution environment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1},
                "timeout": {"type": "number", "minimum": 1},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "tool_search",
        "description": "Find and activate deferred extension tools.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "skill",
        "description": "Invoke a registered SKILL.md workflow by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "minLength": 1},
                "args": {"type": "string"},
            },
            "required": ["skill_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "skill_evolve",
        "description": "Apply an explicitly requested durable lesson to a Skill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "minLength": 1},
                "lesson": {"type": "string", "minLength": 1},
                "rationale": {"type": "string"},
                "target": {"type": "string", "enum": ["active", "project", "user"]},
            },
            "required": ["skill_name", "lesson"],
            "additionalProperties": False,
        },
    },
    {
        "name": "skill_create",
        "description": "Create an explicitly requested reusable Skill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string", "minLength": 1},
                "instructions": {"type": "string", "minLength": 1},
                "when_to_use": {"type": "string"},
                "target": {"type": "string", "enum": ["project", "user"]},
                "context": {"type": "string", "enum": ["inline", "fork"]},
                "user_invocable": {"type": "boolean"},
                "allowed_tools": {
                    "type": ["string", "array", "null"],
                    "items": {"type": "string"},
                },
                "evidence": {"type": "string"},
            },
            "required": ["name", "description", "instructions"],
            "additionalProperties": False,
        },
    },
]


__all__ = ["ToolDef", "tool_definitions"]
