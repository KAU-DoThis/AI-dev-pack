from .. import prompts


"""Provider-independent contracts for task-local tools."""

HOST_ASK_SCHEMA = {
    "name": "host_ask",
    "description": (prompts.HOST_ASK_DESCRIPTION),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "context": {"type": "string"},
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}

MEMO_WRITE_SCHEMA = {
    "name": "memo_write",
    "description": (
        prompts.MEMO_WRITE_DESCRIPTION
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "kind": {"type": "string"},
            "source_refs": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["content", "kind"],
        "additionalProperties": False,
    },
}

CONVERSATION_SEARCH_SCHEMA = {
    "name": "conversation_search",
    "description": (
        prompts.CONVERSATION_SEARCH_DESCRIPTION
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

CONVERSATION_READ_SCHEMA = {
    "name": "conversation_read",
    "description": (
        prompts.CONVERSATION_READ_DESCRIPTION
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message_index": {
                "type": "integer",
                "minimum": 0,
            },
            "char_offset": {
                "type": "integer",
                "minimum": 0,
            },
            "max_chars": {
                "type": "integer",
                "minimum": 256,
                "maximum": 6000,
            },
        },
        "required": ["message_index"],
        "additionalProperties": False,
    },
}


# Completion is an explicit protocol, independent of a model's text stop reason.
TASK_COMPLETE_SCHEMA = {
    "name": "task_complete",
    "description": (
        prompts.TASK_COMPLETE_DESCRIPTION
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "output": {
                "type": "string",
                "description": prompts.TASK_OUTPUT_DESCRIPTION,
                "minLength": 1,
            },
        },
        "required": ["output"],
        "additionalProperties": False,
    },
}


# Runtime validation lives in Planning, including cross-item dependency checks.
_PLAN_ITEM_PROPERTIES = {
    "item_id": {"type": "string", "minLength": 1},
    "description": {"type": "string", "minLength": 1},
    "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "blocked", "failed"]},
    "depends_on": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True},
    "source_refs": {"type": "array", "items": {"type": "string", "minLength": 1}},
    "status_note": {"type": "string"},
}
_PLAN_CHANGE_PROPERTIES = {
    "reason": {"type": "string", "minLength": 1,
               "description": prompts.PLAN_CHANGE_REASON_DESCRIPTION},
    "source_refs": {"type": "array", "items": {"type": "string", "minLength": 1},
                    "description": prompts.PLAN_SOURCE_DESCRIPTION},
}
_PLAN_REVISION = {"type": "integer", "minimum": 1,
                  "description": prompts.PLAN_REVISION_DESCRIPTION}

PLAN_CREATE_SCHEMA = {
    "name": "plan_create",
    "description": (
        prompts.PLAN_CREATE_DESCRIPTION
    ),
    "parameters": {
        "type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": _PLAN_ITEM_PROPERTIES,
                       "required": ["item_id", "description"], "additionalProperties": False}},
            **_PLAN_CHANGE_PROPERTIES,
        }, "required": ["items", "reason"], "additionalProperties": False,
    },
}

PLAN_READ_SCHEMA = {
    "name": "plan_read",
    "description": prompts.PLAN_READ_DESCRIPTION,
    "parameters": {"type": "object", "properties": {"revision": {"type": "integer", "minimum": 1},
                   "scope": {"type": "string", "enum": ["self", "parent"]}},
                   "additionalProperties": False},
}

PLAN_UPDATE_SCHEMA = {
    "name": "plan_update",
    "description": (
        prompts.PLAN_UPDATE_DESCRIPTION
    ),
    "parameters": {
        "type": "object", "properties": {
            "updates": {"type": "array", "items": {"type": "object", "properties": {
                key: value for key, value in _PLAN_ITEM_PROPERTIES.items() if key != "depends_on"
            }, "required": ["item_id"], "additionalProperties": False}},
            "expected_revision": _PLAN_REVISION, **_PLAN_CHANGE_PROPERTIES,
        }, "required": ["updates", "expected_revision", "reason"], "additionalProperties": False,
    },
}

PLAN_REVISE_SCHEMA = {
    "name": "plan_revise",
    "description": (
        prompts.PLAN_REVISE_DESCRIPTION
    ),
    "parameters": {
        "type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": _PLAN_ITEM_PROPERTIES,
                       "required": ["item_id"], "additionalProperties": False}},
            "expected_revision": _PLAN_REVISION, **_PLAN_CHANGE_PROPERTIES,
        }, "required": ["items", "expected_revision", "reason"], "additionalProperties": False,
    },
}


# Caller identity is bound by Runtime, never supplied as an LLM argument.
SUBAGENT_SPAWN_SCHEMA = {
    "name": "subagent_spawn",
    "description": (
        prompts.SUBAGENT_SPAWN_DESCRIPTION
    ),
    "parameters": {"type": "object", "properties": {
        "instruction": {"type": "string", "minLength": 1},
        "context": {"type": "array", "items": {"type": "string"}},
        "config_id": {"type": "string", "minLength": 1},
        "role": {"type": "string"},
        "result_requirements": {"type": "string"},
        "workspace_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "tool_ids": {"type": "array", "items": {"type": "string"}},
        "redelegate": {"type": "boolean"},
        "plan_ref": {"type": "object", "properties": {
            "plan_id": {"type": "string", "minLength": 1},
            "revision": {"type": "integer", "minimum": 1},
            "item_id": {"type": "string", "minLength": 1},
        }, "required": ["plan_id", "revision", "item_id"], "additionalProperties": False},
    }, "required": ["instruction", "context", "config_id"], "additionalProperties": False},
}

SUBAGENT_STATUS_SCHEMA = {
    "name": "subagent_status",
    "description": prompts.SUBAGENT_STATUS_DESCRIPTION,
    "parameters": {"type": "object", "properties": {
        "task_id": {"type": "string", "minLength": 1},
    }, "additionalProperties": False},
}

SUBAGENT_RESULT_SCHEMA = {
    "name": "subagent_result",
    "description": (
        prompts.SUBAGENT_RESULT_DESCRIPTION
    ),
    "parameters": {"type": "object", "properties": {
        "task_id": {"type": "string", "minLength": 1},
    }, "required": ["task_id"], "additionalProperties": False},
}

SUBAGENT_WAIT_SCHEMA = {
    "name": "subagent_wait",
    "description": (
        prompts.SUBAGENT_WAIT_DESCRIPTION
    ),
    "parameters": {"type": "object", "properties": {
        "task_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                     "items": {"type": "string", "minLength": 1}},
    }, "required": ["task_ids"], "additionalProperties": False},
}

SUBAGENT_CANCEL_SCHEMA = {
    "name": "subagent_cancel",
    "description": (
        prompts.SUBAGENT_CANCEL_DESCRIPTION
    ),
    "parameters": {"type": "object", "properties": {
        "task_id": {"type": "string", "minLength": 1},
    }, "required": ["task_id"], "additionalProperties": False},
}

SUBAGENT_SCHEMAS = (SUBAGENT_SPAWN_SCHEMA, SUBAGENT_STATUS_SCHEMA, SUBAGENT_RESULT_SCHEMA,
                     SUBAGENT_WAIT_SCHEMA, SUBAGENT_CANCEL_SCHEMA)


def _communication_schema(name, description, properties, required):
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False}}


_TEXT = {"type": "string", "minLength": 1}
_QUESTION = {"message": _TEXT, "related_request_id": _TEXT}
PARENT_ASK_SCHEMA = _communication_schema("parent_ask",
    prompts.PARENT_ASK_DESCRIPTION,
    _QUESTION, ["message"])
SUBAGENT_ASK_SCHEMA = _communication_schema("subagent_ask",
    prompts.SUBAGENT_ASK_DESCRIPTION,
    {"task_id": _TEXT, **_QUESTION}, ["task_id", "message"])
AGENT_REPLY_SCHEMA = _communication_schema("agent_reply",
    prompts.AGENT_REPLY_DESCRIPTION,
    {"request_id": _TEXT, "message": _TEXT}, ["request_id", "message"])
AGENT_WAIT_REPLY_SCHEMA = _communication_schema("agent_wait_reply",
    prompts.AGENT_WAIT_REPLY_DESCRIPTION, {"request_id": _TEXT}, ["request_id"])
PARENT_NOTIFY_SCHEMA = _communication_schema("parent_notify",
    prompts.PARENT_NOTIFY_DESCRIPTION,
    {"message": _TEXT}, ["message"])
SUBAGENT_SEND_SCHEMA = _communication_schema("subagent_send",
    prompts.SUBAGENT_SEND_DESCRIPTION,
    {"task_id": _TEXT, "message": _TEXT, "kind": {"type": "string", "enum": ["notify", "instruction"],
                                                 "default": "notify"}}, ["task_id", "message"])
COMMUNICATION_SCHEMAS = (PARENT_ASK_SCHEMA, SUBAGENT_ASK_SCHEMA, AGENT_REPLY_SCHEMA,
                        AGENT_WAIT_REPLY_SCHEMA, PARENT_NOTIFY_SCHEMA, SUBAGENT_SEND_SCHEMA)

TOOL_INTEGRATION_SCHEMAS = (
    _communication_schema("tool_request",
        prompts.TOOL_REQUEST_DESCRIPTION,
        {"requested_tool": _TEXT, "reason": _TEXT, "intended_use": _TEXT},
        ["requested_tool", "reason", "intended_use"]),
    _communication_schema("tool_forward",
        prompts.TOOL_FORWARD_DESCRIPTION,
        {"request_id": _TEXT}, ["request_id"]),
    _communication_schema("tool_wait",
        prompts.TOOL_WAIT_DESCRIPTION, {"request_id": _TEXT}, ["request_id"]),
    _communication_schema("tool_status",
        prompts.TOOL_STATUS_DESCRIPTION, {}, []),
)

RESOURCE_SCHEMAS = (
    _communication_schema("resource_deny", prompts.RESOURCE_DESCRIPTIONS["resource_deny"],
                          {"request_id": _TEXT, "reason": _TEXT}, ["request_id", "reason"]),
    _communication_schema("workspace_request", prompts.RESOURCE_DESCRIPTIONS["workspace_request"],
        {"paths": {"type": "array", "items": _TEXT, "minItems": 1}, "reason": _TEXT}, ["paths", "reason"]),
    _communication_schema("resource_forward", prompts.RESOURCE_DESCRIPTIONS["resource_forward"],
        {"request_id": _TEXT}, ["request_id"]),
    _communication_schema("resource_grant", prompts.RESOURCE_DESCRIPTIONS["resource_grant"],
        {"request_id": _TEXT, "workspace_paths": {"type": "array", "items": _TEXT, "minItems": 1},
         "redelegate": {"type": "boolean"}}, ["request_id"]),
    _communication_schema("resource_wait", prompts.RESOURCE_DESCRIPTIONS["resource_wait"],
        {"request_id": _TEXT}, ["request_id"]),
    _communication_schema("resource_status", prompts.RESOURCE_DESCRIPTIONS["resource_status"], {}, []),
)
