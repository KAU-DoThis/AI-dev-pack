"""Execution boundaries, state transitions and limits for one task.

Context owns Conversation/compaction state; Channel owns communication; Tools
owns dispatch. Runtime orchestrates those interfaces; Channel owns current TaskState/AgentState.
"""
from __future__ import annotations

import asyncio
import copy
import math
from dataclasses import asdict, dataclass, field, replace
from time import perf_counter
from threading import get_ident
from typing import Any, Awaitable, Callable, TypeVar
from uuid import uuid4

from shs_agent.llm import LLMResponse, ToolCall
from shs_agent.llm.errors import ContextOverflow, ContextUnrecoverable

from ..context import ContextBuilder, TaskContext
from ..context.compaction import estimate_context_chars
from ..context.conversation import tool_error
from ..core import AgentCore
from ..planning import Planning
from ..state import AgentActivity
from ..subagent.scope import SubagentScope
from ..tracer.models import utc_timestamp
from ..tracer.serialization import capture
from ..session.channel import HostResponseTimeout, QuestionInterrupted, TaskChannel
from ..tools import SessionTools
from ..tools.session_tools import ToolSnapshot, ToolUnavailable
from ..types import AgentLimits, HostCommand, TaskResult, TaskStatus, nonempty

T = TypeVar("T")


def usage_metrics(usage: Any, _ancestors: tuple[int, ...] = ()) -> dict[str, Any] | None:
    """Keep supplied numeric measurements, never arbitrary provider response text.

    Unknown/non-finite values are omitted, not guessed or converted with repr().
    This also keeps optional diagnostics from breaking JSON trace export.
    """
    if not isinstance(usage, dict) or id(usage) in _ancestors or len(_ancestors) >= 16:
        return None
    metrics = {}
    for key, value in usage.items():
        if not isinstance(key, str):
            continue
        if type(value) is int or (type(value) is float and math.isfinite(value)):
            metrics[key] = value
        elif isinstance(value, dict):
            nested = usage_metrics(value, (*_ancestors, id(usage)))
            if nested:
                metrics[key] = nested
    return metrics or None


@dataclass
class ToolExecution:
    """Per-attempt metadata, including calls rejected before dispatch."""
    llm_call_id: str
    invocation_index: int
    execution_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: float | None = None
    duration_ms: float | None = None
    request_artifact_id: str | None = None
    dispatch_artifact_id: str | None = None
    raw_result_artifact_id: str | None = None

    def payload(self, call: ToolCall) -> dict[str, Any]:
        return {
            "tool_call_id": call.id, "name": call.name,
            "tool_execution_id": self.execution_id, "llm_call_id": self.llm_call_id,
            "arguments_ref": {"message_index": self.invocation_index, "tool_call_id": call.id,
                              "artifact_id": self.request_artifact_id},
        }


class TaskRuntime:
    def __init__(self, *, channel: TaskChannel, context: TaskContext, core: AgentCore,
                 builder: ContextBuilder, tools: SessionTools, model: str,
                 limits: AgentLimits, options: Any = None,
                 model_selector: Callable[[int], str] | None = None,
                 planning: Planning | None = None, subagents: SubagentScope | None = None):
        self.channel = channel
        self.context = context
        self.builder = builder
        self.core = core
        self.tools = tools
        self.model = model
        self.limits = limits
        self.options = copy.deepcopy(options)
        self.model_selector = model_selector
        self.planning = planning
        self.subagents = subagents
        self._revision = 0
        self._revision_source = "host"
        self._pending: list[HostCommand] = []
        self._context_communications: list[dict[str, Any]] = []
        self._llm_attempts = 0
        self._usage_totals: dict[str, int | float] = {}
        self._retry_of: str | None = None
        self._budget_tokens = limits.context_window_tokens
        self._budget_source = limits.context_window_source if self._budget_tokens else "unknown"
        self._budget_model: str | None = None

    def _transition(self, status: TaskStatus) -> None:
        current = self.channel.status
        if current.terminal:
            raise RuntimeError("cannot transition a finished task")
        allowed = {
            TaskStatus.CREATED: {TaskStatus.RUNNING},
            TaskStatus.RUNNING: {TaskStatus.WAITING_FOR_HOST, TaskStatus.CANCELLING},
            TaskStatus.WAITING_FOR_HOST: {TaskStatus.RUNNING, TaskStatus.CANCELLING},
            TaskStatus.CANCELLING: set(),
        }
        if status != current and not status.terminal and status not in allowed[current]:
            raise RuntimeError(f"invalid transition: {current.value} -> {status.value}")
        self.channel.set_status(status)

    async def run(self) -> TaskResult:
        self._transition(TaskStatus.RUNNING)
        self.channel.emit("started", {"model": self.model, "limits": asdict(self.limits)})
        try:
            return await self._loop()
        except HostResponseTimeout:
            return await self._finish(TaskStatus.INCOMPLETE, "host_response_timeout")
        except ContextUnrecoverable as exc:
            return await self._finish(TaskStatus.INCOMPLETE, "context_unrecoverable", warnings=(str(exc),))
        except asyncio.CancelledError:
            return await self._finish(TaskStatus.CANCELLED, "runtime_cancelled")
        except Exception as exc:
            return await self._finish(TaskStatus.FAILED, "runtime_error", warnings=(
                f"{type(exc).__name__}: {exc}",
            ))

    async def _loop(self) -> TaskResult:
        while True:
            self._drain_commands()
            self._apply_instructions()
            if self.channel.status == TaskStatus.CANCELLING:
                return await self._finish(TaskStatus.CANCELLED, "host_cancelled")
            if self.channel.host_response_expired:
                return await self._finish(TaskStatus.INCOMPLETE, "host_response_timeout")
            if self.channel.agent.steps >= self.limits.max_steps:
                return await self._finish(TaskStatus.LIMIT_REACHED, "max_steps")
            self.tools.apply_pending()
            self._apply_instructions()
            number = self.channel.agent.steps + 1
            model = self.model_selector(number) if self.model_selector else self.model
            nonempty(model, "selected model")
            await self._compact(model)
            self._drain_commands()
            self._apply_instructions()
            if self.channel.status == TaskStatus.CANCELLING:
                return await self._finish(TaskStatus.CANCELLED, "host_cancelled")

            if self.channel.host_response_expired:
                return await self._finish(TaskStatus.INCOMPLETE, "host_response_timeout")

            self.tools.apply_pending()
            self._apply_instructions()
            offered = self.tools.snapshot()
            revision = self._revision
            messages = self.builder.build(self._context_snapshot())
            self.channel.update_state(agent={"steps": number, "model": model})
            llm_call_id = uuid4().hex
            records: dict[str, str] = {}
            self.channel.emit("step_started", {
                "step": number, "model": model, "llm_call_id": llm_call_id,
                "context_chars": estimate_context_chars(messages, offered.schemas),
                "context_compactions": self.context.compaction.count,
            })
            try:
                response = await self._wait_operation(lambda: self._call_llm(
                    llm_call_id=llm_call_id, purpose="decision",
                    records=records,
                    model=model, messages=messages, tools=offered.schemas, options=self.options,
                ))
            except ContextOverflow as exc:
                await self._recover_context(model, exc, llm_call_id, messages, offered.schemas)
                # Retry the same logical decision; no tools in the rejected call ran.
                self.channel.update_state(agent={"steps": number - 1})
                self._retry_of = llm_call_id
                continue
            self._retry_of = None
            if self.channel.status == TaskStatus.CANCELLING:
                self.channel.emit("response_discarded", {
                    "step": number, "llm_call_id": llm_call_id, "reason": "host_cancelled",
                }, artifact_ids=tuple(records.values()))
                return await self._finish(TaskStatus.CANCELLED, "host_cancelled")
            if self.channel.host_response_expired:
                self.channel.emit("response_discarded", {
                    "step": number, "llm_call_id": llm_call_id, "reason": "host_response_timeout",
                }, artifact_ids=tuple(records.values()))
                return await self._finish(TaskStatus.INCOMPLETE, "host_response_timeout")
            if revision != self._revision:
                self.channel.emit("response_discarded", {
                    "step": number, "llm_call_id": llm_call_id, "reason": f"new_{self._revision_source}_instruction",
                }, artifact_ids=tuple(records.values()))
                continue
            if response.finish_reason in {"length", "max_tokens", "content_filter"}:
                self.channel.emit("response_discarded", {
                    "step": number, "llm_call_id": llm_call_id, "reason": "incomplete_response",
                    "finish_reason": response.finish_reason,
                }, artifact_ids=tuple(records.values()))
                return await self._finish(TaskStatus.INCOMPLETE, response.finish_reason, response.content or "")
            if not response.tool_calls and not (response.content or "").strip():
                self.channel.emit("response_discarded", {
                    "step": number, "llm_call_id": llm_call_id, "reason": "empty_response",
                }, artifact_ids=tuple(records.values()))
                return await self._finish(TaskStatus.FAILED, "empty_response")

            invocation_index = self.context.conversation.record_response(response)
            response_ref = {"message_index": invocation_index, "artifact_id": records["normalized_response"]}
            self.channel.emit("response_recorded", {
                "llm_call_id": llm_call_id, "conversation_ref": response_ref,
            }, artifact_ids=(records["normalized_response"],))
            if response.content:
                self.channel.emit("message", {
                    "content": response.content, "llm_call_id": llm_call_id,
                    "conversation_ref": response_ref,
                }, artifact_ids=(records["normalized_response"],))
            if response.tool_calls:
                result = await self._run_batch(response, offered, revision, invocation_index, llm_call_id,
                                                records["normalized_response"])
                if result is not None:
                    return result

    async def _call_llm(self, *, llm_call_id: str, purpose: str, model: str,
                        messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                        options: Any, records: dict[str, str]) -> LLMResponse:
        self._llm_attempts += 1
        if self._budget_model is not None and model != self._budget_model:
            # A capacity reported by one model is not evidence for another.
            self._budget_tokens = self.limits.context_window_tokens
            self._budget_source = self.limits.context_window_source if self._budget_tokens else "unknown"
        self._budget_model = model
        metadata = {
            "llm_call_id": llm_call_id, "purpose": purpose, "model": model,
            "logical_step": self.channel.agent.steps, "attempt": self._llm_attempts,
            "retry_of": self._retry_of,
            "provider": f"{type(self.core.provider).__module__}.{type(self.core.provider).__qualname__}",
        }
        chars = estimate_context_chars(messages, tools)
        context_usage = {
            "context_window_tokens": self._budget_tokens, "context_window_source": self._budget_source,
            "input_tokens_estimate": math.ceil(chars / 3), "estimate_method": "json_chars_div_3",
            "estimate_is_exact": False,
            "remaining_tokens_estimate": max(0, self._budget_tokens - math.ceil(chars / 3))
            if self._budget_tokens else None,
        }
        if purpose == "decision":
            state = self._context_snapshot()
            prompt_id = self.channel.add_artifact("prompt_composition", {
                "blocks": self.builder.blocks(state), "variables": state.prompt_state,
                "context_usage": context_usage, "summary_upto": state.summary.summarized_upto,
                "tool_set_revision": self.tools.snapshot().revision,
            }, operation_id=llm_call_id)
            records["prompt_composition"] = prompt_id
            self.channel.emit("prompt_composed", {**metadata, "artifact_id": prompt_id,
                                                  **context_usage}, artifact_ids=(prompt_id,))
        loop, owner_thread = asyncio.get_running_loop(), get_ident()

        def record(kind: str, captured, observed_at: str) -> None:
            # An externally cancelled sync worker may outlive the Runtime. Its
            # late response is outside the closed snapshot, never a fake success.
            if self.channel.done:
                return
            artifact_id = self.channel.add_artifact(
                f"llm_{kind}", None, operation_id=llm_call_id, captured=captured,
            )
            records[kind] = artifact_id
            if kind == "common_request":
                self.channel.update_state(agent={"operation_artifact_id": artifact_id})
                self.channel.emit("context_built", {**metadata, "artifact_id": artifact_id},
                                  artifact_ids=(artifact_id,))
                if purpose == "decision":
                    self.tools.offered(llm_call_id, artifact_id)
                    for communication in self._context_communications:
                        self.channel.trace_communication("agent_message_context_built", {
                            **communication, "llm_call_id": llm_call_id,
                            "task_refs": [*communication["task_refs"],
                                          {"task_id": self.channel.task_id, "artifact_id": artifact_id}]})
                    self.context.conversation.mark_agent_messages_seen(
                        data["message_index"] for data in self._context_communications)
                    self._context_communications.clear()
            self.channel.emit(f"llm_{kind}", {**metadata, "artifact_id": artifact_id,
                                             "observed_at": observed_at}, artifact_ids=(artifact_id,))

        def observer(kind: str, data: Any) -> None:
            # Capture at the boundary before SDK/Host mutation, even on a worker.
            captured = capture(data, credentials=kind in {"common_request", "adapter_request"})
            observed_at = utc_timestamp()
            if get_ident() == owner_thread:
                record(kind, captured, observed_at)
            elif not loop.is_closed():
                loop.call_soon_threadsafe(record, kind, captured, observed_at)

        self.channel.update_state(agent={
            "activity": AgentActivity.CONTEXT_COMPACTION if purpose == "context_compaction" else AgentActivity.LLM_CALL,
            "operation_id": llm_call_id, "operation_kind": "llm", "operation_artifact_id": None,
            "model": model, "provider": metadata["provider"],
        })
        self.channel.emit("llm_call_started", {
            **metadata, "message_count": len(messages), "tool_schema_count": len(tools),
        })
        started_at = perf_counter()
        outcome: dict[str, Any] = {}
        try:
            response = await self.core.step(model=model, messages=messages, tools=tools, options=options,
                                            observer=observer)
            outcome = {
                "status": "succeeded", "finish_reason": response.finish_reason,
                "usage": usage_metrics(response.usage), "tool_call_count": len(response.tool_calls),
                "content_chars": len(response.content or ""),
            }
            for key, value in (outcome["usage"] or {}).items():
                if type(value) in (int, float):
                    self._usage_totals[key] = self._usage_totals.get(key, 0) + value
            outcome["cumulative_usage"] = dict(self._usage_totals)
            return response
        except asyncio.CancelledError:
            outcome = {"status": "cancelled"}
            raise
        except Exception as exc:
            outcome = {"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}
            raise
        finally:
            response_received = "common_response" in records or "adapter_response" in records
            self.channel.emit("llm_call_finished", {
                **metadata, **outcome, **context_usage, "duration_ms": (perf_counter() - started_at) * 1000,
                "response_received": response_received,
                "validation_succeeded": "normalized_response" in records,
                "artifacts": dict(records),
                "adapter_observation": "sdk_boundary" if "adapter_request" in records else "unavailable",
            }, artifact_ids=tuple(records.values()))
            self.channel.update_state(agent={"activity": AgentActivity.PREPARING,
                                             "operation_id": None, "operation_kind": None,
                                             "operation_artifact_id": None})

    def _context_snapshot(self):
        request = self.context.request
        tool_state = self.tools.integration.state() if self.tools.integration else None
        if tool_state and not tool_state["requests"] and not self.tools.integration.owner.tools_enabled:
            tool_state = None
        return replace(self.context.snapshot(plan=self.planning.snapshot() if self.planning else None),
                       tool_state=tool_state,
                       prompt_state={"role": request.role, "result_requirements": request.result_requirements,
                                     "parent_kind": "agent" if self.channel.task.parent_task_id else "host",
                                     "tools": self.tools.snapshot().schemas,
                                     "workspace": self.tools.workspace.snapshot() if self.tools.workspace else None})

    async def _recover_context(self, model, error, call_id, messages, schemas):
        if error.limit_tokens:
            self._budget_tokens, self._budget_source = error.limit_tokens, "provider_error"
        before = estimate_context_chars(messages, schemas)
        self.channel.emit("context_overflow", {
            "llm_call_id": call_id, "context_chars": before,
            "context_window_tokens": self._budget_tokens, "context_window_source": self._budget_source,
        })
        # Unknown windows stay unknown. The chunk ceiling is a recovery heuristic,
        # not a claimed model capacity; confirmed summary overflow halves it.
        budget = min(before // 2, self._budget_tokens * 2 if self._budget_tokens else 32000)
        while True:
            snapshot = self._context_snapshot()
            plan = self.context.compaction.recovery_plan(
                snapshot.conversation, context_chars=before, budget_chars=budget)
            if plan is None:
                raise ContextUnrecoverable("Required input or one complete history group cannot fit in a bounded summary.")
            try:
                await self._compact(model, plan=plan, recovery_of=call_id)
            except ContextOverflow:
                budget = min(budget // 2, estimate_context_chars(plan.messages, []) - 1)
                continue
            except (ValueError, TypeError) as exc:
                raise ContextUnrecoverable(f"Summary was unusable: {exc}") from exc
            if self.channel.status == TaskStatus.CANCELLING:
                return
            after = estimate_context_chars(self.builder.build(self._context_snapshot()), schemas)
            if after < before:
                return
            # Progress is measured by consumed source ranges, never a fixed retry
            # count. Stop when no raw group is left, including a growing summary.
            self.channel.emit("context_recovery_continued", {
                "recovery_of": call_id, "context_chars": after,
                "summarized_upto": self.context.compaction.summary.summarized_upto})

    async def _compact(self, model: str, *, plan=None, recovery_of=None) -> None:
        schemas = self.tools.snapshot().schemas
        snapshot = self._context_snapshot()
        plan = plan or self.context.compaction.plan(snapshot.conversation, context_chars=estimate_context_chars(
            self.builder.build(snapshot), schemas,
        ))
        if plan is None:
            return
        llm_call_id = uuid4().hex
        records: dict[str, str] = {}
        metadata = {**plan.event_payload(), "llm_call_id": llm_call_id, "recovery_of": recovery_of}
        self.channel.emit("context_compaction_started", metadata)
        started_at = perf_counter()
        try:
            response = await self._wait_operation(lambda: self._call_llm(
                llm_call_id=llm_call_id, purpose="context_compaction",
                records=records,
                model=model, messages=plan.messages, tools=[], options=self.options,
            ))
            if self.channel.status == TaskStatus.CANCELLING:
                self.channel.emit("context_compaction_cancelled", {
                    **metadata, "reason": "host_cancelled",
                    "duration_ms": (perf_counter() - started_at) * 1000,
                })
                return
            payload = self.context.compaction.apply(plan, response)
            payload["context_chars_after"] = estimate_context_chars(
                self.builder.build(self._context_snapshot()), schemas,
            )
            summary_id = self.channel.add_artifact("context_summary", {
                "summary": asdict(self.context.compaction.summary), "range": plan.event_payload(),
                "response_artifact_id": records["normalized_response"],
                "summary_truncated": payload["summary_truncated"],
            }, operation_id=llm_call_id, references=(records["normalized_response"],))
            self.channel.emit("context_compacted", {
                **payload, "llm_call_id": llm_call_id, "recovery_of": recovery_of, "summary_artifact_id": summary_id,
                "duration_ms": (perf_counter() - started_at) * 1000,
            }, artifact_ids=(summary_id,))
        except asyncio.CancelledError:
            self.channel.emit("context_compaction_cancelled", {
                **metadata, "reason": "runtime_cancelled",
                "duration_ms": (perf_counter() - started_at) * 1000,
            })
            raise
        except Exception as exc:
            self.channel.emit("context_compaction_failed", {
                **metadata, "error": {"type": type(exc).__name__, "message": str(exc)},
                "duration_ms": (perf_counter() - started_at) * 1000,
            })
            raise

    async def _run_batch(self, response: LLMResponse, offered: ToolSnapshot,
                         revision: int, invocation_index: int, llm_call_id: str,
                         response_artifact_id: str) -> TaskResult | None:
        invalid = len(response.tool_calls) > 1 and any(
            call.name == "task_complete" for call in response.tool_calls
        )
        terminal_reason: str | None = None
        for call in response.tool_calls:
            execution = ToolExecution(llm_call_id, invocation_index)
            execution.request_artifact_id = self.channel.add_artifact("tool_request", {
                "llm_call_id": llm_call_id, "tool_call_id": call.id, "name": call.name,
                "requested_arguments": call.arguments,
                "response_artifact_id": response_artifact_id,
            }, operation_id=execution.execution_id, references=(response_artifact_id,))
            self._drain_commands()
            if self.channel.status == TaskStatus.CANCELLING:
                outcome = tool_error("not_executed", "host_cancelled")
            elif self.channel.host_response_expired:
                terminal_reason = "host_response_timeout"
                outcome = tool_error("not_executed", terminal_reason)
            elif revision != self._revision:
                outcome = tool_error("not_executed", f"superseded_by_{self._revision_source}_instruction")
            elif terminal_reason:
                outcome = tool_error("not_executed", terminal_reason)
            elif invalid:
                outcome = tool_error("invalid_tool_batch", "task_complete must be called alone")
            elif self.channel.agent.tool_calls >= self.limits.max_tool_calls:
                terminal_reason = "max_tool_calls"
                outcome = tool_error("not_executed", terminal_reason)
            else:
                try:
                    outcome = await self._wait_operation(lambda: self._execute_tool(
                        call, offered, revision, execution,
                    ))
                except HostResponseTimeout:
                    terminal_reason = "host_response_timeout"
                    outcome = tool_error(terminal_reason, "Host did not answer in time")
                except asyncio.CancelledError:
                    self._record_tool_result(call, execution,
                                             tool_error("runtime_cancelled", "Runtime was cancelled"))
                    raise
            if call.name == "task_complete" and outcome["ok"] and self.channel.has_pending_input():
                outcome = tool_error("CommunicationError", "new incoming messages require another decision before completion")
            outcome = self._record_tool_result(call, execution, outcome)
            self._drain_commands()
            if (call.name == "task_complete" and outcome["ok"]
                    and self.channel.status != TaskStatus.CANCELLING and revision == self._revision):
                return await self._finish(TaskStatus.COMPLETED, "task_complete", outcome["output"])

        # Keep every assistant/tool pair contiguous before inserting host messages.
        self._apply_instructions()
        if self.channel.status == TaskStatus.CANCELLING:
            return await self._finish(TaskStatus.CANCELLED, "host_cancelled")
        if self.channel.host_response_expired:
            return await self._finish(TaskStatus.INCOMPLETE, "host_response_timeout")
        if terminal_reason:
            status = (TaskStatus.INCOMPLETE if terminal_reason == "host_response_timeout"
                      else TaskStatus.LIMIT_REACHED)
            return await self._finish(status, terminal_reason)
        return None

    def _record_tool_result(self, call: ToolCall, execution: ToolExecution,
                            outcome: dict[str, Any]) -> dict[str, Any]:
        result_index = len(self.context.conversation)
        outcome = self.context.conversation.record_tool(call, outcome)
        self.channel.update_state(agent={"tool_errors": self.channel.agent.tool_errors + int(not outcome["ok"])})
        error_type = outcome.get("error", {}).get("type")
        if execution.started_at is None:
            status = "skipped" if error_type in {"not_executed", "invalid_tool_batch"} else "rejected"
        elif error_type == "runtime_cancelled":
            status = "cancelled"
        else:
            status = "succeeded" if outcome["ok"] else "failed"
        llm_view = self.context.conversation.tool_view(outcome)
        result_id = self.channel.add_artifact("tool_result", {
            "raw_result_artifact_id": execution.raw_result_artifact_id,
            "dispatch_artifact_id": execution.dispatch_artifact_id,
            "request_artifact_id": execution.request_artifact_id,
            "llm_result": llm_view,
            "error": outcome.get("error"),
            "status": status, "executed": execution.started_at is not None,
            "duration_ms": execution.duration_ms,
        }, operation_id=execution.execution_id,
            references=tuple(ref for ref in (execution.request_artifact_id, execution.dispatch_artifact_id,
                                             execution.raw_result_artifact_id) if ref is not None))
        self.channel.emit("tool_finished", {
            **execution.payload(call), "status": status, "executed": execution.started_at is not None,
            "duration_ms": execution.duration_ms,
            "result": llm_view,
            "result_ref": {"message_index": result_index, "artifact_id": result_id},
        }, artifact_ids=(execution.request_artifact_id, result_id))
        return outcome

    async def _execute_tool(self, call: ToolCall, offered: ToolSnapshot,
                            revision: int, execution: ToolExecution) -> dict[str, Any]:
        # Recheck after scheduling: a host command may arrive before this coroutine starts.
        self._drain_commands()
        if self.channel.status == TaskStatus.CANCELLING or revision != self._revision or self.channel.host_response_expired:
            return tool_error("not_executed", "task_cancelled_or_superseded")
        asking = False
        try:
            self.tools.check_available(call.name, offered)
            self.channel.update_state(agent={"tool_calls": self.channel.agent.tool_calls + 1})
            execution.dispatch_artifact_id = self.channel.add_artifact("tool_dispatch", {
                "name": call.name, "passed_arguments": call.arguments,
                "request_artifact_id": execution.request_artifact_id,
                "tool_set_revision": offered.revision,
                "boundary": "SessionTools.execute",
            }, operation_id=execution.execution_id, references=(execution.request_artifact_id,))
            self.channel.update_state(agent={"activity": AgentActivity.TOOL_EXECUTION,
                                             "operation_id": execution.execution_id, "operation_kind": "tool",
                                             "operation_artifact_id": execution.dispatch_artifact_id})
            self.channel.emit("tool_started", {**execution.payload(call),
                                               "dispatch_artifact_id": execution.dispatch_artifact_id},
                              artifact_ids=(execution.request_artifact_id, execution.dispatch_artifact_id))
            execution.started_at = perf_counter()
            if call.name == "host_ask":
                asking = True
                self._transition(TaskStatus.WAITING_FOR_HOST)
            output = await self.tools.execute(call.name, call.arguments,
                                              step=self.channel.agent.steps,
                                              invocation_index=execution.invocation_index,
                                              tool_ref={
                                                  "tool_execution_id": execution.execution_id,
                                                  "tool_call_id": call.id,
                                                  "llm_call_id": execution.llm_call_id,
                                                  "message_index": execution.invocation_index,
                                                  "request_artifact_id": execution.request_artifact_id,
                                              })
            execution.duration_ms = (perf_counter() - execution.started_at) * 1000
            execution.raw_result_artifact_id = self.channel.add_artifact("tool_raw_result", {
                "output": output,
            }, operation_id=execution.execution_id)
            return {"ok": True, "output": output}
        except HostResponseTimeout:
            raise
        except QuestionInterrupted as exc:
            return tool_error("question_interrupted", str(exc))
        except ToolUnavailable as exc:
            return tool_error(exc.kind, str(exc))
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            return tool_error("tool_cancelled", "Tool raised CancelledError")
        except Exception as exc:
            return tool_error(type(exc).__name__, str(exc))
        finally:
            if execution.started_at is not None and execution.duration_ms is None:
                execution.duration_ms = (perf_counter() - execution.started_at) * 1000
            if asking and self.channel.status != TaskStatus.CANCELLING:
                self._transition(TaskStatus.RUNNING)
            if execution.started_at is not None:
                self.channel.update_state(agent={"activity": AgentActivity.PREPARING,
                                                 "operation_id": None, "operation_kind": None,
                                                 "operation_artifact_id": None})

    async def _wait_operation(self, factory: Callable[[], Awaitable[T]]) -> T:
        """Receive commands while awaiting external work, without detaching its work.

        Public cancel waits for external sync/async operations; only our own question
        wait is interrupted. Python threads cannot safely be forcibly stopped.
        """
        operation = asyncio.create_task(factory())
        wake: asyncio.Task[None] | None = None
        try:
            while not operation.done():
                self._drain_commands()
                wake = asyncio.create_task(self.channel.wait_commands())
                await asyncio.wait({operation, wake}, return_when=asyncio.FIRST_COMPLETED)
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
                wake = None
            self._drain_commands()
            return await operation
        finally:
            if wake is not None:
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)

    def _drain_commands(self) -> None:
        for command in self.channel.take_commands():
            if command.type == "cancel":
                self._transition(TaskStatus.CANCELLING)
                self.channel.interrupt_question("host_cancelled")
                self.channel.interrupt_communication_wait("host_cancelled")
                if self.subagents is not None:
                    self.subagents.interrupt_wait("host_cancelled")
                self.channel.acknowledge(command)
            elif self.channel.status == TaskStatus.CANCELLING:
                self.channel.acknowledge(command, reason="task_is_cancelling")
            elif command.type == "instruction":
                self._revision += 1
                self._revision_source = "host"
                self._pending.append(command)
                self.channel.interrupt_question("superseded_by_host_instruction")
                self.channel.interrupt_communication_wait("superseded_by_host_instruction")
                if self.subagents is not None:
                    self.subagents.interrupt_wait("superseded_by_host_instruction")
            elif command.type == "agent_instruction":
                self._revision += 1
                self._revision_source = "parent"
                self.channel.interrupt_communication_wait("superseded_by_parent_instruction")
            elif command.type == "host_question_ready":
                self.channel.collect_host_answer()
            elif command.type == "answer":
                accepted = self.channel.accept_answer(command.payload["question_id"], command.payload["text"])
                self.channel.acknowledge(command, reason=None if accepted else "no_matching_pending_question")
            else:
                self.channel.acknowledge(command, reason="unsupported_command")

    def _apply_instructions(self) -> None:
        for command in self._pending:
            if self.channel.status == TaskStatus.CANCELLING:
                self.channel.acknowledge(command, reason="task_is_cancelling")
            else:
                self.context.conversation.add_instruction(command.payload["content"])
                self.channel.acknowledge(command)
        self._pending.clear()
        # This method is called only outside assistant/tool batches. Agent input
        # never changes the instruction revision unless explicitly an instruction.
        received, sent = self.channel.take_agent_messages()
        for message in sent:
            index = self.context.conversation.record_agent_message(message, outgoing=True)
            self.channel.emit("agent_message_recorded", {"message_id": message.message_id,
                "request_id": message.request_id, "direction": "sent", "message_index": index})
        for message, artifact_id in received:
            index = self.context.conversation.record_agent_message(message)
            data = {"message_id": message.message_id, "request_id": message.request_id,
                    "kind": message.kind, "from_task_id": message.from_task_id,
                    "to_task_id": message.to_task_id, "message_index": index,
                    "task_refs": [{"task_id": message.from_task_id or self.channel.task_id, "artifact_id": artifact_id}]}
            self.channel.trace_communication("agent_message_applied", data)
            self._context_communications.append(data)
        for request in self.channel.requests():
            if request.status != "pending":
                self.context.conversation.resolve_agent_request(request.message.request_id)
        for question_id, text in self.channel.take_host_answers():
            index = self.context.conversation.record_host_answer(question_id, text)
            self.channel.emit("host_answer_applied", {"question_id": question_id, "message_index": index})
        for data, artifact_id in self.channel.take_tool_updates():
            index = self.context.conversation.record_tool_update(data)
            self.channel.emit("tool_access_context_applied", {"request_id": data["request_id"],
                "status": data["status"], "message_index": index}, artifact_ids=(artifact_id,))
        self.channel.clear_communication_interrupt()

    async def _finish(self, status: TaskStatus, reason: str, output: str = "",
                warnings: tuple[str, ...] = ()) -> TaskResult:
        self._drain_commands()
        self._apply_instructions()
        if self.channel.status == TaskStatus.CANCELLING:
            status, reason, output = TaskStatus.CANCELLED, "host_cancelled", ""
        if self.subagents is not None:
            await self._wait_operation(lambda: self.subagents.finish_parent(status))
            self._drain_commands()
            self._apply_instructions()
            if self.channel.status == TaskStatus.CANCELLING:
                status, reason, output = TaskStatus.CANCELLED, "host_cancelled", ""
        if status == TaskStatus.LIMIT_REACHED:
            self.channel.emit("limit_reached", {
                "limit": reason, "steps": self.channel.agent.steps, "tool_calls": self.channel.agent.tool_calls,
                "maximum": getattr(self.limits, reason),
            })
        self.tools.close()
        result = TaskResult(
            task_id=self.channel.task_id, status=status, output=output, stop_reason=reason,
            steps=self.channel.agent.steps, tool_calls=self.channel.agent.tool_calls, tool_errors=self.channel.agent.tool_errors,
            warnings=warnings, **self.context.result_fields(),
            plan=self.planning.snapshot() if self.planning else None,
            workspace=self.tools.workspace.snapshot() if self.tools.workspace else None,
        )
        context_id = self.channel.add_artifact("final_context", {
            "messages": result.messages, "memos": result.memos,
            "context_summary": result.context_summary, "context_compactions": result.context_compactions,
            "workspace": result.workspace,
        })
        self._transition(status)
        self.channel.close(result, context_artifact_id=context_id)
        return replace(result, tracer=self.channel.tracer_snapshot(),
                       tree=await self.subagents.snapshot() if self.subagents is not None and
                            self.channel.task.depth == 0 else None)
