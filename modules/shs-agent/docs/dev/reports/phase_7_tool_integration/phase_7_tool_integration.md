# 7단계: Tool Request 및 Host Integration

작성일: 2026-10-18 (Asia/Seoul).

Agent의 추가 도구 요청을 직접 부모 → Root → Host로 전달하고, Host가 승인한 실제 handler를
최초 요청 Task에만 적용한다. 적용된 스키마는 다음 LLM 판단에 제공되며 기존 Executor를 통해 호출한다.
요청, 승인, 적용, LLM 제공, 실제 실행을 각각 별도 기록으로 검증했다.

## 1. 구현 전 조사와 기준선

조사 대상은 [목표 아키텍처](../../architecture/SHS_AGENT_TARGET_ARCHITECTURE.md)의 7·11·12·14·16절,
[6단계 보고서](../phase_6_recursive_subagents/phase_6_recursive_subagents.md)와 현재 소스다.

| 기존 구조 | 확인한 계약과 이번 구현의 연결점 |
| --- | --- |
| `ProblemSolver._assemble_task` | Root/자손의 공통 Core/Context/Planning/Runtime/Session 조립 경로 |
| `SubagentManager` / `SubagentScope` | Root 전체 Host 조회와 Agent의 직접 자식 권한 분리; 기존 그대로 사용 |
| `SubagentConfig` | 자식 Registry/Executor는 명시적이며 부모 도구 자동 상속 없음 |
| `SessionTools.snapshot/check_available` | 제공된 이름·스키마·Registry version으로 호출 직전 변경 검증 |
| `ToolRegistry` / `ToolExecutor` | `Tool(ToolSpec, handler)` 등록과 실제 함수 호출; 교체할 필요 없음 |
| `TaskChannel` | Host 명령, 질문 ID, Agent request ID, replay 가능한 이벤트 및 대기 알림 |
| Communication | `parent_ask`, `agent_reply`, `related_request_id`, 직접 관계 검증, Task Artifact 참조 |
| Runtime | LLM 요청 중 Host 지시 revision 변경 시 응답 폐기; batch 중간에 Conversation 입력 삽입 금지 |
| Tracer / Tree | 개별 schema 2, Tree schema 2; Task별 기록과 cross-task Artifact 참조 |

기본 `python`은 Python 3.10이며 패키지 경로가 없어 최초 테스트 로드에 실패했다.
기존 `.venv`의 Python 3.12.9로 실제 **199/199 unittest, 22.045초** 통과를 확인하고 구현을 시작했다.
Windows 샌드박스에서는 asyncio 초기화가 정체되어 해당 실행을 중단하고 정상 실행 권한으로 검증했다.
기존 미커밋 변경, 목표 문서, 이전 보고서, `old/_deprecated` 보관본을 보존했다. commit/push하지 않았다.

## 2. 변경 파일과 책임

경로의 `agent/`는 `src/shs_agent/agent/`를 뜻한다. 이번 단계에서 수정한 책임만 적었다.

| 파일 | 책임 |
| --- | --- |
| `agent/tools/integration.py` (신규) | ToolRequest 수명·원래 요청자·전달 ID 연결, Host 결정 검증, Task별 적용 대기 |
| `agent/tools/session_tools.py` | Task 전용 추가 Registry/Executor, 안전한 적용, 스키마 revision/identity 및 제공 기록 |
| `agent/tools/schemas.py` | `tool_request`, `tool_forward`, `tool_wait`, `tool_status` 4개 내부 도구 |
| `agent/solver.py` | Root별 Host catalog와 Task별 endpoint를 기존 공통 조립 경로에 주입 |
| `agent/session/handle.py` | 공개 `grant_tool/deny_tool/tool_state/requests` |
| `agent/session/channel.py` | 구조화된 적용 결과 대기열, 대기 해제, Host 질문 보존 |
| `agent/runtime/runner.py` | batch 밖에서 승인 적용, 결과의 Conversation 반영, 실제 LLM 제공 및 종료 정리 |
| `agent/context/state.py`, `builder.py`, `conversation.py` | 현재 도구/요청 상태의 읽기용 Context, 결과를 user 작업 데이터로 기록 |
| `agent/subagent/models.py` | `allowed_tool_ids`, 새 Artifact의 cross-task 참조 검증 |
| `agent/subagent/manager.py` | 자식 안내와 승인 범위 기록; 기존 실제 관계 라우팅 재사용 |
| `agent/state.py` | 관찰 가능한 `waiting_for_tool_approval` 활동 |
| `src/shs_agent/__init__.py` | `ToolRequest`, `ToolIntegrationError` 공개 export |
| `tests/test_tool_integration.py` (신규) | Fake Provider/Gate 기반 신규 29개 테스트 |
| `examples/tool_integration.py` (신규) | 외부 서버 없는 전체 흐름, 비차단 관찰/결정 분리, 스키마/Tree export |
| `README.md`, `MANIFEST.in` | 사용법과 새 예제의 sdist 포함 |
| 이 보고서와 JSON 5개 | 구현 계약과 실제 Provider에 전달한 스키마 |

`shs_agent/tool/`의 Registry/Executor 구현은 변경하지 않았다. 새 Scheduler·권한 엔진·계정·UI·서버·
외부 도구 구현·런타임 의존성은 추가하지 않았다.

## 3. 모델과 공개 API

### 구성

```python
from shs_agent import ProblemSolver, SubagentConfig
from shs_agent.tool import Tool, ToolSpec, ToolRegistry, ToolExecutor

catalog = ToolRegistry()
catalog.register(Tool(ToolSpec("database_query", "Host transaction query", {
    "type": "object", "properties": {"months": {"type": "integer"}},
    "required": ["months"], "additionalProperties": False,
}), database_query))  # Host가 구현한 callable; 인증/실행 환경은 Host 소유

# 단일 Task도 동일하게 사용 가능
root = await ProblemSolver(provider).start(
    "query transactions", model="host-model",
    host_tools=catalog, allowed_tool_ids=("database_query",),
)
```

`create_task`, `start`, `solve` 모두 `host_tools: ToolRegistry | None`과
`allowed_tool_ids: tuple[str, ...]`를 받는다. 기본 `host_tools=None`은 기존 실행 경로를 유지하며
새 내부 도구를 노출하지 않는다. 명시적인 빈 catalog도 기능 활성화는 가능하다.

자손은 `SubagentConfig.allowed_tool_ids`를 별도로 지정한다. 기본 `()`는 동적 승인 대상 없음이다.
목록은 tuple로 고정하고 중복·잘못된 타입·미등록 ID를 검증한다. 기존 정적 Registry/Executor 권한과
동적 승인 대상 목록은 별개다. 부모의 목록·추가 도구를 자손에게 자동 복사하지 않는다.
Host가 처음부터 승인한 자식 도구는 기존 `SubagentConfig.registry/executor`로 제공한다.
동적 도구에는 개별 Host 결정을 요구하며 Agent의 승인/재위임 API는 추가하지 않았다.

### 요청 값

`ToolRequest`는 내부 수명 모델이며 공개 API/도구 반환은 독립된 dict 사본이다.

| 필드 | 의미 |
| --- | --- |
| `request_id` | 최초 도구 요청 ID. Host grant/deny는 이 값을 사용 |
| `requesting_task_id` | 최초 요청 Task; 전달한 부모 ID로 바뀌지 않음 |
| `requested_tool`, `reason`, `intended_use` | 요청 도구의 정확한 이름과 목적 |
| `status` | `requested`, `awaiting_host`, `approved`, `applied`, `denied`, `apply_failed`, `closed` |
| `hops` | 각 전달의 request ID, 실제 송수신 Task, 이전 `related_request_id` |
| `host_request_id` | Root가 Host 검토를 요청한 별도 ID; `host_ask.question_id`가 아님 |
| `host_request_ref`, `decision_ref` | Host 요청/결정의 Task-qualified Artifact 참조 |
| `decision_reason`, `revision` | 거부/적용 실패 사유, 실제 적용 revision |

### Host API

```python
target = root if requesting_task_id == root.task_id else root.subagents.child(requesting_task_id)
receipt = await target.grant_tool(request_id=original_request_id, tool_id="database_query")
assert receipt["status"] == "approved"  # 아직 적용 완료가 아님

# 별도 요청을 거부할 때:
# await target.deny_tool(request_id=original_request_id, reason="allowed data scope exceeded")

current = await target.tool_state()
print(current["tools"], current["revision"], current["schema_id"])
print(current["last_offered_revision"], current["requests"])
schema_detail = await target.artifact(current["artifact_id"])
```

grant/deny는 동일 event loop에서 검증·접수하며 진행 중 LLM/handler가 끝나기를 기다리지 않는다.
`tool_state`는 integration을 활성화한 Task의 현재 목록·schema identity·Artifact·revision·최근 LLM 제공
revision과 해당 Task가 실제 알고 있는 요청만 반환한다. `requests()`는 기존 일반 AgentRequest들의 사본이다.

승인 시 실제 존재하는 원래 요청, 정확한 대상, Root까지의 전달 완료, 미결정 상태, 대상/조상의 실행 상태,
catalog 등록, 이름 일치, Task 허용 목록, callable, 객체 스키마/필수 인자 및 handler signature의 일치를
검증한다. signature를 검사할 수 없는 callable은 Host가 명시적 Python wrapper로 연결한다.
전체 JSON Schema 검증 엔진을 새로 추가한 것은 아니다.

검증 실패는 `ToolIntegrationError`와 `tool_grant_rejected`로 반환하며 승인으로 기록하지 않는다.
요청은 `awaiting_host`로 남아 Host가 수정된 연결을 검토하거나 명시적으로 거부할 수 있다.
이미 승인/거부/적용한 요청, 다른 Root·형제·부모 Session, 전달 hop ID를 결정 대상으로 사용하면 거부한다.

## 4. 부모·Root·Host 요청 흐름

```text
A-1: tool_request(database_query, reason, intended_use)
  └─ 기존 ask_parent → AgentRequest q1 → A
A: tool_forward(q1)
  └─ 기존 ask_parent(related_request_id=q1) → AgentRequest q2 → Root
Root: tool_forward(q2)
  └─ tool_host_approval_requested: 원래 q1/A-1 + host_request_id
Host: manager.child(A-1).grant_tool(request_id=q1, tool_id=database_query)
  └─ approved → A-1의 적용 대기열
A-1: 현재 batch 종료 → 적용 → 다음 LLM schema → database_query 실행
  └─ parent_notify / 기존 자식 결과 회수 → A → Root 최종 완료
```

요청은 기존 Communication 질문으로 전달하고 구조화된 모델을 별도로 연결한다. JSON 본문이나
`agent_reply("Approved")`, `answer(question_id, "Approved")`를 파싱해서 권한을 주지 않는다.
`tool_forward`는 마지막 전달의 실제 직접 수신자만 호출할 수 있다. 형제/조부모를 지정하는 인자가 없다.

부모는 `agent_reply`로 이미 아는 정보를 답하거나 기존 도구로 해결할 수 있다. 이 답변도 `tool_wait`를
해제하여 다음 판단으로 전달하지만 요청 상태를 `approved/applied`로 바꾸지 않는다.
상위 전달이 필요할 때만 `tool_forward`를 사용한다. 도구 요청을 일반 질문으로 위장하거나,
일반 질문 ID를 구조화된 도구 요청처럼 승인할 수 없다.

## 5. Task별 실제 도구 집합

각 SessionTools는 기존 기본 Registry/Executor와 내부 도구 Registry를 유지하고, 승인된 추가 도구만
Task 전용 Registry와 기존 `ToolExecutor`에 연결한다. 같은 기본 Registry를 쓰는 형제·다른 Root에도
추가 도구는 나타나지 않는다. Host catalog를 실행 Registry로 직접 사용하거나 공유 Registry에 register하지 않는다.

승인 시 ToolSpec을 복사하고 실제 handler 참조를 함께 고정한다. 이후 catalog 교체는 이미 승인된
handler를 바꾸지 않는다. callable 내부의 인증 클라이언트·환경·동시 실행 안정성은 Host 책임이다.
handler 객체와 그 속성은 기록하지 않는다.

integration 활성화 시 기본 Registry의 시작 스키마/version을 고정한다. 기본 ToolRegistry에서는 handler
identity도 확인한다. 임의 변경은 `tool_changed`로 거부하며 다음 정상 스냅샷을 만들 수 없으면 기존
Runtime 실패 경로로 끝난다. 승인 API를 우회한 변경을 새 권한으로 인식하지 않는다.
기능을 활성화하지 않은 실행은 기존 스키마/version 변경 감지 계약을 유지한다.

적용은 후보 Registry를 구성한 후 전용 Executor와 함께 교체한다. 실패하면 이전 실행 집합을 유지하며
`apply_failed`를 기록한다. 승인만 있고 handler가 없는 적용 성공 상태는 만들지 않는다.

## 6. 안전한 변경 경계와 Context

```text
LLM 요청: revision 0
  → 응답 및 해당 tool call batch
  → batch 도중 Host 승인: approved (revision 0 유지)
  → 모든 기존 call/result 기록
  → 다음 판단 준비: 실제 handler 연결, revision 1 / applied
  → 다음 실제 common request: revision 1 / tool_set_offered
  → 실제 ToolExecutor 실행 및 결과 기록
```

적용 경계는 Runtime loop의 다음 판단 준비 지점이다. compaction 중 승인이 들어올 경우에도
compaction 종료 후 결정 요청 직전에 적용한다. 진행 중 LLM이나 외부 도구를 강제로 중단하지 않는다.
도구 승인은 Host 지시 revision을 올리지 않으며 그 이유만으로 LLM 응답을 버리지 않는다.
옛 응답에 새 도구 이름이 들어 있어도 당시 제공되지 않았다면 `unknown_tool`이다.

`tool_wait`는 승인 접수에서 해제된다. 적용 완료까지 tool handler 안에서 기다리면 안전 경계에
도달할 수 없으므로, `approved` 반환 → batch 종료 → Runtime 적용 순서를 사용한다.
새 질문·일반 답변·Host 지시·취소·Host 답변도 기존 알림을 사용해 대기를 해제한다. timeout/TTL/재시도는 없다.

Host/runtime 결과는 TaskChannel에서 보관하고 batch 밖에서 Conversation의 user 작업 데이터로 기록한다.
ContextBuilder는 현재 Task의 읽기용 도구 상태를 assistant 작업 데이터로 함께 제공한다.
system 메시지로 승격하거나 다른 Agent의 Conversation을 복사하지 않는다.
승인/적용/거부 결과는 실제 요청에 관여한 부모들에게도 Host/runtime 관찰 자료로 전달해 대기를 해제한다.
이 결과 전달은 부모가 다른 Task에 권한을 주는 별도 통신 채널이 아니다.

Host의 일반 `host_ask` 질문은 계속 독립적인 question_id를 사용한다. 대기 중 자식 질문 또는 도구
결정이 들어오면 원래 질문을 보존한 채 판단을 재개한다. 답변은 기존 question_id와 정확히 연결해야 한다.
종료 전에 미적용 승인 요청은 `apply_failed`, 결정 전 요청은 `closed`로 정리하며 성공으로 바꾸지 않는다.
거부/연결 실패 자체는 Task 실패나 자식 취소를 발생시키지 않으며 Agent가 대안·Replan을 선택한다.

## 7. Host 전체 트리 관찰·제어

기존 API를 재사용한다. TaskState/AgentState/Plan/LLM/Tool 상세/질문은 별도 GUI 모델을 만들 필요 없이 읽는다.

```python
manager = root.subagents
for item in await manager.descendants():
    target = manager.child(item["task_id"])
    state = await target.state()
    print(state.task.parent_task_id, state.task.depth, state.task.status, state.agent.activity)
    if state.task.plan_artifact_id:
        plan = (await target.artifact(state.task.plan_artifact_id)).data
    if state.agent.operation_artifact_id:
        detail = (await target.artifact(state.agent.operation_artifact_id)).data
    pending = [r for r in await target.requests() if r.status == "pending"]
    tools = await target.tool_state()
    # 각 사용자 조작에서 독립적으로 호출:
    # await target.send_instruction("limit analysis to the last three months")
    # await target.answer(question_id=state.agent.question_id, text="table")
    # await target.cancel()
    # await target.grant_tool(request_id=..., tool_id=...)
    # await target.deny_tool(request_id=..., reason=...)
live_tree = await manager.snapshot()
```

관찰은 `session.events(after_sequence=state.last_event_sequence)`로 이어받는다. 여러 구독자는 독립적이다.
Task별 sequence를 전역 순서처럼 비교하지 않는다. Host는 Root와 새로 등록된 각 Session을 관찰하고
자신의 Queue에 이벤트를 넘긴다. 질문 이벤트를 소비하는 coroutine에서 사용자 입력을 기다리지 않는다.

```python
async def observe(target, ui_queue):
    async for event in target.events():
        ui_queue.put_nowait((target.task_id, event))

# 별도 UI/server callback: 사용자 응답이나 결정을 받은 시점에 실행
# await target.answer(question_id=question_id, text=user_text)
# await target.grant_tool(request_id=original_request_id, tool_id=tool_id)
```

실행 가능한 [Fake Host 예제](../../../examples/tool_integration.py)는 새 자손 관찰 등록까지 수행한다.
Manager/Channel private 필드에 접근하지 않는다. 실제 UI·웹 서버·분산 이벤트 버스는 구현하지 않는다.

## 8. Tracer와 Tree

| 기록 | 의미/연결 |
| --- | --- |
| `tool_access_requested/forwarded` | 최초 요청자, 목적, 실제 hop 및 기존 AgentRequest Artifact |
| `tool_host_approval_requested` | Root의 별도 Host 검토 ID, 원래 요청 및 forwarding 참조 |
| `tool_host_approved/denied`, `tool_grant_rejected` | Host 결정 대상, 결정 전 검증 실패, 사유 |
| `tool_approval_received` | 승인 접수; 실제 적용과 분리 |
| `tool_definition` | 승인한 ToolSpec 한 번 저장; handler/인증 객체 제외 |
| `tool_set`, `tool_set_changed` | revision별 실제 스키마, SHA-256 identity, 이전 set 및 Definition Artifact |
| `tool_access_applied/apply_failed/closed` | 실행 연결 결과, Host 결정 Artifact와 연결 |
| `tool_access_observed/context_applied` | 관계자 관찰과 안전 경계에서 최초 Agent의 Context 반영 |
| `tool_set_offered` | 실제 llm_call_id/common_request Artifact와 당시 revision 연결 |
| 기존 `tool_request/dispatch/raw_result/result/finished` | 실제 호출과 반환; dispatch에 당시 tool_set_revision 추가 |

LLM common request는 호출 당시 전체 스키마의 사본을 이미 기록하므로 과거 호출을 현재 목록과 연결하지 않는다.
큰 스키마는 Artifact에 저장하고 이벤트는 revision/identity/참조/이름 목록을 사용한다.
결정 기록에는 handler·인증 객체·API Key 같은 연결 정보를 넣지 않는다. Host가 공개하는 스키마와
Agent가 쓰는 요청 본문 자체에 비밀을 넣지 않는 것은 기존 Host 계약이다.

개별 Tracer와 Tree envelope는 **schema 2 유지**, 기존 schema 1/2 읽기 범위를 유지한다.
새 event/artifact 종류와 `waiting_for_tool_approval` 활동 값은 추가 확장이다. 구 버전 코드가 새 활동 값을
역직렬화할 수 있다고 보장하지 않는다. 새 코드는 기존 파일을 읽는다.
Tree 검증을 새 tool 관련 `task_refs`까지 확장했으며, 잘못된 참조를 넣은 JSON은 거부한다.

```python
from shs_agent import TaskTreeSnapshot
result = await root.result()
text = result.tree.to_json(indent=2)
tree = TaskTreeSnapshot.from_json(text)
for task_id, trace in tree.traces.items():
    for event in trace.events:
        if event.type == "tool_set_offered":
            past_schemas = trace.artifact(event.payload["artifact_id"]).data["schemas"]
            past_input = trace.artifact(event.payload["request_artifact_id"]).data
```

## 9. 검증 및 재현

Python 3.12.9 / 기존 `.venv`, 표준 unittest와 Fake Provider/Tool을 사용했다.
동시성 순서는 `asyncio.Event`, Gate, 실제 이벤트로 동기화한다. sleep이나 실행 시간으로 성공을 판단하지 않는다.
테스트의 watchdog은 테스트 정체 감지용이며 제품 timeout 정책이 아니다.

| 검증 | 결과 |
| --- | --- |
| 구현 전 전체 unittest | **199/199**, 22.045초 |
| 새 Tool Integration 테스트 | **29/29**, 6.321초 |
| 최종 전체 unittest | **228/228** (기존 199 + 신규 29), 35.266초 |
| wheel/sdist | 최종 `uv build --offline` 성공, dist에 `.whl` / `.tar.gz` 생성 |
| wheel 격리 suite | **228/228**, 32.506초 |
| sdist 격리 suite | **228/228**, 35.942초 |
| 경량 import / 배포 내용 | `-I -S` 통과, 현재 소스/테스트 byte 일치, old/_deprecated 제외, torch/ollama/openai/sqlalchemy 미로드 |
| 새 예제 배포 | sdist에 포함된 `examples/tool_integration.py`와 작업 소스 byte 일치 |
| 정적 점검 | Python AST / JSON 구문 검사, `git diff --check` 통과 (기존 LF/CRLF 안내만 출력) |
| 독립 Fake 예제 | Root + 자손 2개 completed, 실제 database handler 1회, 결과 months=3 / transactions=73 |
| JSON export/re-read | live/최종 Tree, 새 참조 검증, 과거 스키마 보존 테스트 통과 |
| 실제 Ollama | 이번 단계에서 실행하지 않음 |

신규 검증은 요청만으로 권한 미부여, 자연어 Host/부모 승인 미부여, 정확한 대상/원래 ID,
허용 목록, handler 부재/인자 불일치, Root/형제 격리, 부모 정적 도구 미상속, 중복 결정 거부,
진행 중 LLM/외부 도구/batch 무결성, 복수 승인 revision, catalog 교체 격리, 기본 Registry/handler 변경 차단,
적용 실패 후 계속 실행, 거부 후 plan_revise, 취소 전 미적용, Host 질문 유지와 정확한 답변 ID,
대체 답변/Host 지시/결정에 따른 대기 해제, detached 상태, 인증 객체 미수집을 포함한다.
기존 199개가 Depth 1~3 실행, Host 전체 자손 제어, planning·communication·tracer 회귀를 담당한다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe examples/tool_integration.py --schema-dir docs/reports/phase_7_tool_integration
uv build --offline
.\.venv\Scripts\python.exe tests/verify_distribution.py
git diff --check
```

uv의 샌드박스 캐시 쓰기 거부도 확인되어, 기존 로컬 캐시를 사용하는 정상 권한 offline 빌드로 검증했다.
제품 코드에 환경 우회·sleep·재시도 정책을 추가하지 않았다.
최종 빌드 로그는 `build/phase_7_build.log`, 두 배포 격리 실행 로그는 `build/phase_7_distribution.log`에 보존했다.

실제 Provider 입력에서 내보낸 JSON:

- [새 내부 도구 4개](tool_integration_tools.schema.json)
- [Root 내부 도구](root_internal_tools.schema.json)
- [Depth 1 내부 도구](depth_1_internal_tools.schema.json)
- [Depth 2 내부 도구](depth_2_internal_tools.schema.json)
- [Depth 2 승인 후 실제 전체 도구](depth_2_after_grant.schema.json)

실제 수량은 Root 22개, Depth 1 23개, Depth 2 16개이며, Depth 2 승인 뒤에는 외부
`database_query`를 포함해 17개다. 새 도구 4개 파일은 실제 Root 제공 목록에서 추출했다.

Tree 원본은 `build/tool_integration_demo/task_tree.json`에 생성된다. 이 파일은 실행별 ID를 담는
검증 산출물이며 Git의 기존 build 제외 규칙을 유지한다.

## 10. 범위와 남은 제약

- 동적 catalog는 기본 ToolRegistry의 ToolSpec+callable 계약이다. 기존 사용자 정의 Registry/Executor는
  기본 도구로 계속 사용할 수 있지만 새 동적 도구는 catalog에 명시적인 handler로 연결해야 한다.
- 같은 asyncio event loop 안에서 사용한다. 다른 스레드/GUI에서의 event-loop 전달은 Host가 담당한다.
- 동적 승인은 현재 Task 한정이며 미래 자손의 권한 목록이나 정적 profile을 변경하지 않는다.
- handler의 실제 데이터 접근 범위, 인증, 부작용, 스레드/동시 실행 안전성은 Host가 구현한다.
- 승인 거부·적용 실패를 모델이 어떻게 해결할지는 Agent 판단이다. 자동 Replan·자동 실패·자동 재시도는 없다.
- 취소 접수 이후 승인·적용은 거부하며, 진행 중 외부 작업은 기존 협력적 취소 경계를 유지한다.
- 실행 중 회수·강제 중단, 임의 코드 생성/import/설치, 자동 권한 확대, RBAC/ACL, 계정 정책,
  TTL/요청 timeout, 새 Scheduler, 자동 영속화/Resume, Host Memory/MindMap, UI는 구현하지 않았다.
- 실제 외부 DB/PowerShell/browser/Ollama 연결 검증은 이 단계 범위 밖이다. Fake handler 호출을 실제 DB 접근으로 표현하지 않는다.

7단계의 요청 → Host 결정 → 실제 Task 적용 → 다음 LLM 제공 → Executor 호출/반환까지 구현했다.

2026-10-18 추가 검증에서는 실제 Ollama 모델 4개와 로컬 SQLite로 모델별 1회씩 실행했다.
원래 구현 검증과 구분한 실측 결과는 [Gemma 보고서](ollama_gemma_report.md),
[Qwen 보고서](ollama_qwen_report.md), [비교·아키텍처 보고서](ollama_comparison_architecture_report.md)에 있다.
