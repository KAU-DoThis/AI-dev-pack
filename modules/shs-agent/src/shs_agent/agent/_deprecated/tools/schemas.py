
"""write in english for better performance"""

HOST_ASK_SCHEMA = {
    "name": "host_ask",
    "description": "Ask the host for a required decision and wait for its answer.",
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

MEMORY_WRITE_SCHEMA = {
    "name": "memory_write",
    "description": (
        "Write concise task-local memory that remains available during this task "
        "and is returned to the host with the final result. Use for durable facts, "
        "decisions, constraints, and important discoveries; do not store transient chatter."
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
        "Search the append-only original transcript of this task, including older "
        "messages hidden from the current prompt by context compaction. Returns "
        "stable message_index references and snippets. Use this when an exact "
        "older detail may be missing from the summary."
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
        "Read an exact chunk of one message from the append-only original task "
        "transcript by the message_index returned from conversation_search. "
        "Increase char_offset to continue reading a long message."
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


# sometimes the small model may not be able to finish the task, so we need to provide a way to finish the task and return the final answer to the user.
TASK_COMPLETE_SCHEMA = {
    "name": "task_complete",
    "description": (
        "Complete the current task. "
        "Call this tool only when the task is fully finished. "
        "Put the final user-facing answer in output. "
        "Do not call this tool together with other tools."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "output": {
                "type": "string",
                "description": "Final answer to the user.",
                "minLength": 1,
            },
        },
        "required": ["output"],
        "additionalProperties": False,
    },
}