"""Public composition root. Provider, model and tool ownership stay with the host."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from typing import Any, Callable

from shs_agent.tool import ToolExecutor, ToolRegistry
from shs_agent.workspace import Workspace

from .context import ContextBuilder, TaskContext
from .communication.endpoint import host_connection
from .core import AgentCore
from .planning import Planning
from .runtime import TaskRuntime
from .session import TaskSession
from .session.channel import TaskChannel
from .subagent import SubagentConfig, SubagentManager
from .subagent.models import INHERIT_OPTIONS
from .tools import SessionTools
from .tools.integration import ToolIntegration, tool_ids
from .types import (AgentLimits, LLMProvider, TaskRequest, TaskResult,
                    ToolExecutorLike, ToolRegistryLike, nonempty)


class ProblemSolver:
    """Create independent tasks without loading models or contacting a server.

    A default provider may be supplied for compatibility; each task may override
    it. Host-owned provider/executor instances must support the host's chosen
    concurrency. Passing no registry creates a fresh empty registry per task.
    """

    def __init__(self, provider: LLMProvider | None = None):
        self._default_provider = provider

    def create_task(self, *, request: TaskRequest, model: str,
                    provider: LLMProvider | None = None,
                    registry: ToolRegistryLike | None = None,
                    executor: ToolExecutorLike | None = None,
                    limits: AgentLimits | None = None, options: Any = None,
                    model_selector: Callable[[int], str] | None = None,
                    subagents: dict[str, SubagentConfig] | None = None,
                    host_tools: ToolRegistry | None = None,
                    allowed_tool_ids: tuple[str, ...] = (), workspace: Workspace | None = None,
                    delegable_tool_ids: tuple[str, ...] = ()) -> TaskSession:
        """Create a dormant session. Call session.start() to execute it."""
        nonempty(model, "model")
        if not isinstance(request, TaskRequest):
            raise TypeError("request must be TaskRequest")
        if limits is not None and not isinstance(limits, AgentLimits):
            raise TypeError("limits must be AgentLimits")
        if model_selector is not None and not callable(model_selector):
            raise TypeError("model_selector must be callable")
        provider = provider if provider is not None else self._default_provider
        if provider is None:
            raise ValueError("supply a provider for this task or ProblemSolver")
        registry, executor = self._resolve_tools(registry, executor)
        limits = limits if limits is not None else AgentLimits()
        allowed_tool_ids = tool_ids(allowed_tool_ids)
        delegable_tool_ids = tool_ids(delegable_tool_ids)
        if workspace is not None and not isinstance(workspace, Workspace):
            raise TypeError("workspace must be Workspace or None")
        if host_tools is None and allowed_tool_ids:
            raise ValueError("allowed_tool_ids requires host_tools")
        integration = ToolIntegration(host_tools, enable_tools=bool(delegable_tool_ids) or
            (isinstance(subagents, dict) and any(isinstance(c, SubagentConfig) and c.delegable_tool_ids
                                                for c in subagents.values())))
        channel = TaskChannel(request, model=model,
                              provider=f"{type(provider).__module__}.{type(provider).__qualname__}")
        manager = None
        if subagents is not None:
            if not isinstance(subagents, dict) or not subagents:
                raise ValueError("subagents must be a nonempty dict of host-approved config profiles")
            if host_tools is None and any(isinstance(config, SubagentConfig) and config.allowed_tool_ids
                                          for config in subagents.values()):
                raise ValueError("child allowed_tool_ids requires host_tools")
            # Freeze host options/profile dictionaries; Provider/Tool instances remain shared.
            parent_options = copy.deepcopy(options)
            configs = {key: replace(value, options=copy.deepcopy(value.options))
                       if isinstance(value, SubagentConfig) else value for key, value in subagents.items()}
            settings = {channel.task_id: (provider, parent_options, limits)}

            def resolve(parent: TaskChannel, config: SubagentConfig) -> SubagentConfig:
                parent_provider, inherited_options, parent_limits = settings[parent.task_id]
                child_limits = config.limits
                if child_limits is not None and (child_limits.max_total_subagents is not None or
                                                  child_limits.max_subagent_depth is not None):
                    raise ValueError("child profiles specify per-task limits only; root budget belongs to the manager")
                return replace(config, provider=config.provider if config.provider is not None else parent_provider,
                               model=config.model if config.model is not None else parent.agent.model,
                               options=copy.deepcopy(inherited_options if config.options is INHERIT_OPTIONS else config.options),
                               limits=child_limits if child_limits is not None else replace(
                                   parent_limits, max_total_subagents=None, max_subagent_depth=None))

            def create_child(child_request: TaskRequest, config: SubagentConfig,
                             child_channel: TaskChannel) -> TaskSession:
                child_registry, child_executor = self._resolve_tools(config.registry, config.executor)
                scope = manager.bind(child_channel, config.allowed_subagent_config_ids)
                session = self._assemble_task(request=child_request, provider=config.provider, model=config.model,
                    registry=child_registry, executor=child_executor, limits=config.limits,
                    options=config.options, model_selector=None, channel=child_channel,
                    subagents=scope,
                    integration=integration, allowed_tool_ids=config.allowed_tool_ids,
                    workspace=config.workspace, delegable_tool_ids=config.delegable_tool_ids,
                    parent_plan=lambda revision=None: manager.plan(child_channel.task.parent_task_id, revision),
                    communication=manager.communication(child_channel, can_manage=scope.can_spawn))
                settings[child_channel.task_id] = (config.provider, copy.deepcopy(config.options), config.limits)
                return session

            manager = SubagentManager(channel=channel, limits=limits, configs=configs,
                                      resolve=resolve, create=create_child)
        return self._assemble_task(request=request, provider=provider, model=model,
                                   registry=registry, executor=executor, limits=limits,
                                   options=options, model_selector=model_selector, channel=channel,
                                   subagents=manager.bind(channel, manager.config_ids) if manager is not None else None,
                                   host_manager=manager,
                                   integration=integration, allowed_tool_ids=allowed_tool_ids,
                                   workspace=workspace, delegable_tool_ids=delegable_tool_ids,
                                   communication=manager.communication(channel) if manager is not None else
                                                 host_connection(channel, limits.host_response_timeout))

    @staticmethod
    def _resolve_tools(registry, executor):
        if registry is None:
            if executor is not None:
                raise ValueError("registry is required when supplying an executor")
            registry = ToolRegistry()

        if executor is None:
            if not isinstance(registry, ToolRegistry):
                raise ValueError("a custom registry requires an explicit executor")
            executor = ToolExecutor(registry)

        if isinstance(executor, ToolExecutor) and executor.registry is not registry:
            raise ValueError("ToolExecutor and registry must refer to the same registry")
        
        if not callable(getattr(registry, "schemas", None)):
            raise TypeError("registry must provide schemas()")
        
        if not callable(getattr(executor, "execute", None)):
            raise TypeError("executor must provide execute()")
        
        return registry, executor

    def _assemble_task(self, *, request: TaskRequest, provider: LLMProvider, model: str,
                       registry: ToolRegistryLike, executor: ToolExecutorLike,
                       limits: AgentLimits, options: Any,
                       model_selector: Callable[[int], str] | None,
                       channel: TaskChannel, subagents=None,
                       communication=None, host_manager: SubagentManager | None = None,
                       integration=None, allowed_tool_ids=(), workspace=None, delegable_tool_ids=(),
                       parent_plan=None) -> TaskSession:
        """One composition path for a task, independent of its place in a tree."""
        core = AgentCore(provider)
        context = TaskContext(channel.task_id, request, limits)
        builder = ContextBuilder(limits)
        planning = Planning(channel.task_id, publish=channel.publish_plan)
        tools = SessionTools(registry, executor, context, channel, limits, planning=planning, subagents=subagents,
                             communication=communication, integration=integration, allowed_tool_ids=allowed_tool_ids,
                             workspace=workspace, delegable_tool_ids=delegable_tool_ids, parent_plan=parent_plan)
        if subagents is not None:
            subagents.bind_environment(tools, planning)
        runtime = TaskRuntime(channel=channel, context=context, core=core, builder=builder, tools=tools,
                              model=model, limits=limits, options=options, model_selector=model_selector,
                              planning=planning, subagents=subagents)
        return TaskSession(channel, runtime.run, subagents=host_manager, tool_access=tools.integration, tools=tools)

    async def start(self, task: str | TaskRequest, *, model: str,
                    provider: LLMProvider | None = None,
                    registry: ToolRegistryLike | None = None,
                    executor: ToolExecutorLike | None = None,
                    limits: AgentLimits | None = None, options: Any = None,
                    model_selector: Callable[[int], str] | None = None,
                    subagents: dict[str, SubagentConfig] | None = None,
                    host_tools: ToolRegistry | None = None,
                    allowed_tool_ids: tuple[str, ...] = (), workspace: Workspace | None = None,
                    delegable_tool_ids: tuple[str, ...] = ()) -> TaskSession:
        """Start execution and immediately return its interactive handle."""
        request = TaskRequest(task) if isinstance(task, str) else task
        session = self.create_task(request=request, provider=provider, model=model,
                                   registry=registry, executor=executor, limits=limits,
                                   options=options, model_selector=model_selector, subagents=subagents,
                                   host_tools=host_tools, allowed_tool_ids=allowed_tool_ids,
                                   workspace=workspace, delegable_tool_ids=delegable_tool_ids)
        await session.start()
        return session

    async def solve(self, task: str | TaskRequest, *, model: str,
                    provider: LLMProvider | None = None,
                    registry: ToolRegistryLike | None = None,
                    executor: ToolExecutorLike | None = None,
                    limits: AgentLimits | None = None, options: Any = None,
                    model_selector: Callable[[int], str] | None = None,
                    subagents: dict[str, SubagentConfig] | None = None,
                    host_tools: ToolRegistry | None = None,
                    allowed_tool_ids: tuple[str, ...] = (), workspace: Workspace | None = None,
                    delegable_tool_ids: tuple[str, ...] = ()) -> TaskResult:
        """Wait for a final result. Interactive hosts should use start().

        Without an answering host, host_ask ends at host_response_timeout (or
        waits indefinitely when explicitly configured with None).
        """
        session = await self.start(task, provider=provider, model=model, registry=registry,
                                    executor=executor, limits=limits, options=options,
                                    model_selector=model_selector, subagents=subagents,
                                    host_tools=host_tools, allowed_tool_ids=allowed_tool_ids,
                                    workspace=workspace, delegable_tool_ids=delegable_tool_ids)
        try:
            return await session.result()
        except asyncio.CancelledError:
            if not session.done:
                await session.cancel()
            raise
