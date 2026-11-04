"""Real LLM coding in a Host-provided workspace. No filesystem work on import.

PYTHONPATH=src python examples/workspace_coding_host.py --workdir artifacts/coding-run
Host owns subprocess policy, lifetime, independent acceptance and trace export.
The command tool executes Python code; logical file mounts are not OS isolation.
"""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys

from shs_agent import AgentLimits, ProblemSolver, SubagentConfig, TaskRequest, TaskStatus, Workspace
from shs_agent.llm import ChatOptions
from shs_agent.tool import Tool, ToolExecutor, ToolRegistry, ToolSpec


REQUIREMENTS = """Repair csv_totals.py and create test_csv_totals.py in /workspace.
Interface: python csv_totals.py INPUT.csv prints exactly one JSON object mapping
each category to the sum of its amount column. Use csv.DictReader, numeric sums,
UTF-8, and the standard library. Duplicate categories accumulate, negative and
decimal amounts work, quoted commas and Unicode categories work, an empty file
with only headers returns {}. An invalid numeric amount exits nonzero and prints
an informative error on stderr. The supplied CLI is deliberately broken.
Delegate implementation to one worker and unittest tests to a second worker,
with precise instruction/role/background/result_requirements. Give both the
shared workspace_paths ['/workspace'] and tool_ids ['run_python']. config_id is
'worker'. Do NOT put file_write or any other built-in tool in tool_ids: workspace_paths
automatically provides all the built-in file tools. If a spawn is rejected, correct
its arguments and try again; this acceptance requires two successful children.
Wait for children and read their results. Run the tests and sample CLI
yourself; repair failures if needed before task_complete. Do not just describe code.
The workers must actually write files and use run_python to execute their checks.
Use standard unittest discovery from the workspace (python -m unittest discover -v).
Root must call verify_requirements and inspect every check before completing.
Passing agent-written tests alone is insufficient: stdout must be valid JSON
accepted by json.loads, not a Python dict representation. Fix any failed Host check.
No dependencies, pip, unrelated files or shell commands are needed.
An initial failing Host acceptance check is preserved in the report.
Use only the supplied schema's argument names. Children do not have spawn tools.
Use subagent_wait for pending work instead of repeatedly polling status.
"""


def command_registry(workspace):
    def run_python(args):
        if not isinstance(args, list) or not args or not all(isinstance(x, str) for x in args):
            raise ValueError("args must be a nonempty string array")
        args = list(args)
        if args[0] in {"python", "python3", "python.exe"}:
            args = args[1:]
        if not args:
            raise ValueError("provide a script or -m unittest")
        if args[0] == "-m":
            if len(args) < 2 or args[1] != "unittest":
                raise ValueError("only the unittest module is allowed")
        elif args[0].endswith(".py"):
            args[0] = str(workspace.resolve(args[0]))
        else:
            raise ValueError("Use args=['-m', 'unittest', 'discover', '-v'] for tests, "
                             "or args=['/workspace/script.py', ...]. Save inline code to a file first.")
        args = [str(workspace.resolve(arg)) if arg.startswith("/") else arg for arg in args]
        process = subprocess.run([sys.executable, *args], cwd=workspace.resolve("."),
                                 capture_output=True, text=True, encoding="utf-8",
                                 timeout=60, env=None)
        return {"args": args, "exit_code": process.returncode,
                "stdout": process.stdout, "stderr": process.stderr}
    registry = ToolRegistry()
    registry.register(Tool(ToolSpec("run_python",
        "Run a workspace Python script or -m unittest; args excludes the Python executable. "
        "Returns actual exit_code/stdout/stderr. cwd is /workspace.",
        {"type": "object", "properties": {"args": {"type": "array", "items": {"type": "string"}}},
         "required": ["args"], "additionalProperties": False}), run_python))
    return registry


def acceptance(folder):
    checks = []
    cases = [
        ("sample.csv", "category,amount\napple,2\nbanana,3\napple,-0.5\n", {"apple": 1.5, "banana": 3}),
        ("quoted.csv", 'category,amount\n"red,apple",1.25\n사과,2\n"red,apple",2.75\n', {"red,apple": 4, "사과": 2}),
        ("empty.csv", "category,amount\n", {}),
        ("invalid.csv", "category,amount\napple,nope\n", None),
    ]
    for name, content, expected in cases:
        (folder / name).write_text(content, encoding="utf-8")
        process = subprocess.run([sys.executable, "csv_totals.py", name], cwd=folder,
                                 capture_output=True, text=True, encoding="utf-8", timeout=15)
        try:
            output = json.loads(process.stdout)
        except ValueError:
            output = None
        passed = ((process.returncode != 0 and bool(process.stderr.strip())) if expected is None
                  else process.returncode == 0 and output == expected)
        checks.append({"case": name, "passed": passed, "expected": expected,
                       "exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})
    return checks


async def run(folder, model):
    from shs_agent.llm.provider.ollama import OllamaLLM
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "csv_totals.py").write_text("import json\nprint(json.dumps({}))\n", encoding="utf-8")
    before = acceptance(folder)
    workspace = Workspace(folder, delegable=True)
    commands = command_registry(workspace)
    commands.register(Tool(ToolSpec("verify_requirements",
        "Run independent Host CLI acceptance checks; returns passed, expected values and actual stdout/stderr.",
        {"type": "object", "properties": {}, "additionalProperties": False}),
        lambda: acceptance(folder)))
    empty = ToolRegistry()
    options = ChatOptions(temperature=0, max_tokens=4096,
                          extra={"think": True, "options": {"num_ctx": 32768}})
    session = await ProblemSolver(OllamaLLM()).start(
        TaskRequest(REQUIREMENTS, role="implementation coordinator",
                    result_requirements="Report actual test counts, commands, failures and repairs."),
        model=model, registry=commands, workspace=workspace,
        delegable_tool_ids=("run_python",), options=options,
        limits=AgentLimits(max_steps=40, max_tool_calls=100, max_total_subagents=4,
                           max_subagent_depth=1, context_window_tokens=32768),
        subagents={"worker": SubagentConfig(registry=empty, executor=ToolExecutor(empty),
                   limits=AgentLimits(max_steps=25, max_tool_calls=70, context_window_tokens=32768))})
    async for envelope in session.events(scope="tree"):
        event = envelope["event"]
        if event.type in {"step_started", "tool_started", "tool_finished", "finished", "subagent_created"}:
            print(json.dumps({"tree_sequence": envelope["tree_sequence"], "task": event.task_id,
                              "type": event.type, "step": event.step,
                              "name": event.payload.get("name"), "status": event.payload.get("status")},
                             ensure_ascii=False), flush=True)
        if event.type == "question":
            target = session if event.task_id == session.task_id else session.subagents.child(event.task_id)
            await target.answer(event.payload["question_id"],
                                "Use the interface and requirements already supplied; decide implementation details yourself.")
    result = await session.result()
    tree = await session.subagents.snapshot()
    after = acceptance(folder)
    test_result = commands.get("run_python").handler(["-m", "unittest", "discover", "-v"])
    accepted = (result.status == TaskStatus.COMPLETED and len(tree.traces) >= 3
                and all(t.state().task.status == TaskStatus.COMPLETED for t in tree.traces.values())
                and all(c["passed"] for c in after)
                and test_result["exit_code"] == 0 and "Ran 0 tests" not in test_result["stderr"])
    report = {"model": model, "workspace": workspace.snapshot(), "status": result.status.value,
              "output": result.output, "stop_reason": result.stop_reason,
              "warnings": result.warnings, "before": before, "after": after,
              "tests": test_result, "accepted": accepted, "task_count": len(tree.traces),
              "event_count": len(tree.event_order)}
    (folder / "host-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "tree.json").write_text(tree.to_json(), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return accepted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--model", default="qwen3.5:9b")
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(run(args.workdir.resolve(), args.model)) else 1)
