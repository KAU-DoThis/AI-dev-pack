# shs-agent Agent 모듈 정식 구현 보고서

작성일: 2026-10-17 (Asia/Seoul). 이번 범위는 기존 Agent 기능의 책임별 재구현과 공개 API 정리다.
목표 문서 `docs/SHS_AGENT_TARGET_ARCHITECTURE.md`의 장기 확장 항목을 모두 구현한 것은 아니다.

## 1. 조사와 기존 작업 보존

작업 전 목표 아키텍처, 기존 `report.md`, Agent 전체와 LLM/Tool 계약, 패키징 및 Ollama 예제를 조사했다.
기존 Runtime은 실행 루프와 함께 질문 Future, 명령 처리, 메모리 기록, 대화 검색, 압축 및 직렬화까지
직접 소유했다. Solver는 생성자에서 Provider에 고정되었고 `create_task()`만 제공했다.
현재 사용하지 않는 `tools_depricated.py`와 별도 도구 구현도 중복되어 있었다.

- 작업 시작 시 미커밋 항목은 `docs/`와 `report.md`였다. 기존 목표 문서는 수정하지 않았다.
- 기존 보고서를 **바이트 그대로** `docs/SHS_AGENT_PROTOTYPE_REPORT.md`에 보존했다.
  이 문서의 줄 번호·경로는 조사 당시의 것이며 현재 구현 문서가 아니다.
- `src/shs_agent/agent/`의 **Python 파일 11개 전체**를 `agent/old/` 아래 원래 상대 경로로 복사했다.
  복사 직후 모든 파일의 SHA-256이 원본과 같은지 검사했다. 보관본 Python 코드는 수정하지 않았다.
- 새 실행 경로는 `old`를 import하지 않는다. 파일 재배치만 한 것이 아니라 Context와 Session의
  소유권, Runtime과의 인터페이스, 내부 도구 연결, 생성 API를 다시 구현했다.
- commit/push, staging은 수행하지 않았다.

## 2. 최종 구조와 파일별 책임

```text
src/shs_agent/
├── __init__.py                   공개 API export, 배포 메타데이터와 버전 일치
├── agent/
│   ├── __init__.py               Agent 공개 API export
│   ├── solver.py                 ProblemSolver; 입력 검사, 작업별 객체 조립, start/solve
│   ├── core.py                   AgentCore; Provider 판단 1회, 응답 검증·정규화
│   ├── types.py                  Request/Result/Status/Event/Memory/Limits, 주입 Protocol
│   ├── _async.py                 sync/async Provider·Executor 호출 연결
│   ├── runtime/
│   │   ├── __init__.py            TaskRuntime export
│   │   └── runner.py             루프, 실행 상태·카운터, 상태 전이, 명령 적용 경계, 한도
│   ├── session/
│   │   ├── __init__.py            TaskSession export
│   │   ├── handle.py             Host 핸들: start/result/events/instruction/answer/cancel
│   │   └── channel.py            명령함·멱등성·이벤트 로그·다중 구독·질문 ID/Future
│   ├── context/
│   │   ├── __init__.py            TaskContext export
│   │   ├── memory.py             transcript, 작업 메모리, Context view, 검색/읽기, 결과 snapshot
│   │   └── compaction.py         원자적 tool 그룹 경계, 압축 요청, 문자량 추정
│   ├── tools/
│   │   ├── __init__.py            SessionTools export
│   │   ├── schemas.py            내부 도구 5개의 공통 스키마
│   │   └── session_tools.py      내부 Registry/Executor 구성, 외부 도구 연결, 가용성 검사
│   └── old/
│       ├── README.md             보관 목적과 배포 제외 설명
│       ├── __init__.py
│       ├── solver.py
│       ├── core.py
│       ├── types.py
│       ├── runtime.py
│       ├── session.py
│       ├── context.py
│       ├── tools_depricated.py
│       └── tools/
│           ├── __init__.py
│           ├── schemas.py
│           └── session_tools.py
├── llm/                          기존 Provider·LLMResponse·ToolCall·옵션 재사용, 수정 없음
├── tool/                         기존 Tool/ToolSpec/Registry/Executor 재사용, 수정 없음
└── storage/                      기존 독립 Storage 모듈 유지, 수정·자동 연결 없음

tests/
├── __init__.py                   테스트 패키지
├── fakes.py                      scripted Provider, 제어 가능한 대기, Fake Tool, 공통 검증
├── test_runtime.py               반복 실행·명시적 완료·오류·지시 무효화·취소·한도·작업 분리
├── test_session.py               질문·답변·명령 멱등성·다중 이벤트 구독·재조회·대기 취소
├── test_context.py               메모리·원본 조회·페이지 읽기·압축 경계와 통합 실행
├── test_contracts.py             Provider 검증·입력 격리·동기 호출·경량 import·설정 검사
└── verify_distribution.py        wheel/sdist 검사 및 site-packages 없는 wheel 회귀 테스트

MANIFEST.in                       old를 sdist에서 제외, 테스트 보조 파일 포함
pyproject.toml                    old 패키지 제외, package-data 비활성화, pytest 수집 경로
README.md                         UTF-8 사용 안내, 공개 API, 설치·검증 명령
test.py                           기존 Ollama 예제를 새 start API로 갱신
docs/SHS_AGENT_PROTOTYPE_REPORT.md 기존 미커밋 보고서 원본
report.md                         현재 구현·검증 보고서
```

### 책임 경계

`ProblemSolver`만 각 구성요소를 조립한다. 작업마다 Core, Context, Channel, 도구 바인딩, Runtime,
Session을 새로 만든다. Solver에는 선택적인 기본 Provider 참조 외에 실행 상태가 없다.

`AgentCore.step()`은 LLM 호출 한 번만 수행한다. 입력을 복사하고 content/tool call ID·이름·인자 JSON을
검증하여 기존 `LLMResponse`로 정규화한다. 반복·질문 대기·실제 도구 실행은 하지 않는다.

`TaskRuntime`은 `ExecutionState`에 step/tool/error 카운터, 지시 revision, 취소 및 적용 전 지시만 가진다.
Context의 리스트나 Channel의 큐·Future를 직접 조작하지 않는다. Context가 요청한 요약 호출을
Core에 전달하고 결과를 돌려주므로, 압축의 저장 방식과 실행 제어가 분리된다.

`TaskChannel`은 Session 내부 통신 구현이다. Runtime은 `take_commands`, `wait_commands`,
`acknowledge`, `interrupt_question`, `emit`, `set_status`, `close`를 이용한다.
`TaskSession`은 실행 함수를 주입받고 구체 Runtime을 import하지 않는다. 순환 import가 없다.

`TaskContext`는 원본 transcript·노트·요약을 소유한다. 실행 카운터, 취소, 질문 Future는 소유하지 않는다.
`SessionTools`는 기존 `ToolRegistry`와 `ToolExecutor`로 내부 도구도 등록·호출하며, 외부 도구는
Host가 준 Executor에 그대로 위임한다. Host Registry를 수정하거나 별도 외부 실행기를 만들지 않았다.

## 3. 이전 구현과의 차이 및 호환성

| 항목 | 새 동작 / 호환성 |
|---|---|
| Solver 생성 | `ProblemSolver()` 가능. `ProblemSolver(provider)`도 기본 Provider 지정으로 유지 |
| 작업별 설정 | `start/solve/create_task`에서 Provider, model, registry, executor, options, limits, model_selector 지정 |
| 실행 API | `start(task=...)`는 실행을 예약한 Session 반환, `solve(task=...)`는 최종 TaskResult 반환 |
| 하위 API | 기존 `create_task(request=TaskRequest(...), ...)` + `await session.start()` 유지 |
| 기본 도구 | registry/executor 생략 시 작업별 빈 Registry/Executor 생성. 내부 도구 5개는 항상 제공 |
| 잘못된 도구 연결 | 구체 ToolExecutor의 Registry 불일치는 생성 단계에서 거부. custom Registry에는 명시적 Executor 필요 |
| 답변 API | `answer(question_id, text)` 권장. 기존 위치 인자와 `request_id=..., content=...` 키워드도 유지 |
| 질문 이벤트 | 기존 request_id를 유지하고 같은 값의 question_id 추가. question_closed에 종료 reason 추가 |
| 질문 중 추가 지시 | 기존 질문을 `superseded_by_host_instruction`으로 닫고 다음 판단으로 진행. 무기한 질문에 묶이던 동작 개선 |
| Context 보존 | 원본 도구 결과를 JSON으로 기록한 후 LLM view/이벤트 사본만 제한. 검색·읽기로 잘린 뒷부분 조회 가능 |
| 압축과 권한 | 요약·생성 노트는 assistant 역할의 작업 데이터로 전달. system prompt에 합치지 않음 |
| 명시적 Host 입력 | 최초 task/context와 추가 지시를 압축 뒤에도 원문 보존. 요약 때문에 제약이 사라지지 않음 |
| Provider 응답 기록 | 기존 LLMResponse.to_message()를 이용해 thinking도 보존. usage는 아직 TaskResult 집계 API 없음 |
| 종료 정보 | finished 이벤트에 tool_errors와 warnings 추가. 기존 이벤트 이름·sequence·주요 필드는 유지 |
| 취소 시 미적용 지시 | 아직 Context에 반영하지 않은 지시는 취소 시 command_rejected로 확인 가능 |
| 결과 대기자 취소 | session.result() 대기자 취소/timeout은 실행을 취소하지 않음. solve() 자체 취소는 세션에 협력적 취소 요청 |
| Runtime 자체 취소 | 종료 정리 후 CANCELLED/runtime_cancelled 결과로 반환. 기존 구현의 CancelledError 재전파와 다름 |
| 내부 API | `ProblemSolver.core`, `TaskSession._runtime`, `types.WorkingMemory`, 기존 context helper는 호환 대상으로 유지하지 않음 |
| Session 직접 생성 | Solver를 통해 생성. 내부 생성자는 Channel과 실행 함수를 요구 |
| 버전 | 배포 설정 0.1.0에 `__version__`을 맞춤. 기존 소스의 0.3.1과 불일치하던 상태 정리, 릴리스 수행 아님 |

일반 텍스트만으로 완료하지 않는 정책, `task_complete`가 다른 도구와 섞인 배치 전체 거부,
단계별 `model_selector(step_number)` 호출, 순차 도구 실행, Registry version 변경 검사는 유지했다.
예약 이름 충돌은 시작 전 오류다. 일반 도구 예외는 실패 관찰로 LLM에 전달되어 후속 판단으로 복구할 수 있다.
외부 도구 인자의 완전한 JSON Schema 검증과 권한 판정은 Host/Executor 책임이다.

## 4. 실제 호출 흐름

1. Host가 `await solver.start(task=..., provider=..., model=...)` 호출.
2. Solver가 문자열을 TaskRequest로 변환하고 설정을 검사한다. 작업별 Channel, Context, Core,
   SessionTools, Runtime을 구성하고 Session에 `runtime.run` 실행 함수를 연결한다.
3. `session.start()`가 asyncio Task를 예약한다. created 이벤트는 이미 기록되어 있으며,
   Runtime 시작 시 RUNNING 상태 이벤트를 발행한다.
4. Runtime이 명령을 수신하고 취소/실행 한도를 확인한다. 추가 지시를 안전한 메시지 경계에서
   Context에 넣고 적용 이벤트를 발행한다. 동기 model_selector는 1부터 시작하는 판단 step 번호를 받는다.
5. Context가 필요하면 CompactionPlan을 만든다. Runtime → Core → Provider로 별도 요약 호출 후
   Context에 결과를 반영한다. 요약 호출은 판단 step 수에 포함하지 않으며 현재 선택 모델/옵션을 사용한다.
6. 도구 스키마/version과 지시 revision을 확보하고 Context view로 Core.step()을 호출한다.
   동기 Provider는 worker thread, async Provider는 이벤트 루프에서 수행한다.
7. 요청 중 Host 지시가 도착하면 revision이 증가한다. 이전 revision 응답은 response_discarded로 알리고
   transcript·도구 실행에 반영하지 않는다. 소비한 step은 되돌리지 않는다.
8. 정상 응답은 Context에 기록한다. 일반 텍스트는 message 이벤트 후 다음 판단으로 진행한다.
   tool_calls는 순서대로 처리하고, 호출 직전 취소·revision·예산·등록 스냅샷을 검사한다.
9. 내부 도구는 작업 전용 Registry/Executor → Channel 또는 Context 메서드로 연결한다.
   외부 도구는 Host Executor로 위임한다. host_ask는 WAITING_FOR_HOST 상태와 질문 이벤트를 만들고
   ID가 맞는 답변을 기다린다. 잘못되거나 닫힌 질문의 답변은 command_rejected가 된다.
10. 모든 tool call에 성공/실패/미실행 결과를 기록한다. 새 지시나 취소가 와도 이미 실행한 외부 도구는
    종료를 기다려 결과를 남기고, 나머지 도구는 not_executed 결과를 붙인다. 사용자 메시지를 tool batch
    중간에 끼워 넣지 않는다. 외부 부작용의 rollback은 수행하지 않는다.
11. 단독 task_complete가 유효한 output을 반환하고 지시 revision·취소 조건을 통과하면 COMPLETED.
    그 외에는 루프를 계속하거나 실패/취소/한도/미완료로 종료한다.
12. Runtime이 Context snapshot으로 TaskResult를 만든다. Channel에 최종 상태와 finished를 발행하고
    닫는다. 구독자는 남은 이벤트를 받은 뒤 종료하며 `session.result()`는 독립 복사본을 반환한다.

### 상태와 실행 예산

| 종료 상태 | 대표 stop_reason |
|---|---|
| COMPLETED | task_complete |
| FAILED | empty_response, runtime_error (Provider/검증/압축 실패 등의 warnings 포함) |
| CANCELLED | host_cancelled, runtime_cancelled |
| LIMIT_REACHED | max_steps, max_tool_calls |
| INCOMPLETE | length, max_tokens, content_filter, host_response_timeout |

기본값: 판단 20회, 실제 도구 실행 50회(내부 완료 도구 포함), Host 응답 300초.
거부된 미실행 도구는 tool_calls를 소비하지 않으며 실패 관찰은 tool_errors에 포함한다.
빈 응답, 불완전 응답, 요약 실패를 성공으로 처리하지 않는다.

## 5. 공개 API 사용 예시

### 외부 LLM을 사용하지 않는 완전한 최소 예제

```python
import asyncio
from shs_agent import ProblemSolver
from shs_agent.llm import LLMResponse, ToolCall

class FakeProvider:
    async def chat(self, model, messages, tools=None, options=None):
        return LLMResponse(None, [ToolCall("task_complete", {"output": "완료"})])

async def main():
    result = await ProblemSolver().solve(
        task="간단한 작업", provider=FakeProvider(), model="fake",
    )
    print(result.status.value, result.output)

asyncio.run(main())
```

### Host 도구와 상호작용형 작업

```python
from shs_agent import AgentLimits, ProblemSolver, TaskRequest
from shs_agent.llm import ChatOptions
from shs_agent.llm.provider.ollama import OllamaLLM
from shs_agent.tool import Tool, ToolSpec, ToolRegistry, ToolExecutor

async def run_host(answer_question):
    provider = OllamaLLM(host="http://localhost:11434")  # 원격 URL도 사용 가능
    registry = ToolRegistry()
    registry.register(Tool(
        ToolSpec("add", "Add two numbers", {
            "type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"], "additionalProperties": False,
        }),
        lambda a, b: a + b,
    ))
    solver = ProblemSolver()
    session = await solver.start(
        task=TaskRequest("41 + 1을 도구로 계산하고 완료해줘.", context=("필요하면 Host에 질문한다.",)),
        provider=provider, model="qwen3.5:9b",
        registry=registry, executor=ToolExecutor(registry),
        options=ChatOptions(temperature=0.2), limits=AgentLimits(max_steps=10),
    )
    async for event in session.events():
        if event.type == "question":
            text = await answer_question(event.payload)  # Host가 제공한 async UI 함수
            await session.answer(question_id=event.payload["question_id"], text=text)
        else:
            print(event.sequence, event.type, event.payload)
    return await session.result()
```

실제 UI는 이벤트 소비와 사용자 입력 처리를 분리하고 `question_closed`를 반영해야 한다.
예제처럼 답변 함수 안에서 오래 기다리면 그 소비자의 이벤트 표시도 지연된다.
Provider 생성은 SDK Client만 구성한다. 모델 조회/추론은 생성 시 수행하지 않고, GPU·로컬 모델도 필요 없다.
Ollama/OpenAI SDK는 각각 선택 설치이며 기존 OpenAILLM(base_url=..., api_key=...)도 같은 API에 전달할 수 있다.

동일 Session을 가진 Host가 별도 UI 동작에서 사용할 제어 API:

```python
await session.send_instruction("요구사항을 이 조건으로 바꿔줘.", message_id="instruction-1")
await session.answer(question_id=question_id, text="두 번째 방법을 사용해줘.")
await session.cancel()
result = await session.result()

# 각 구독자는 독립 cursor를 소유. 마지막 처리 sequence 이후만 다시 읽기.
async for event in session.events(after_sequence=last_sequence):
    process_event(event)
```

명령 반환 문자열은 접수 ID다. 적용/거부는 이벤트로 확인한다. 동일 ID·동일 내용 재전송은 멱등이고,
같은 ID로 내용을 바꾸면 ValueError다. 종료 뒤 새 명령은 RuntimeError이고 기존 동일 명령 재전송만 허용한다.
모든 Session 메서드는 같은 asyncio 이벤트 루프에서 사용해야 한다.

단순 실행과 기존 하위 API:

```python
result = await solver.solve(task="문제를 해결해줘.", provider=provider, model="model-a",
                            registry=registry, executor=executor)

session = solver.create_task(request=TaskRequest("작업"), provider=provider, model="model-a",
                             model_selector=lambda step: "model-a" if step == 1 else "model-b")
await session.start()
result = await session.result()
```

`model_selector`는 빠른 동기 함수이며 Provider 교체 기능이 아니다. `solve()`도 모델이 host_ask를 호출하면
답변을 기다리므로, 상호작용이 필요한 Host는 `start()`를 사용한다. 응답 timeout=None은 무기한 대기를 뜻한다.

## 6. 기능 유지와 검증 결과

### 자동 테스트

검증 환경: Windows, 프로젝트 `.venv` Python 3.12.9. 테스트는 unittest와 Fake Provider/Tool을 사용한다.
pytest·pytest-asyncio 설치 없이 실행 가능하며 기존 dev extras는 유지했다.

```text
.venv/Scripts/python.exe -m unittest discover -s tests -v
Ran 46 tests
OK
```

| 요구 기능 | 검증 근거 |
|---|---|
| Task 생성·정상 완료 | 생성 전 Provider 호출 없음, start 멱등, 일반 텍스트 후 명시적 완료, solve·기본/작업별 Provider |
| LLM↔Tool 반복 | 한 응답의 복수 sync/async 도구 결과가 다음 Provider 입력에 포함, Host Executor 재사용 |
| 도구 오류 | 실행 예외, 미등록 도구, 순환 결과 직렬화 오류, 잘못된 완료 배치 후 회복 |
| Host 질문·답변 | 질문 ID 매칭, 잘못된/이른 답변 거부, 구 API 키워드, timeout·질문 닫기 |
| 추가 지시 | LLM 대기 중 응답 폐기, 도구 실행 중 결과 보존·남은 도구 미실행, 질문/요약 중 지시 반영 |
| 취소·한도 | 시작 전·Provider 대기·도구 실행·질문 대기 취소, step/tool 한도, 완료 도구 예산 |
| 이벤트 구독 | 동시 구독자 동일 로그, 느린 소비자 독립, sequence 재조회, payload/result 수정 격리 |
| Task별 분리 | 동시 Task의 ID·Provider·모델·Context·메모리·도구와 취소가 독립 |
| Working Memory | memory_write 다음 문맥 재사용·결과 반환·항목/총량 제한 |
| Context 관리 | tool batch 원자 경계, 원본 보존, summary 검증·실패, 큰 결과의 뒷부분 검색·분할 읽기 |
| 모델·Provider 계약 | model_selector, options 복사, sync Provider thread 분리, 반환 awaitable, 잘못된 응답 검증 |
| 경량 import | 별도 `python -I -S`에서 SDK/SQLAlchemy/GPU 없이 import·Solver 생성, old 미로드 |
| Provider 생성 | SDK mock으로 생성자가 모델 목록/추론 호출을 하지 않는지 확인 |

시간 경합은 주로 asyncio.Event와 명시적 gate로 재현했다. 임의의 sleep으로 완료 시점을 추정하지 않는다.
Host timeout 테스트만 짧은 실제 timeout을 사용한다. Windows sandbox에서는 asyncio 내부 socketpair 생성이
차단되어 최소 `asyncio.run(asyncio.sleep(0))`도 멈췄다. traceback으로 확인 후 자동 승인된 sandbox 외부
실행에서 테스트했다. 이는 외부 LLM 의존성 문제가 아니다.

### 패키징

```text
uv build
.venv/Scripts/python.exe tests/verify_distribution.py
```

- setuptools package discovery에서 `shs_agent.agent.old`, 하위 패키지를 제외했다.
- `include-package-data = false`와 `MANIFEST.in`의 prune으로 보관 코드를 wheel/sdist 모두에서 제외한다.
- sdist에는 테스트 helper도 포함하므로 소스 배포본에서 테스트를 재실행할 수 있다.
- 검증 스크립트는 아카이브에서 old/폐기 모듈이 없는지, wheel 코드가 현재 소스와 같은지 검사한다.
  그 뒤 `-I -S`로 **wheel을 직접 import해 동일 회귀 테스트 전체를 실행**하고 old/외부 SDK 미로드를 확인한다.
- 실제 결과: wheel/sdist 검사 통과, 현재 소스와 일치, 격리된 wheel 테스트 **46개 모두 통과**.
  생성물은 `dist/shs_agent-0.1.0-py3-none-any.whl`, `dist/shs_agent-0.1.0.tar.gz`다.
- 기본 의존성은 기존 SQLAlchemy를 유지했다. 새 런타임 의존성, PyTorch/GPU/추론 엔진은 추가하지 않았다.

### 실제 서버 검증과 예제

`http://127.0.0.1:11434/api/tags`를 3초 timeout으로 조회했으나 연결 거부(WinError 10061)였다.
따라서 **실제 Ollama 추론은 실행하지 않았다**. 모델 다운로드/서버 기동도 하지 않았다.
OpenAI/vLLM 실제 서버 호출과 Storage 통합 테스트도 이번 결과에 포함하지 않는다.

기존 `test.py`는 `ProblemSolver()` + `await solver.start(task=..., provider=...)`로 수정했다.
OLLAMA_HOST와 OLLAMA_MODEL로 주소/모델을 지정하며, 기본 모델은 qwen3.5:9b다.
기존 계산 도구·Host 입력·결과 표시를 유지했고 모델 목록 조회도 thread로 분리했다.
실제 서버가 준비되면 `python test.py`로 수동 종단간 확인할 수 있다.

## 7. 구현하지 않은 기능과 남은 경계

- Workspace/Mount/파일 권한, Storage 자동 기록·복구, Pause/Resume, Planning, Sub-agent,
  실행 중 Provider 교체, MindMap/Life Agent는 구현하지 않았다.
- Context는 현재 Task의 작업 기억이다. 장기 기억 선택/저장과 다음 Task로 넘길 내용은 Host가 결정한다.
- 외부 호출 취소는 협력적이다. 진행 중 sync/async Provider·외부 도구는 완료를 기다리며,
  스레드/프로세스 강제 종료·rollback은 제공하지 않는다. Host는 자체 클라이언트/도구 timeout을 정해야 한다.
- 공개 cancel과 달리 이벤트 루프 자체를 종료하면 asyncio 정리 규칙을 따른다. 실행 중 worker thread의
  외부 부작용을 엔진이 되돌린다는 보장은 없다.
- Context 압축은 문자량 기반 휴리스틱이다. 기본 threshold 48,000, 최근 최소 12개, 요약 최대 12,000자다.
  최초 입력·지시·도구 스키마가 크거나 접을 그룹이 없으면 threshold를 넘을 수 있다.
  모델별 토큰 창 보장·요약 실패 fallback·자동 Provider retry는 후속 정책이다.
- tool view 최대 12,000자와 달리 원본 transcript·이벤트·멱등성 기록은 세션 수명 동안 메모리에 누적된다.
  메모리 content는 항목 4,000/합계 24,000자 제한이며 kind/source_refs 전체 길이 제한은 아니다.
  장시간 작업의 보존 정책과 저장 연동은 별도 설계가 필요하다.
- 도구는 순차 실행한다. 외부 Registry/Executor/Provider 객체를 여러 Task에서 공유할 때 thread safety와
  Host 자원 분리는 Host 책임이다. 등록 객체의 직접 변경·외부 스레드 변경을 원자적으로 잠그는 시스템은 아니다.
- 이벤트는 토큰 streaming이 아니며 디스크 replay/backpressure를 제공하지 않는다. 결과의 사실성 검증,
  모델별 tool calling 품질, token usage/비용 집계도 이번 보장에 포함하지 않는다.

향후 기능은 Solver의 구성 단계, Runtime 실행 정책, Channel 통신, Context 저장/view 중 해당 경계에서
확장할 수 있다. 현재 코드에 빈 확장 모듈이나 선행 구현용 계층은 추가하지 않았다.


---

## 8. 1단계 구현 추가 보고: Conversation/Memo와 ContextBuilder

작성일: 2026-10-17 (Asia/Seoul). 위 1~7절은 이전 구현 당시의 보고서로 그대로 보존했다.
이번 절이 Conversation/Memo/ContextBuilder에 대한 현재 구현을 설명한다. 목표 문서의 실제 경로는
`docs/architecture/SHS_AGENT_TARGET_ARCHITECTURE.md`이며, §8·§16의 1단계·§17.2를 기준으로 작업했다.
현재 소스와 호출 경로를 먼저 확인하고, **수정 전 기존 unittest 46개 통과**를 확인한 뒤 변경했다.
기존 책임별 엔진을 재사용했으며 프로토타입 이전이나 전체 엔진 재구현은 하지 않았다.

### 8.1 변경 파일과 책임

아래 Agent 파일 경로는 `src/shs_agent/agent/` 기준이다.

| 파일 | 이번 변경과 책임 |
|---|---|
| `context/conversation.py` (추가) | `Conversation`: 최초 system/Task/Host Context, 추가 지시, LLM 응답·thinking·tool call/result 원본과 인덱스 소유. 검색·부분 조회·분리된 스냅샷 제공. 도구 결과의 길이 제한 사본 공통 함수 |
| `context/memo.py` (추가) | `Memo`: 명시적 작업 메모 작성·한도 검사·출처 보존·불변 TaskMemo tuple 조회. Conversation에서 자동 추출하지 않음 |
| `context/state.py` (추가) | `TaskContext`: Task별 Conversation/Memo/Compaction 연결, `ContextSnapshot`과 결과 필드 제공. LLM 메시지 조합 없음 |
| `context/builder.py` (추가) | `ContextBuilder.build(state)`: 읽기용 상태로 일반 LLM 입력을 조합하는 유일한 구현. 원본 수정·LLM/도구/DB 호출·실행 루프 없음 |
| `context/compaction.py` | 기존 문자량 추정·원자적 도구 묶음 경계·요약 요청 형식 유지. `Summary`와 `Compaction`에 요약 상태·대상 선택·응답 검증·결과 반영 책임 배치 |
| `context/__init__.py` | `TaskContext`, `ContextSnapshot`, `ContextBuilder` export |
| `context/memory.py` (제거) | 한 파일에 섞여 있던 책임을 위 모듈로 분리. 호환 wrapper나 중복 입력 조합 코드를 남기지 않음 |
| `solver.py` | Task별 ContextBuilder를 만들어 기존 TaskRuntime에 주입 |
| `runtime/runner.py` | 일반 판단을 Builder → Core 경로로 연결. 기록은 Conversation, 요약은 Compaction 인터페이스 사용. 기존 실행·명령·질문·취소 정책 유지 |
| `types.py` | TaskMemo, TaskResult.memos, Memo 한도 설정, 기본 prompt의 도구명 갱신 |
| `tools/schemas.py`, `tools/session_tools.py` | memo_write 한 개 등록, Memo/Conversation 소유자에게 위임, 새 Memo 이벤트·결과 키 사용 |
| `session/channel.py` | finished 이벤트의 작업 메모 개수 키를 memo_items로 변경 |
| `__init__.py`, `src/shs_agent/__init__.py` | 공개 TaskMemo export로 갱신 |
| `tests/test_context.py`, `test_runtime.py`, `test_session.py`, `test_contracts.py` | 기존 검증 의도를 유지하며 이름·분리된 Context API 갱신 |
| `tests/test_context_builder.py` (추가) | Builder 역할·순서·출처·반복 호출·수정 격리·연속 요약·실제 Runtime 연결·Memo 반환·큰 결과 재조회 검증 7개 |
| `tests/verify_distribution.py` | 기존 검사 유지, 제거한 context/memory.py의 배포 제외와 sdist 소스 바이트 일치 검사 추가 |
| `README.md`, `test.py` | 용어·공개 API·호환성 안내와 결과 표시 예제 갱신, README 목표 문서 링크 수정 |
| `report.md` | 기존 본문을 보존하고 이번 절 추가 |

`core.py`, `llm/`, `tool/`, `storage/`, Session의 질문/답변 구현, 패키지 설정의 기존 작업을 재사용했다.
이번 작업에서 `agent/old/` 및 `docs/`의 목표·과거 문서는 수정하지 않았다. 실행 경로에서 old를 import하지 않는다.
새 런타임 의존성, 빈 미래 확장 클래스·모듈, commit/push/staging을 추가하지 않았다.

### 8.2 실제 Context 생성·요약 호출 흐름

1. `ProblemSolver.create_task()`가 기존과 같이 Task별 Channel/Core/Context/SessionTools/Runtime을 만들고,
   같은 AgentLimits로 `ContextBuilder`를 생성해 Runtime에 주입한다. `start()`와 `solve()`도 이 조립 경로를 쓴다.
2. Runtime은 안전한 경계에서 추가 지시를 `context.conversation.add_instruction()`으로 기록한다.
3. 압축 판단 시 `context.snapshot()` → `builder.build(snapshot)`으로 현재 입력을 구성한다.
   메시지와 도구 스키마의 문자량을 `Compaction.plan(snapshot.conversation, context_chars=...)`에 전달한다.
4. 요약이 필요하면 **Runtime → AgentCore.step(model, plan.messages, tools=[]) → Provider**로 호출한다.
   `Compaction.apply(plan, response)`가 텍스트 검증·길이 제한·Summary 경계·횟수를 갱신한다.
   Runtime은 Builder로 반영 후 문자량을 계산해 기존 context_compacted 이벤트를 발행한다.
5. 일반 판단은 **Runtime._loop → TaskContext.snapshot → ContextBuilder.build → AgentCore.step → Provider.chat**다.
   Builder와 별개인 TaskContext/Runtime 내부의 기존 messages() 조합 로직은 제거했다.
6. 정상 LLM 응답과 도구 결과는 Conversation에 원본으로 기록한다. `memo_write`는 기존 Registry/Executor 경유로
   `Memo.write()`에 기록하고 `task_memo_written` 이벤트를 발행한다. 다음 build에서 즉시 포함된다.
7. 종료 시 `TaskContext.result_fields()`가 원본 messages, memos, context_summary, context_compactions를 반환하고,
   Runtime이 기존 TaskResult/Session 결과 반환 경로를 사용한다.

Builder 순서는 **system → 최초 Task → Host Context(각 user) → Summary(있으면 assistant) → Memo(있으면 assistant)
→ 요약 범위에 들어간 추가 지시(user, 기록 순서) → 최근 Conversation**이다. 최근 범위의 추가 지시는 원래 위치에
한 번만 포함한다. 최초 지시·추가 지시의 공백과 줄바꿈도 보존하고, Host Context의 기존 `Host context:` 표식을 유지한다.
Summary의 원본 인덱스 경계 및 Memo의 memo_id/task_id/kind/source_refs/created_step을 유지한다.
Summary/Memo를 system 지시에 합치지 않는다.

Snapshot은 frozen 데이터 컨테이너이며 Conversation의 중첩 메시지 사전은 deep copy로 분리된다.
Builder는 이를 읽기만 하고 반환 메시지도 별도로 복사한다. Memo는 불변 TaskMemo와 tuple로 제공한다.
반환 메시지의 중첩 도구 인자·내용·목록, 외부 입력과 스냅샷 사전을 수정해도 원본은 바뀌지 않으며,
반복 build는 Conversation이나 Memo에 기록을 추가하지 않는다.

### 8.3 명칭 변경과 호환성

| 이전 이름 | 현재 이름 / 계약 |
|---|---|
| `TaskMemory` | `TaskMemo` (`shs_agent` 및 `shs_agent.agent`에서 공개 export) |
| `TaskMemory.memory_id` | `TaskMemo.memo_id` |
| `TaskResult.memory` | `TaskResult.memos: tuple[TaskMemo, ...]` |
| `memory_write` / `MEMORY_WRITE_SCHEMA` | `memo_write` / `MEMO_WRITE_SCHEMA` |
| 작업 메모 작성 도구 결과의 `memory_id` | `memo_id`; available_in_context는 유지 |
| `task_memory_written` | `task_memo_written`; 이벤트 payload도 memo_id 사용 |
| finished payload의 `memory_items` | `memo_items` |
| `AgentLimits.max_memory_item_chars` | `max_memo_item_chars`, 기본값 4,000자 유지 |
| `AgentLimits.max_task_memory_chars` | `max_task_memo_chars`, 기본값 24,000자 유지 |
| 내부 `TaskContext.write_memory()` | `TaskContext.memo.write()` |
| 내부 `TaskContext.messages()` | `ContextBuilder.build(TaskContext.snapshot())` |
| 내부 Context 기록·검색/읽기·압축 메서드 | 각각 context.conversation 및 context.compaction 메서드로 이동 |

현재 저장소의 실제 사용처는 Agent 실행 코드·공개 export·테스트·`test.py`였으며 모두 갱신했다.
보관본과 목표/과거 문서 외에 구 작업 메모 API를 계속 요구하는 사용처는 발견하지 못했다.
따라서 **구 이름의 호환 별칭·이중 이벤트·이중 결과 필드를 제공하지 않는 변경**이다. 외부 소비자는 위 표대로 갱신해야 한다.
LLM의 내부 도구 목록에는 `memo_write`만 포함하며 같은 기능을 `memory_write`로 중복 등록하지 않는다.
Host가 소유하는 장기 Memory의 용어·별도 계약은 변경하지 않았다.

`TaskResult.messages`, `context_summary`, `context_compactions`는 유지했다.
`conversation_search/read`의 message_index, offset, 검색 순위·제한·현재 호출 제외 및 응답 키
(`transcript_messages`, `original_transcript_preserved` 포함)는 호환성을 위해 유지했다.
이는 작업 메모의 Memory 명칭과 다른 기존 원본 기록 조회 계약이다.
`start/solve/create_task`, 질문 답변의 기존 위치/키워드 API와 기타 실행 이벤트도 유지했다.

### 8.4 유지한 동작과 검증 결과

검증 환경: Windows, 프로젝트 `.venv` Python 3.12.9. 실제 서버 없이 unittest와 기존 Fake Provider/Tool 사용.

| 검증 | 결과 |
|---|---|
| 수정 전 `.venv/Scripts/python.exe -m unittest discover -s tests -v` | 46개 통과 |
| 수정 후 동일 전체 unittest | 기존 46개 + 신규 7개 = 53개 통과 |
| `uv build` | wheel/sdist 생성 성공 |
| `.venv/Scripts/python.exe tests/verify_distribution.py` | 아카이브 검사 통과, `-I -S` 격리 wheel 테스트 53개 통과 |
| 경량 import | site-packages 없는 실행에서 Solver 생성과 테스트, torch/ollama/openai/sqlalchemy 및 old 미로드 |
| 배포 검사 | old·폐기 모듈·context/memory.py 제외, 새 모듈 포함, wheel/sdist 소스 바이트 일치 |

초기 sandbox 실행은 첫 async 테스트의 이벤트 루프 생성 중 멈췄다. 8초 faulthandler 진단으로
`socket._fallback_socketpair → accept` 대기를 확인했고, 해당 실행은 종료했다. 승인을 받은 sandbox 외부 실행에서
수정 전·후 전체 테스트를 완료했다. uv build도 sandbox 캐시 쓰기 거부 후 승인된 외부 실행에서 성공했다.
새 테스트의 첫 실행에서는 공통 ToolCall 표현에 없는 function 키를 참조한 테스트 오류 1건을 수정했고,
현재 공통 표현인 tool_calls[].arguments의 중첩 수정 격리를 검증해 최종 53개 모두 통과했다.

- 원본 Conversation과 LLM/이벤트용 길이 제한 사본을 구분한다. 기존 특수문자·긴 결과의 전체 페이지 복원 테스트를 유지했다.
  신규 통합 테스트는 큰 원본의 뒷부분을 검색한 인덱스로 읽어 다음 Provider 입력에 전달하는 것까지 확인한다.
- 검색은 현재 Task Conversation만 대상으로 한다. 별도로 작성한 Memo나 다른 Task 기록을 암묵적으로 검색하지 않는다.
- 입력 구성 역할·순서, 중첩 반환값 수정 격리, 반복 build 무중복, 두 번의 연속 요약 전후 지시 원문과 도구 묶음을 검증한다.
- Memo 작성 직후와 압축 뒤 일반 판단, 이벤트, 도구 응답, 최종 TaskResult에 같은 Memo와 출처가 반영되는 것을 확인한다.
- Runtime이 Builder의 반환 메시지를 실제 Provider에 전달하며 Plan/Sub-agent 없이 기본 Task가 완료되는 것을 확인한다.
- 기존 LLM↔Tool 반복, 단독 task_complete, 질문·답변·추가 지시·취소, 한도·오류·미완료, Task별 독립 상태를 유지한다.
- 압축의 문자량 기준·최근 메시지 수·요약 길이, 실행 한도, 질문 timeout·취소 정책은 바꾸지 않았다.
  요약 호출은 기존처럼 판단 step에 포함하지 않고 현재 모델/옵션으로 Runtime에서 수행한다.

**이번 1단계에서는 실제 LLM 서버 호출·모델 목록 조회·추론을 수행하지 않았다.**
실제 Ollama/OpenAI/vLLM·DB 종단간 검증 결과로 위 자동 테스트를 해석하면 안 된다.
이전 보고서의 서버 연결 시도 결과는 이전 작업의 기록으로만 유지한다.

### 8.5 이번에 구현하지 않은 항목

Planning/Replanning, Sub-agent, Agent 간 통신, ExecutionTrace, Storage 자동 연결, Host Memory 구현,
Memo 수정·삭제·검색 고도화, 새 검색 알고리즘·토큰 예산 정책은 구현하지 않았다.
이들을 위한 빈 클래스·임시 모듈도 추가하지 않았다. 기존 SQLAlchemy 의존성과 선택적 Provider 의존성은 유지했다.
1단계에서 작업을 마쳤으며 다음 단계로 진행하지 않았다. 기존 미커밋 작업은 보존하고 commit/push하지 않았다.
