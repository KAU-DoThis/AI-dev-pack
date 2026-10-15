
import asyncio

from shs_agent.agent import AgentLimits, ProblemSolver, TaskRequest
from shs_agent.llm.provider.ollama import OllamaLLM
from shs_agent.tool.executor import ToolExecutor
from shs_agent.tool.model import Tool, ToolSpec
from shs_agent.tool.registry import ToolRegistry
from shs_agent.llm.types import ChatOptions


import ast
import math
import operator
from fractions import Fraction


MODEL = "qwen3.5:9b"
#MODEL = "gemma4:e4b"
MODEL = "gemma4:26b"

_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def calculate(expression: str) -> str:
    """Safely evaluate a mathematical expression."""

    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("expression must be a nonempty string")

    if len(expression) > 300:
        raise ValueError("expression is too long")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("invalid math expression") from exc

    if sum(1 for _ in ast.walk(tree)) > 80:
        raise ValueError("expression is too complex")

    def checked(value: Fraction) -> Fraction:
        if (
            value.numerator.bit_length() > 4096
            or value.denominator.bit_length() > 4096
        ):
            raise ValueError("number is too large")
        return value

    def evaluate(node: ast.AST) -> Fraction:

        # Number
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if type(node.value) is float and not math.isfinite(node.value):
                raise ValueError("nonfinite number")
            return checked(Fraction(str(node.value)))

        # Unary operations: -5, +5
        if isinstance(node, ast.UnaryOp):
            op = _UNARY_OPS.get(type(node.op))
            if op is None:
                raise ValueError("unsupported unary operator")
            return checked(op(evaluate(node.operand)))

        # Binary operations
        if isinstance(node, ast.BinOp):
            left = evaluate(node.left)
            right = evaluate(node.right)

            # Exponentiation: 2 ** 10
            if isinstance(node.op, ast.Pow):
                if right.denominator != 1 or abs(right.numerator) > 20:
                    raise ValueError("exponent must be an integer from -20 to 20")
                power = right.numerator
                if (
                    max(
                        left.numerator.bit_length(),
                        left.denominator.bit_length(),
                    ) * abs(power) > 4096
                ):
                    raise ValueError("power result is too large")
                return checked(left ** power)

            op = _BINARY_OPS.get(type(node.op))
            if op is None:
                raise ValueError("unsupported operator")

            if (
                isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod))
                and right == 0
            ):
                raise ZeroDivisionError("division by zero")

            return checked(Fraction(op(left, right)))

        raise ValueError(
            "only numeric arithmetic expressions are allowed"
        )

    result = evaluate(tree.body)

    # Return integers as integer strings and other values as exact fractions.
    if result.denominator == 1:
        answer = str(result.numerator)
    else:
        answer = f"{result.numerator}/{result.denominator}"

    print(f"[TOOL] calculate({expression!r}) = {answer}")

    return answer



def add(a: int, b: int) -> int:
    """Addition tool for testing."""
    print(f"[TOOL] add({a}, {b})")
    return a + b


def build_tools():
    registry = ToolRegistry()

    registry.register(
        Tool(
            spec=ToolSpec(
                name="calculate",
                description=(
                    "Evaluate a mathematical expression. "
                    "Supports +, -, *, /, //, %, ** and parentheses. "
                    "Convert natural-language numbers into a numeric "
                    "expression before calling this tool. "
                    "Use ** for exponentiation, not ^. "
                    "Do not use Python functions or variables."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": (
                                "Mathematical expression to evaluate. "
                                "Example: 42 - (10 * 10**8 + 8000 * 10**8)"
                            ),
                        },
                    },
                    "required": ["expression"],
                    "additionalProperties": False,
                },
            ),
            handler=calculate,
        )
    )

    executor = ToolExecutor(registry)

    return registry, executor


# ============================================================
# Host implementation: display questions and handle user input in the terminal.
# ============================================================

async def handle_host_events(session):
    async for event in session.events():

        # Handle a question from the agent to the host.
        if event.type == "question":
            request_id = event.payload["request_id"]
            question = event.payload["question"]
            context = event.payload.get("context", "")

            print("\n=== HOST ASK ===")
            print("Agent:", question)

            if context:
                print("Context:", context)

            # Run input() in a separate thread so it does not block the event loop.
            try:
                while True:
                    answer = (
                        await asyncio.to_thread(
                            input, "You: "
                        )
                    ).strip()

                    if answer:
                        break

                    print("답변을 입력해 주세요.")

            except EOFError:
                await session.cancel()
                return

            # Send the user's answer to the agent.
            await session.answer(
                request_id=request_id,
                content=answer,
            )

            print("[HOST] Answer sent to agent\n")


async def main():
    # 1. Ollama Provider
    provider = OllamaLLM(
        host="http://localhost:11434"
    )
    original_chat = provider.chat

    def debug_chat(*args, **kwargs):
        response = original_chat(*args, **kwargs)

        print(
            "[LLM]",
            "content =", repr(response.content),
            "thinking =", repr(response.thinking),
            "thinking_chars =", len(response.thinking or ""),
            "tools =", [c.name for c in response.tool_calls],
            "finish_reason =", response.finish_reason,
            "usage =", response.usage,
        )

        return response

    provider.chat = debug_chat

    print("=== Available Ollama Models ===")
    models = provider.get_available_models()

    for model in models:
        print("-", model)

    if MODEL not in models:
        raise RuntimeError(
            f"{MODEL!r} 모델을 찾을 수 없습니다.\n"
            f"설치된 모델: {models}"
        )

    # 2. Problem Solver
    solver = ProblemSolver(provider)

    # 3. Register regular tools.
    registry, executor = build_tools()

    # 4. Create a task.
    session = solver.create_task(
        request=TaskRequest(
            instruction=(
                "먼저 calculate 도구로 41 + 1을 계산해. "
                "그다음 반드시 host_ask 도구로 사용자에게 "
                "계산 결과에서 얼마를 뺄지 질문해. "
                "사용자가 한국어 숫자로 답하더라도 "
                "각 단위의 값을 수학 표현식으로 변환해. "
                "이후 calculate 도구로 뺄셈을 수행해. "
                "도구의 실제 계산 결과를 확인한 후 "
                "task_complete 도구를 호출하여 최종 결과를 알려줘."
            )
        ),
        model=MODEL,
        registry=registry,
        executor=executor,
        options=ChatOptions(
            extra={
                "think": True,
                "options": {
                    "num_ctx": 8192,
                    "num_predict": 2048,
                },
            },
        ),
        limits=AgentLimits(
            max_steps=10,
            max_tool_calls=10,
            host_response_timeout=None,
        ),
    )

    print("\n=== Task Start ===")

    # 5. Start handling host events.
    host_task = asyncio.create_task(
        handle_host_events(session)
    )

    # 6. Run the agent.
    await session.start()

    # 7. Wait for the agent to finish.
    result = await session.result()

    # 8. Wait for host event handling to finish.
    await host_task

    # 9. Print the result.
    print("\n=== Task Result ===")
    print("task_id     :", result.task_id)
    print("status      :", result.status)
    print("stop_reason :", result.stop_reason)
    print("steps       :", result.steps)
    print("tool_calls  :", result.tool_calls)
    print("tool_errors :", result.tool_errors)

    print("\n=== Output ===")
    print(result.output)

    print("\n=== Task Memory ===")
    if not result.memory:
        print("(empty)")
    else:
        for memory in result.memory:
            print(
                f"- [{memory.kind}] "
                f"{memory.content} "
                f"(step={memory.created_step})"
            )

    print("\n=== Transcript ===")
    print(f"{len(result.messages)} messages recorded")


if __name__ == "__main__":
    asyncio.run(main())
