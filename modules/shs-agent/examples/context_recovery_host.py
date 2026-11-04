"""Controlled overflow signal + real Ollama summarization and workspace execution.

This probe does NOT claim that Ollama reported a context overflow. A Host wrapper
injects one normalized error; subsequent summary/retry calls use the real model.
"""
import argparse
import asyncio
import json
from pathlib import Path

from shs_agent import AgentLimits, ProblemSolver, Workspace
from shs_agent.llm import ChatOptions
from shs_agent.llm.errors import ContextOverflow


class ControlledOverflow:
    def __init__(self, provider):
        self.provider = provider
        self.injected = False

    def chat(self, **request):
        if request["tools"] and not self.injected and any(m["role"] == "tool" for m in request["messages"]):
            self.injected = True
            raise ContextOverflow("Controlled Host probe; not a server-reported overflow", limit_tokens=16384)
        return self.provider.chat(**request)


async def run(folder, model):
    from shs_agent.llm.provider.ollama import OllamaLLM
    folder.mkdir(parents=True, exist_ok=False)
    original = "\n".join(f"Observation {n}: sample data was validated; category apple has total 1.5." for n in range(1, 131))
    (folder / "notes.txt").write_text(original, encoding="utf-8")
    provider = ControlledOverflow(OllamaLLM())
    session = await ProblemSolver(provider).start(
        "Read notes.txt using file_read. Write a concise verified summary of those observations to result.txt "
        "using file_write, then read result.txt to verify it. Finish using task_complete alone. "
        "The files are inside the supplied Workspace. Do not rewrite notes.txt.",
        model=model, workspace=Workspace(folder),
        options=ChatOptions(temperature=0, max_tokens=2048,
                            extra={"think": True, "options": {"num_ctx": 16384}}),
        limits=AgentLimits(max_steps=8, context_window_tokens=16384))
    result = await session.result()
    trace = result.tracer
    accepted = (result.status.value == "completed" and result.context_compactions > 0
                and (folder / "result.txt").is_file()
                and (folder / "notes.txt").read_text(encoding="utf-8") == original)
    report = {"model": model, "signal": "controlled_host_injection", "real_summary_provider": "Ollama",
              "status": result.status.value, "stop_reason": result.stop_reason, "warnings": result.warnings,
              "compactions": result.context_compactions, "accepted": accepted, "output": result.output}
    (folder / "trace.json").write_text(trace.to_json(), encoding="utf-8")
    (folder / "host-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return accepted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--model", default="qwen3.5:9b")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.workdir.resolve(), args.model)) else 1)
