import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shs_agent import AgentLimits, Mount, TaskStatus, TaskTreeSnapshot, Workspace
from shs_agent.workspace.tools import FileTools
from shs_agent.agent.tools.session_tools import SessionTools
from tests.fakes import AsyncCase, FakeProvider, calls, complete, event_of, registry_for, tool_results
from tests.test_subagent import profile, spawn, outputs, child_ids, wait_children
from tests.test_tool_integration import state_data
from tests.test_tool_integration import TreeProvider


class WorkspaceFilesTests(unittest.TestCase):
    def test_host_command_example_resolves_logical_paths_to_the_actual_workspace(self):
        from examples.workspace_coding_host import command_registry
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            (folder / "main.py").write_text("from pathlib import Path\nimport sys\nprint(Path(sys.argv[1]).read_text())\n")
            (folder / "input.txt").write_text("actual file")
            command = command_registry(Workspace(root)).get("run_python").handler
            result = command(["python", "/workspace/main.py", "/workspace/input.txt"])
            self.assertEqual(result["exit_code"], 0, result["stderr"])
            self.assertEqual(result["stdout"].strip(), "actual file")

    def test_real_crud_search_subdirectory_and_process_cwd(self):
        cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as folder:
            files = FileTools(Workspace(folder))
            files.file_write("src/main.py", "print('old')\n")
            self.assertEqual(files.file_search(".", "old")["matches"][0]["line"], 1)
            files.file_edit("src/main.py", "old", "new")
            self.assertIn("new", files.file_read("src/main.py")["content"])
            self.assertEqual(len(files.file_list("src")["entries"]), 1)
            files.file_delete("src/main.py")
            files.file_delete("src")
            self.assertEqual(files.file_list(".")["entries"], [])
            with self.assertRaises(PermissionError):
                files.file_delete(".")
        self.assertEqual(Path.cwd(), cwd)

    def test_mounts_readonly_traversal_drives_and_exact_edit(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as extra:
            workspace = Workspace(root, mounts=(Mount("/reference", extra, read_only=True),))
            for path in ("../escape", "/outside", "C:/Windows/win.ini", "//server/share"):
                with self.subTest(path=path), self.assertRaises((PermissionError, ValueError)):
                    workspace.resolve(path)
            with self.assertRaises(PermissionError):
                FileTools(workspace).file_write("/reference/no.txt", "forbidden")
            files = FileTools(workspace)
            files.file_write("a.txt", "same same")
            with self.assertRaises(ValueError):
                files.file_edit("a.txt", "same", "changed")
            self.assertEqual(files.file_read("a.txt", 0, 4)["content"], "same")
            self.assertTrue(files.file_read("a.txt", 0, 4)["has_more"])

    def test_symlink_escape_is_not_read_or_written(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            (Path(outside) / "secret.txt").write_text("outside")
            try:
                (Path(root) / "link").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Host does not permit symlink creation")
            files = FileTools(Workspace(root))
            with self.assertRaises(PermissionError):
                files.file_read("link/secret.txt")
            with self.assertRaises(PermissionError):
                files.file_write("link/secret.txt", "modified")
            self.assertEqual(files.file_search(".", "outside")["matches"], [])

    def test_delegation_narrows_scope_and_does_not_invent_authority(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "part").mkdir()
            with self.assertRaises(PermissionError):
                Workspace(root).delegate(["/workspace/part"])
            parent = Workspace(root, delegable=True)
            child = parent.delegate(["/workspace/part"])
            self.assertEqual(child.cwd, "/workspace/part")
            with self.assertRaises(PermissionError):
                child.resolve("../elsewhere")
            with self.assertRaises(PermissionError):
                child.delegate()
            self.assertTrue(parent.delegate(["/workspace"], redelegate=True).mounts[0].delegable)

    def test_nested_mount_restrictions_cannot_be_erased_by_delegating_parent(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
            workspace = Workspace(root, delegable=True,
                                  mounts=(Mount("/workspace/private", private, read_only=True),))
            with self.assertRaises(PermissionError):
                workspace.delegate(["/workspace"])

    def test_missing_workspace_root_is_not_created(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "missing"
            with self.assertRaises(FileNotFoundError):
                Workspace(missing)
            self.assertFalse(missing.exists())


class WorkspaceRuntimeTests(AsyncCase):
    async def test_host_grant_applied_after_batch_not_midway(self):
        with tempfile.TemporaryDirectory() as folder:
            gate = self.gate(None)
            request = ("workspace_request", {"paths": ["/workspace"], "reason": "write code"})
            provider = FakeProvider(calls(request, ("hold", {}), ("file_write", {"path": "early", "content": "no"})),
                                    calls(("file_write", {"path": "ok.py", "content": "print(1)\n"})), complete())
            session = await self.start(provider, registry=registry_for(hold=gate))
            await gate.entered.wait()
            state = await session.resource_state()
            request_id = state["requests"][0]["request_id"]
            await session.grant_workspace(request_id=request_id, workspace=Workspace(folder))
            self.assertIsNone(await session.workspace())
            gate.release.set()
            result = await self.result(session)
            self.assertEqual(result.status, TaskStatus.COMPLETED)
            self.assertFalse((Path(folder) / "early").exists())
            self.assertEqual((Path(folder) / "ok.py").read_text(), "print(1)\n")
            self.assertEqual(result.workspace["revision"], 1)
            self.assertEqual(tool_results(result)[2]["error"]["type"], "unknown_tool")
            self.assertIn("workspace", provider.requests[-1]["messages"][0]["content"])
            prompt = [a for a in result.tracer.artifacts.values() if a.kind == "prompt_composition"][-1]
            self.assertEqual(prompt.data["variables"]["workspace"]["revision"], 1)

    async def test_parent_supplies_workspace_without_host_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            leaf = FakeProvider(
                calls(("workspace_request", {"paths": ["/workspace"], "reason": "need files"})),
                lambda r: calls(("resource_wait", {"request_id": outputs(r, "workspace_request")[0]["output"]["request_id"]})),
                calls(("file_write", {"path": "child.txt", "content": "child"})), complete())
            class Parent:
                async def chat(self, **request):
                    if not child_ids(request):
                        return calls(spawn())
                    data = state_data(request)
                    pending = [r for r in data["requests"] if r["status"] == "requested"]
                    if pending:
                        return calls(("resource_grant", {"request_id": pending[0]["request_id"]}))
                    if outputs(request, "subagent_wait") and outputs(request, "subagent_wait")[-1]["output"]["reason"] == "terminated":
                        return complete()
                    return wait_children(request)
            session = await self.start(Parent(), workspace=Workspace(folder, delegable=True),
                host_tools=registry_for(), subagents={"child": profile(leaf)},
                limits=AgentLimits(max_total_subagents=1, max_subagent_depth=1))
            result = await self.result(session)
            self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
            self.assertEqual((Path(folder) / "child.txt").read_text(), "child")
            tree = await session.subagents.snapshot()
            self.assertFalse(any(e.type == "tool_host_approval_requested" for t in tree.traces.values() for e in t.events))
            self.assertEqual(len(tree.traces), 2)
            replay = list(TaskTreeSnapshot.from_json(tree.to_json()).events(after_sequence=3))
            live = [e async for e in session.events(scope="tree", after_sequence=3)]
            self.assertEqual(replay, live)
            self.assertEqual(live[-1]["tree_sequence"], sum(len(t.events) for t in tree.traces.values()))

    async def test_spawn_supplies_small_role_workspace_tool_scope_and_parent_plan_none(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "part").mkdir()
            leaf = FakeProvider(calls(("plan_read", {"scope": "parent"}),
                                     ("file_write", {"path": "child.py", "content": "# real file\n"}),
                                     ("echo", {})), complete())
            root = FakeProvider(calls(spawn(role="coder", result_requirements="written file",
                                               workspace_paths=["/workspace/part"], tool_ids=["echo"])),
                                wait_children, complete())
            session = await self.start(root, workspace=Workspace(folder, delegable=True),
                registry=registry_for(echo=lambda: 42), delegable_tool_ids=("echo",),
                subagents={"child": profile(leaf)}, limits=AgentLimits(max_total_subagents=1, max_subagent_depth=1))
            result = await self.result(session)
            self.assertEqual(result.status, TaskStatus.COMPLETED)
            self.assertTrue((Path(folder) / "part" / "child.py").exists())
            self.assertIsNone(outputs(leaf.requests[-1], "plan_read")[0]["output"]["plan"])
            self.assertEqual(outputs(leaf.requests[-1], "echo")[0]["output"], 42)
            self.assertIn("coder", leaf.requests[0]["messages"][0]["content"])
            child = next(t for key, t in result.tree.traces.items() if key != session.task_id)
            original = child.artifact(child.state().task.input_artifact_id).data
            self.assertNotIn("Assigned role:", original["system_prompt"])

    async def test_workspace_publish_failure_keeps_previous_environment(self):
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            gate = self.gate(complete())
            session = await self.start(FakeProvider(gate), workspace=Workspace(old))
            await gate.entered.wait()
            before = await session.workspace()
            with patch.object(SessionTools, "_record_tool_set", side_effect=ValueError("publish failed")):
                with self.assertRaises(ValueError):
                    session._tools.apply_workspace(Workspace(new))
            self.assertEqual(await session.workspace(), before)
            gate.release.set()
            await self.result(session)

    async def test_host_supplies_new_tool_without_preconfigured_catalog(self):
        request = ("tool_request", {"requested_tool": "fresh", "reason": "need data", "intended_use": "lookup"})
        gate = self.gate(calls(("fresh", {})))
        provider = FakeProvider(calls(request), gate, calls(("fresh", {})), complete())
        session = await self.start(provider)
        await gate.entered.wait()
        request_id = (await session.resource_state())["requests"][0]["request_id"]
        await session.supply_tool(request_id=request_id, tool=registry_for(fresh=lambda: 91).get("fresh"))
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(tool_results(result)[1]["error"]["type"], "unknown_tool")
        self.assertEqual(tool_results(result)[2]["output"], 91)
        self.assertEqual(result.status, TaskStatus.COMPLETED)

    async def test_depth_two_workspace_request_reaches_host_and_applies_to_leaf_only(self):
        with tempfile.TemporaryDirectory() as folder:
            leaf = FakeProvider(calls(("workspace_request", {"paths": ["/workspace"], "reason": "need files"})),
                lambda r: calls(("resource_wait", {"request_id": outputs(r, "workspace_request")[0]["output"]["request_id"]})),
                calls(("file_write", {"path": "leaf.py", "content": "# leaf\n"})), complete())
            session = await self.start(TreeProvider("middle"), host_tools=registry_for(),
                subagents={"middle": profile(TreeProvider("leaf"), allowed_subagent_config_ids=("leaf",)),
                           "leaf": profile(leaf)},
                limits=AgentLimits(max_total_subagents=2, max_subagent_depth=2))
            approval = await event_of(session, "tool_host_approval_requested")
            target = session.subagents.child(approval.payload["requesting_task_id"])
            await target.grant_workspace(request_id=approval.payload["request_id"], workspace=Workspace(folder))
            result = await self.result(session)
            self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
            self.assertTrue((Path(folder) / "leaf.py").exists())
            self.assertIsNone(await session.workspace())
            self.assertIsNotNone(await target.workspace())
            self.assertEqual(len(result.tree.traces), 3)
            TaskTreeSnapshot.from_json(result.tree.to_json())

    async def test_parent_can_grant_only_explicitly_delegable_applied_tool(self):
        leaf = FakeProvider(calls(("tool_request", {"requested_tool": "echo", "reason": "need data", "intended_use": "lookup"})),
            lambda r: calls(("resource_wait", {"request_id": outputs(r, "tool_request")[0]["output"]["request_id"]})),
            calls(("echo", {})), complete())
        class Parent:
            async def chat(self, **request):
                if not child_ids(request):
                    return calls(spawn())
                pending = [r for r in state_data(request)["requests"] if r["status"] == "requested"]
                if pending:
                    return calls(("resource_grant", {"request_id": pending[0]["request_id"]}))
                waits = outputs(request, "subagent_wait")
                if waits and waits[-1]["output"]["reason"] == "terminated":
                    return complete()
                return wait_children(request)
        session = await self.start(Parent(), registry=registry_for(echo=lambda: 43),
            delegable_tool_ids=("echo",), subagents={"child": profile(leaf)},
            limits=AgentLimits(max_total_subagents=1, max_subagent_depth=1))
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(outputs(leaf.requests[-1], "echo")[0]["output"], 43)
        self.assertFalse(any(e.type == "tool_host_approval_requested" for e in result.tracer.events))

    async def test_parent_plan_read_does_not_grant_parent_write_access(self):
        child = FakeProvider(calls(("plan_read", {"scope": "parent"}),
                                   ("plan_update", {"updates": [], "expected_revision": 1, "reason": "try parent"}),
                                   ("plan_read", {"scope": "ancestor"})), complete())
        parent = FakeProvider(calls(("plan_create", {"reason": "parent plan", "items": [
            {"item_id": "work", "description": "work to do"}]})), calls(spawn()), wait_children, complete())
        session = await self.start(parent, subagents={"child": profile(child)},
            limits=AgentLimits(max_total_subagents=1, max_subagent_depth=1))
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        read = outputs(child.requests[-1], "plan_read")
        self.assertEqual(read[0]["output"]["plan"]["task_id"], session.task_id)
        self.assertFalse(read[1]["ok"])
        self.assertFalse(outputs(child.requests[-1], "plan_update")[0]["ok"])
        self.assertEqual(result.plan.revision, 1)

    async def test_parent_cannot_delegate_custom_executor_by_copying_raw_handler(self):
        from shs_agent.tool import ToolExecutor
        registry = registry_for(echo=lambda: "raw")
        class CustomExecutor(ToolExecutor):
            pass
        parent = FakeProvider(calls(spawn(tool_ids=["echo"])), complete())
        session = await self.start(parent, registry=registry, executor=CustomExecutor(registry),
            delegable_tool_ids=("echo",), subagents={"child": profile(FakeProvider(complete()))},
            limits=AgentLimits(max_total_subagents=1, max_subagent_depth=1))
        result = await self.result(session)
        self.assertFalse(tool_results(result)[0]["ok"])
        self.assertEqual(len(result.tree.traces), 1)
