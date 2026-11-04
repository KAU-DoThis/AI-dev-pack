# 5단계: 부모↔자식 Agent Communication

작성일: 2026-10-18 (Asia/Seoul). 구현 전 **141개**, 구현 후 **167개** unittest가 통과했다.
Root와 직접 자식이 독립적인 기존 Task 엔진으로 실행하면서 질문·답변·알림·지시를 주고받는다.
질문 전송은 즉시 반환하며 대기는 별도 도구다. 대기 중 새 입력을 받으면 기존 요청을 보존하고
도구 결과를 정상 기록한 뒤 다음 LLM 판단으로 돌아간다. 6단계 이후 기능은 구현하지 않았다.

## 1. 구현 기준과 확인받은 Host 정책

기준은 `docs/architecture/SHS_AGENT_TARGET_ARCHITECTURE.md`의 11절과 최신
`docs/reports/phase_4_subagents/phase_4_subagents.md`, 실제 현재 소스다.
기존 `host_ask`는 하나의 도구 실행에서 Host 답변까지 기다렸으므로 자식 질문이 도착했을 때의
재개 규칙을 사용자에게 확인했다. 사용자는 다음 방식을 선택했다.

> Host 질문을 유지하고 도구 대기만 해제해 자식 질문을 처리한다. 이후 Host 답변은 같은 question_id와 함께 다음 안전한 경계에서 Context에 반영한다.

`host_ask`는 보통 기존처럼 답변 문자열을 반환한다. Agent 입력으로 대기가 해제되면
`{pending: true, question_id, reason, request_ids, message_ids}`를 반환한다. Host 질문은 계속 열려 있고
AgentState의 `question_id`도 유지한다. 다른 Host 질문으로 덮어쓰거나 자동 답변하지 않는다.
같은 question/context로 다시 `host_ask`를 호출하면 기존 질문을 기다리며 ID와 원래 deadline을 유지한다.
Host 답변이 그 사이 도착하면 다음 안전한 경계에 `Host answer` 일반 user 메시지로 기록한다.
아직 Host 질문이 열려 있으면 `task_complete`를 거부한다.

기존 Host 추가 지시·취소는 기존 질문 무효화 규칙을 유지한다. 기존 `host_response_timeout`은
처음 질문한 시점부터 계속 적용된다. 대기를 해제했다고 시간을 새로 주지 않는다. 해제 뒤 deadline이
끝나도 실행 중 외부 LLM/도구를 강제로 취소하지 않고 안전한 경계에서 `incomplete/host_response_timeout`으로
종료한다. **Agent 질문에는 timeout·자동 취소·우선순위·공정성 정책을 추가하지 않았다.**

## 2. 변경 파일과 책임

| 파일 | 이번 변경의 책임 |
|---|---|
| `agent/communication/models.py` | 불변 `AgentMessage`, `AgentRequest`, `CommunicationError` |
| `agent/communication/endpoint.py`, `__init__.py` | 호출 Task가 고정된 Root/Child 통신 인터페이스. 자식에 Manager를 직접 전달하지 않음 |
| `agent/subagent/manager.py` | 실제 직접 관계 검증, 메시지 라우팅, 답변 수신자 결정, 관련 요청 참조, Task 종료 시 요청 닫기, 기존 자식 완료 대기의 입력 인식 |
| `agent/session/channel.py` | 수신·송신 기록 대기열, 요청 상태와 답변 연결, 협력적 대기 신호, Host 질문 보존·재개, 기존 Event/Artifact 발행 |
| `agent/runtime/runner.py` | 기존 실행 루프의 안전한 경계에서 Conversation 적용, 명시적 부모 지시 revision, LLM Context 반영 시점 추적 |
| `agent/tools/session_tools.py` | 실제 호출자에 바인딩된 6개 통신 도구의 조건부 등록·실행, 예약 이름과 완료 경계 검사 |
| `agent/tools/schemas.py` | 6개 Provider 독립 스키마, 기존 wait/host_ask 설명 확장 |
| `agent/context/conversation.py` | 송수신 방향·출처·ID를 가진 원본 메시지 기록, 아직 판단에 들어가지 않은 입력과 미해결 질문의 인덱스 |
| `agent/context/builder.py` | compaction 이후에도 첫 판단에 필요한 메시지와 미해결 질문, 지시의 출처 보존 |
| `agent/state.py` | 답변 대기 활동, 대기 Task/요청 ID, 미해결·대기열 ID. JSON round-trip |
| `agent/subagent/models.py` | TaskTree의 통신 Artifact에 포함된 Task 한정 참조 검증 |
| `agent/solver.py` | 기존 공통 조립 경로에서 각 Task의 좁은 통신 인터페이스 연결 |
| `agent/__init__.py`, `shs_agent/__init__.py` | `AgentMessage`, `CommunicationError` 공개 export |
| `tests/test_communication.py` | Fake Provider/Tool과 명시적 gate를 사용하는 신규 26개 검증 |
| `examples/communication.py` | 서버 없는 A→부모→B→부모→A 재현, 실제 스키마와 TaskTree 내보내기 |
| `README.md`, `MANIFEST.in` | 사용 계약 안내와 새 예제의 sdist 포함 |
| 이 보고서와 `.schema.json` 3개 | 이번 구현 계약과 실제 제공 스키마 |

`Tracer`의 기존 Event/Artifact 저장 및 조회 구현은 그대로 재사용한다. 새로운 Agent 실행 루프,
메시지 버스, 외부 서비스 또는 런타임 의존성은 없다. `asyncio.Event`는 기존 Channel 안의 대기 신호다.
TaskState/AgentState의 유일한 변경 소유자는 계속 TaskChannel이다.

## 3. 메시지·요청 모델

공통 메시지의 정확한 필드는 다음과 같다. ID는 라이브러리가 생성하고, from/to는 실제 바인딩과
Manager 관계표에서 결정한다. LLM은 송신자 ID나 답변 수신자 ID를 전달할 수 없다.

```json
{
  "message_id": "generated-message-id",
  "request_id": "generated-request-id-or-null",
  "from_task_id": "actual-sender",
  "to_task_id": "actual-recipient",
  "kind": "question | reply | notify | instruction",
  "content": "message body",
  "related_request_id": null
}
```

- `question`: 새 request_id를 생성한다. 답변을 기대한다.
- `reply`: 원래 request_id를 사용하며 기록된 수신자만 보낼 수 있다.
- `notify`: 정보 전달이며 request_id는 null이다. 지시 revision을 바꾸지 않는다.
- `instruction`: 부모→직접 자식의 추가 지시다. request_id는 null이며 기존 지시 반영 규칙을 사용한다.
- `related_request_id`: 송신 Task가 실제로 알고 있는 요청만 연결할 수 있다. A의 질문 qA를 받고 B에
  질문할 때 qB의 이 필드로 qA를 연결할 수 있다. qA/qB는 독립적인 요청이며 자동 응답 연결은 없다.

각 Channel은 자신이 당사자인 요청의 불변 상태 값을 보유한다. Manager가 양쪽 Channel을 한 번의
await 없는 라우팅 경계에서 갱신하므로 상태가 어긋난 채 다른 Task가 실행되지 않는다.
요청 상태는 `pending`, `answered`, `target_terminated`, `requester_terminated`다.
답변과 요청의 정규 Artifact 참조를 보관한다. 대기 해제는 상태를 닫지 않는다.

종료·취소 접수된 대상에 보내면 `accepted=false`와 `target_terminated` 또는 `target_cancelling`을
반환한다. 이미 답변했거나 닫힌 요청에 대한 답변은 `request_closed`와 `request_status`를 반환한다.
미등록 대상, 형제, 다른 Root, 임의 요청 접근, 요청자 자신의 답변 등은 `CommunicationError`다.
정상 Tool Runtime의 오류 envelope와 Tracer에 남고 다음 판단에서 회복할 수 있다.
잘못된 필드·타입·빈 문자열은 기존 인자 검증에 따라 TypeError/ValueError가 될 수 있다.

Task 종료 직전에 미해결 요청을 양쪽에 닫고 대기 신호를 보낸다. 요청자는 대상의 종료를 인식하여
무기한 답변 대기에 남지 않는다. 이미 완료된 답변은 상대의 후속 종료 때문에 덮어쓰지 않는다.

## 4. 실제 도구 스키마

실행 예제의 **실제 첫 LLM 요청에 제공된 스키마**를 저장했다.

- [communication_tools.schema.json](communication_tools.schema.json): 통신 도구 6개 전체.
- [root_internal_tools.schema.json](root_internal_tools.schema.json): 활성 Root 내부 도구 18개.
- [child_internal_tools.schema.json](child_internal_tools.schema.json): 자식 내부 도구 12개. 예제 외부 도구 2개 제외.

Root의 `subagent_spawn.config_id.enum`은 예제의 승인 설정 `["worker"]`다. 실제 실행에서는 Host 설정으로 결정된다.
Sub-agent 비활성 Root는 기존 도구만 제공한다. 활성 Root에 `parent_ask/notify`는 없고,
자식에 `host_ask`, spawn/status/result/wait/cancel, subagent_ask/send는 없다.
모든 통신 이름은 비활성 상황에서도 외부 도구 이름으로 등록할 수 없다.

| 도구 | 제공 대상 | 필수 인자 | 선택 인자 |
|---|---|---|---|
| `parent_ask` | 자식 | `message: string` | `related_request_id: string` |
| `subagent_ask` | 활성 Root | `task_id: string`, `message: string` | `related_request_id: string` |
| `agent_reply` | 활성 Root/자식 | `request_id: string`, `message: string` | 없음 |
| `agent_wait_reply` | 활성 Root/자식 | `request_id: string` | 없음 |
| `parent_notify` | 자식 | `message: string` | 없음 |
| `subagent_send` | 활성 Root | `task_id: string`, `message: string` | `kind: "notify" | "instruction"`, 기본 notify |

모든 문자열은 `minLength: 1`, 모든 object는 `additionalProperties: false`다. 핸들러 역시 알 수 없는
키를 거부한다. 선택 필드를 사용하지 않을 때는 생략한다. nullable JSON 스키마는 제공하지 않는다.
도구 결과는 기존 `{"ok": true, "output": ...}` envelope 안에 들어간다.

```json
{"accepted": true, "reason": "queued", "message_id": "m1", "request_id": "q1",
 "task_refs": [{"task_id": "sender", "artifact_id": "message-detail"}]}
```

위는 질문 접수 예시다. 알림·지시는 request_id가 null이다. 답변 접수는 reason=`answered`다.
접수가 대상의 Context 적용이나 작업 완료를 뜻하지 않는다. 거부한 질문의 생성 ID는 활성 요청으로 등록되지 않는다.

답변을 받은 `agent_wait_reply`의 output:

```json
{"reason": "answered", "request_id": "q1", "request_ids": ["q1"],
 "reply": {"message_id": "m2", "request_id": "q1", "from_task_id": "child",
           "to_task_id": "parent", "kind": "reply", "content": "answer", "related_request_id": null}}
```

새 질문으로 대기가 풀린 output:

```json
{"reason": "incoming_request", "request_id": "q1", "pending": true,
 "request_ids": ["q2"], "message_ids": ["m3"]}
```

`request_id`는 기다리던 q1, `request_ids`는 처리할 수신 요청들이다. 실제 질문 본문과 출처는
배치 종료 후 Conversation에 들어간다. `subagent_wait`는 동일한 reason/request_ids/message_ids와
기존 children 상태 목록을 반환한다. 자식 종료는 `terminated`이며 `subagent_result`로 결과를 따로 읽는다.

추가 대기 해제 사유는 `incoming_message`, `superseded_by_host_instruction`,
`superseded_by_parent_instruction`, `host_cancelled`, `host_answer`, `host_response_timeout`이다.
새 부모 지시는 자식에만 적용된다. Host 답변/timeout은 유지 중인 Root Host 질문에 해당한다.
요청 종료는 해당 요청 상태를 reason으로 반환하고 reply는 null이다. 외부에서 대기 coroutine 자체를
취소하면 `wait_cancelled`를 추적하고 CancelledError를 유지한다. 원래 Agent 요청은 취소하지 않는다.

## 5. 안전한 입력 적용과 교착 방지

```text
SessionTools의 실제 호출 Task
  → SubagentManager: 직접 관계/요청 소유권 확인
  → 송신 Tracer에 원문 기록, 수신 Tracer에는 Task 한정 참조
  → 수신 TaskChannel: 대기열·요청 상태 갱신
  → 실행 중 LLM/외부 도구는 계속 실행
  → 질문/완료/Host 대기라면 협력적으로 도구 결과 반환
  → Runtime: 같은 응답의 모든 tool call/result 기록 완료
  → Conversation: 송수신 방향, from/to, kind, request_id, related_request_id 기록
  → ContextBuilder: 다음 판단 입력 구성
  → 기존 LLM/Tool 루프에서 답변·조사·다른 자식 질문·원래 작업 계속
```

질문·알림·답변은 Host 지시 revision을 바꾸거나 진행 중 LLM 응답을 폐기하지 않는다.
부모의 명시적 instruction만 기존 revision 경계를 사용한다. 진행 중 도구는 완료를 기다리고,
이전 revision에서 계획한 배치의 남은 도구는 기존 방식으로 미실행 결과를 기록한다.
Conversation에는 한 응답의 tool call/result 사이에 어떤 통신 메시지도 끼워 넣지 않는다.
완료 도구가 실행되는 동안 새 입력이 도착한 경우에도 완료 전에 다음 판단에서 입력을 보게 한다.

`subagent_wait(A)` 중 A가 `parent_ask`하면 부모의 대기가 풀린다. 부모는 질문에 직접 답하거나 B에게
별도 질문을 보낼 수 있다. `agent_wait_reply(qB)` 중 다른 질문을 받아도 대기를 풀고 qB는 유지한다.
대기 상태 확인과 신호 clear 사이에는 await가 없어 수신을 놓치지 않는다. 대기 종료가 자식 실행 핸들을
취소하지 않도록 기존 shield 정책을 유지한다. 새 독립 판단 루프나 자동 중재 정책은 없다.
이미 Context에 전달한 질문을 매번 다시 새 수신으로 처리하지 않으며, 미해결 요청 ID는 계속 보존한다.
상호 무지 때문에 답이 없는 의미적 순환 의존은 부모/LLM이 조율해야 한다.

## 6. ContextBuilder·State·Tracer

수신 Agent 메시지는 출처가 포함된 **user 역할의 작업 데이터**, 송신 기록은 assistant 역할이다.
다른 Agent의 정보를 system 지시로 승격하지 않는다. Conversation/Memo/Planning은 계속 Task별 독립이다.
질문·답변을 Memo나 Plan으로 자동 복사하지 않는다. 질문의 답변 완료와 Task 완료도 독립이다.

ContextBuilder는 compaction에 접힌 미해결 송수신 질문을 request_id와 함께 보존한다.
아직 첫 판단에 들어가지 않은 수신 메시지도 원래 출처·유형·본문으로 포함한다. 첫 실제 decision 요청에
들어간 뒤에는 일반 이력으로 취급하되 미해결 질문과 명시적 지시는 유지한다.
요약 이후 원본은 기존 `conversation_search/read`로 조회할 수 있다.

AgentState에는 다음 작은 필드만 추가했다.

| 필드 | 의미 |
|---|---|
| activity=`waiting_for_parent_reply` / `waiting_for_child_reply` | 부모/자식 답변을 실제로 기다리는 중 |
| 기존 `waiting_for_subagents` | 자식 종료 대기 |
| `waiting_request_id` | 현재 기다리는 질문 |
| `waiting_task_ids` | 현재 기다리는 상대 Task 목록 |
| `outgoing_request_ids` | 이 Task가 보낸 미해결 질문 |
| `incoming_request_ids` | 이 Task가 받은 미해결 질문 |
| `queued_message_ids` | 아직 안전한 경계에서 적용하지 않은 수신 메시지 |
| 기존 `question_id` | 유지 중인 Host 질문. Agent request_id와 별도 |

본문을 State에 복사하지 않는다. immutable tuple을 사용하며 `to_dict()`에서는 JSON 배열로 내보낸다.
`SessionState.from_dict()`와 state cursor 재생은 동일한 값을 복원한다.

통신 추적에는 `agent_message_requested/accepted/received`, `agent_message_recorded`(송신 Conversation),
`agent_message_applied`(수신 Conversation), `agent_message_context_built`(실제 다음 판단 입력),
`agent_wait_started/finished`, `agent_request_closed`, `agent_message_rejected`, `agent_reply_rejected`를 사용한다.
기존 `subagent_wait_started/finished`도 해제 사유와 요청 목록을 남긴다.
Host 쪽에는 `host_wait_released`, `host_answer_applied`와 기존 question/question_closed가 연결된다.

정규 메시지 원문은 송신 Task의 `agent_message_requested` Artifact에 한 번 기록한다.
수신·적용·중재 관계는 `{task_id, artifact_id}`로 연결하며 `related_request_id`의 원래 질문도 연결한다.
기존의 실제 LLM 요청/응답·도구 인자/결과·최종 Conversation 상세 기록은 유지하므로 실행 증거로 필요한
본문 반복은 존재한다. 수신 전달 이벤트마다 원문 Artifact를 다시 만들지는 않는다.
source에는 실제 `llm_call_id`, `tool_execution_id`, `tool_call_id`, 도구 요청 Artifact가 연결된다.
Context 반영 기록은 사용된 LLM 공통 요청 Artifact까지 참조한다.

sequence는 계속 각 Task 안에서 독립적이다. TaskTreeSnapshot은 `subagent_*`와 `agent_*` Artifact의
task_refs를 검증한다. 전체 트리의 JSON round-trip과 실시간 이벤트/최종 이벤트 일치를 검증했다.
종료 직전 받은 메시지는 원본·수신·적용 기록이 남아도 다음 LLM 판단이 없을 수 있으며, 이 경우
`agent_message_context_built`를 만들어내지 않는다.

## 7. 사용 예시

Host는 기존 설정과 자식 Session을 그대로 사용한다. 질문 송신자는 가상의 Host Agent가 아니다.

```python
from shs_agent import AgentLimits, ProblemSolver, SubagentConfig
from shs_agent.tool import ToolRegistry, ToolExecutor

child_registry = ToolRegistry()  # Host가 허용한 외부 도구만 등록
root = await ProblemSolver().start(
    "A와 B를 생성해 로그인 구현을 나누고 결과를 검토하라.",
    provider=provider, model=model,
    limits=AgentLimits(max_total_subagents=2, max_subagent_depth=1),
    subagents={"worker": SubagentConfig(
        registry=child_registry, executor=ToolExecutor(child_registry))},
)

# 실제 Host에서는 별도 coroutine에서 root/child events를 구독한다.
async for event in root.events():
    if event.type == "question":  # 진짜 host_ask만 이 이벤트를 사용
        await root.answer(event.payload["question_id"], "Host가 결정한 답변")
    for artifact_id in event.artifact_ids:
        detail = await root.artifact(artifact_id)

result = await root.result()
for item in await root.subagents.children():
    child = root.subagents.child(item["task_id"])
    state = await child.state()
    trace = await child.trace()
    # child.events/artifact/send_instruction/cancel/result도 기존과 동일
tree_json = result.tree.to_json(indent=2)
```

아래는 실제 내부 도구 호출 순서 예시이며 Python 전역 함수 API는 아니다.

```text
A: parent_ask(message="로그인 응답 형식이 필요합니다") → qA
A: agent_wait_reply(request_id=qA)
Root: subagent_wait(task_ids=[A, B]) → reason=incoming_request, request_ids=[qA]
Root: subagent_ask(task_id=B, message="응답 형식을 확인해 줘", related_request_id=qA) → qB
Root: agent_wait_reply(request_id=qB)
B: 기존 도구 배치를 마친 뒤 agent_reply(request_id=qB, message="access_token과 user")
B: 원래 백엔드 작업 계속
Root: qB 답변 검토 후 agent_reply(request_id=qA, message="access_token과 user")
A: qA 답변을 사용하고 원래 작업 완료
Root: subagent_wait → subagent_result(A/B) → task_complete
```

부모가 이미 답을 알면 qB 없이 qA에 바로 답하면 된다. 긍정적인 권한 요청 답변만으로
자식 Registry/Executor를 변경하지 않으며, 실제로 제공되지 않은 도구 호출은 계속 unknown_tool이다.

## 8. 검증 결과와 재현

Python 3.12.9 / 기존 `.venv`를 사용했다. 처음의 기본 `python`은 3.10이며 패키지 검색 경로도 없어
테스트를 로드하지 못했다. 올바른 interpreter로 전환한 뒤 Windows 샌드박스의 asyncio 내부
loopback socketpair 생성이 차단되는 것을 stack dump로 확인했다. 승인된 정상 실행 환경에서는
수정 전 141개가 **9.019초**에 통과했다. 이 환경 문제를 제품 코드 변경으로 우회하지 않았다.

| 검증 | 결과 |
|---|---|
| 구현 전 전체 unittest | 141/141 통과 |
| 구현 후 전체 unittest | **167/167 통과, 14.195초** |
| 신규 통신 테스트 | 26개. 기존 141개 테스트 파일 보존 |
| 오프라인 wheel/sdist 빌드 | `uv build --offline` 성공 |
| 배포 검증: wheel 격리 환경 | **167/167 통과, 12.008초** |
| 배포 검증: sdist 격리 환경 | **167/167 통과, 12.246초** |
| 배포물 내용 검사 | 현재 Python 소스·테스트와 byte 단위 일치, old/_deprecated 제외 |
| 경량 import | `-I -S`에서 성공. torch/ollama/openai/sqlalchemy 및 보관용 Agent 모듈 미로드 |
| Fake 통신 재현 예제 | Root/A/B 모두 completed, 도구 오류 0, Tree 상세 참조·JSON 복원 성공 |
| `git diff --check` | 통과. 기존 작업의 LF/CRLF 안내만 출력 |

uv의 샌드박스 캐시 쓰기도 거부되어 정상 환경의 기존 캐시로 offline 빌드했다. 네트워크에서
모델이나 의존성을 받지 않았다. wheel/sdist는 `dist/`에 생성했다.

신규 테스트는 양방향 질문·답변, 여러 요청의 역순 답변, Notify/Instruction 차이, 두 wait의 새 질문 해제,
원래 요청 재대기, B의 도구 배치 중 수신과 후속 작업, LLM 중 질문/알림과 응답 유지, 지시에 따른 응답 폐기와
배치 잔여 정산, Conversation 독립성, 다른 Root·형제·임의 요청·송신자 위조 거부, 중복/늦은 답변과 대상 종료,
Host 지시·취소, Host 질문 보존·재개·늦은 답변·기존 deadline, 상태/cursor 및 실시간/최종 Tracer 일치,
손상된 cross-task 참조 거부, compaction 후 출처 보존, 동적 권한 미부여, 단일 Task 회귀를 포함한다.
동시성 순서는 asyncio.Event와 이벤트 관측으로 확정한다. deadline 검증도 기존 timer callback을 명시적으로
실행하며 고정 sleep을 쓰지 않는다. 테스트 harness의 watchdog은 제품 통신 timeout 정책이 아니다.

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe examples/communication.py --schema-dir docs/reports/phase_5_communication
uv build --offline
.\.venv\Scripts\python.exe tests/verify_distribution.py
```

이번 Fake 예제 원본은 [task_tree.json](../../../build/communication_demo/task_tree.json)과
[summary.json](../../../build/communication_demo/summary.json)에 있다. Root는 8 steps/10 calls,
A는 3/3, B는 4/4이며 모두 도구 오류 0이었다. B가 질문을 받은 순간과 답변 후에도
`running/tool_execution`이었던 상태를 summary에 보존했다. 재실행하면 이 예제 출력 경로는 갱신된다.

**실제 Ollama 검증은 이번 단계에서 실행하지 않았다.** Fake/Mock 통과를 실제 모델의 통신 판단 능력이나
추론 병렬성 검증으로 주장하지 않는다. 기존 Ollama 검증 스크립트와 과거 실제 실행 자료는 보존했다.
모델 다운로드·서버 설정 변경은 하지 않았다.

## 9. 남은 제약과 보존 사항

Root와 직접 자식(depth=1)만 지원한다. 재귀 생성, 형제 직접 채널, 동적 도구 권한 변경, 자동 스케줄러,
Storage 영속화·Resume, Host Memory/MindMap, 시각화 UI는 없다.
미해결 요청과 상세 이력은 Task/Session 수명 동안 메모리에 남는다. 수신/보존 한도나 TTL을 임의로 추가하지 않았다.
장시간 외부 작업은 협력적 경계까지 계속 기다리며 강제 중단하지 않는다. Agent가 질문을 이해하고
적절히 답하는 의미적 정확성, 공정성 및 순환 의존 해결은 보장하지 않는다.

기존 Planning/Replanning, TaskState/AgentState 소유권, AgentLimits, 부모 정상 완료 거부 및 비정상 종료 시
자식 취소·회수 정책을 유지했다. 기존 미커밋 변경·이전 보고서·old/_deprecated 보관본을 보존했다.
commit/push하지 않았으며 **5단계 구현에서 멈춘다.**
