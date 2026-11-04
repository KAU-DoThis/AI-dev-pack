# 4단계: 직접 Sub-agent와 병렬 실행

작성일: 2026-10-17. 기존 소스에서 수정 전 unittest **116개**가 통과함을 확인했다.
Root → 직접 자식(depth=1)까지 기존 Task 엔진을 재사용하는 선택적 Sub-agent 실행을 추가했다.
정상 흐름에서는 부모가 두 자식의 결과를 명시적으로 회수하고 계획을 갱신한 뒤 완료한다.
이 문서는 이번 대화에서 확인받은 실행 정책과 실제 구현을 기록한다. 과거 아키텍처·보고서는 수정하지 않았다.

## 1. 적용한 정책

아키텍처 10절·18.2절 및 요청문에서 미확정으로 남긴 항목을 구현 전에 질문했다.
사용자는 첫 제안을 수정하여 다음 설정 상속을 허용했고, 완료 거부와 비정상 종료 시 협력적 취소·회수를 선택했다.

| 항목 | 이번 계약 |
|---|---|
| 활성화 | `start/create_task/solve(subagents={...})`로 Host가 명시. `None`이면 비활성화. 활성 설정 사전은 비어 있지 않아야 함 |
| Root 한도 | `AgentLimits.max_total_subagents`, `max_subagent_depth` 두 값 모두 Host 지정 필수. 새 필드의 `None`은 미지정이며 무제한이 아님 |
| 허용 값 | 누적 수는 정수 ≥0. 깊이는 0 또는 1. bool·음수·깊이 2 이상은 오류 |
| 설정 선택 | Host의 `SubagentConfig` 사전에서 LLM이 `config_id` 선택. 승인된 Provider·모델·옵션·도구 조합만 실행 |
| 기본 상속 | 설정별 Provider·모델·LLM Options·개별 AgentLimits 생략 시 부모 값 사용. 모델은 spawn 시 부모의 실제 현재 모델 |
| 도구 | Registry/Executor를 각 설정에 명시. 부모의 모든 외부 도구 자동 상속 없음. 다른 부분집합이 필요하면 Host가 별도 설정으로 등록 |
| 생성 집계 | 공통 조립·관계 기록·Session 등록을 마친 시점에 +1. 등록 전 오류는 0. 등록 후 예약/실행 실패는 계속 집계. 완료해도 차감 없음 |
| 부모 완료 | 실행 중 자식이 있으면 `task_complete`가 `SubagentError`. 부모 다음 판단으로 복귀. 자동 대기·완료 없음 |
| 부모 비정상 종료 | 실패·취소·개별 한도·미완료 종료에서 실행 중 자식에 협력적 취소 요청 후 모두 회수. 자식 자동 재시작 없음 |
| 새 부모 지시 | 자식 자동 전파 없음. 필요하면 Host가 해당 자식의 기존 `send_instruction()` 호출 |
| 대기 해제 | 새 부모 지시·취소는 현재 `subagent_wait`만 해제. 자식 취소는 별도의 명시적 cancel 또는 부모 종료 정리 단계에서 발생 |
| 보존 수명 | 실행 중 Manager가 Session·관찰 핸들·회수 결과를 보유. 종료 후에도 Manager를 가진 Root Session에서 조회 가능. TTL·삭제·분리 실행 없음 |

`INCOMPLETE`도 정상 완료가 아니므로 같은 비정상 종료 정리를 적용한다. 부모 취소 접수·명령 적용은 기존 경계를 따른다.
이미 실행 중인 부모의 외부 호출은 종료를 기다리고, 부모 종료 정리에서 자식 취소를 전파한다.
자식의 외부 호출도 강제 종료하지 않으므로 부모 결과 반환이 늦어질 수 있다. 외부 작업을 자동 rollback하지 않는다.
일반 완료에서 **종료한** 자식 결과를 LLM이 읽었는지까지 강제하지는 않는다. 예제는 모두 명시적으로 읽는다.

## 2. 파일별 책임과 공통 조립

| 파일 | 책임 |
|---|---|
| `agent/subagent/models.py` | Host 승인 설정 `SubagentConfig`, 요청 오류, `TaskTraceRef`, 독립 `TaskTreeSnapshot` 및 JSON/관계 참조 검증 |
| `agent/subagent/manager.py` | Root 범위 등록·누적 예산·Session/실행 관찰 핸들·회수 결과 소유. 상태는 자식 Channel에서 읽음. 생성/조회/대기/취소/종료 정리 |
| `agent/subagent/__init__.py` | 실제 모델·Manager export |
| `agent/solver.py` | 공통 `_assemble_task()`와 Registry/Executor 검증. Root Manager 생성, Host 설정 해석과 자식 조립 함수를 Manager에 주입 |
| `agent/tools/schemas.py` | 5개 내부 도구의 Provider 독립 스키마 |
| `agent/tools/session_tools.py` | 기존 로컬 Registry/Executor에 조건부 등록. Runtime에 바인딩된 호출자 ID 사용. 선택적 Plan 참조 확인과 완료 가능 여부 검사 |
| `agent/runtime/runner.py` | 기존 판단/도구 루프 유지. 명령 경계에서 자식 대기 해제. 종료 전 협력적 회수 및 결과에 tree 연결 |
| `agent/session/channel.py` | TaskState 관계 정보 초기화·검증. 상태/이벤트의 기존 단일 소유자 유지 |
| `agent/session/handle.py` | Root의 공개 `subagents`, 기존 자식 Session 인터페이스, 실행 중 `trace()` 사본. 등록 후 예약 실패도 결과로 관찰 가능 |
| `agent/state.py` | TaskState 관계 3필드, 실제 활동 `waiting_for_subagents` |
| `agent/types.py`, 공개 `__init__.py` | 한도 2필드, `TaskResult.tree` 선택 필드, 새 공개 모델 |
| `tests/test_subagent.py` | 설정·한도·병렬·독립성·취소·실패 회복·저장/참조 경계 25개 테스트 |
| `tests/verify_ollama_subagents.py` | 별도 실행하는 실제 Ollama 검증. 부모 도구 선택·자식 실행·동시 진행·SDK 구간·기존 서버 로그를 원본과 함께 저장 |
| `examples/subagents.py` | 서버 없이 실행하는 Gate 기반 통합 예제와 실제 스키마/JSON 내보내기 |
| `README.md`, `MANIFEST.in` | 사용 안내 및 sdist에 새 예제 포함 |

```text
Solver.create_task: 기존 Host 작업 설정 검증
  → Root TaskChannel 생성 (parent=None, root=self, depth=0)
  → 명시적 활성화 시 Root Manager 1개 생성
  → _assemble_task(request, provider, model, registry/executor, limits, channel, manager)
      TaskContext(Conversation/Memo/Compaction) + ContextBuilder + Planning
      AgentCore + SessionTools + TaskRuntime + TaskSession

부모 LLM → 일반 ToolCall → SessionTools → Manager.spawn
  → 설정·한도 확인 → Solver가 주입한 자식 조립 함수
  → 같은 _assemble_task(..., child_channel, manager=None)
  → 관계/위임 설정 기록과 등록·집계 → Session.start 예약 → 실제 현재 상태 즉시 반환
  → 기존 Runtime의 LLM↔Tool 루프 → 단독 task_complete
```

자식용 새 Root 관리자/예산/판단 루프가 없다. Manager는 Runtime을 직접 만들지 않고 Runtime도 Solver를 import하지 않는다.
같은 Solver의 두 Root는 서로 다른 Manager·등록표·예산을 가진다. Sub-agent를 비활성화한 기존 작업에는 Manager가 없다.
자식은 기존 9개 도구 중 `host_ask`를 제외한 8개를 사용하고, Sub-agent 도구는 제공하지 않는다.
활성 Root는 기존 9개 + 새 5개를 사용한다. 숨긴 이름을 외부 도구로 등록하는 우회도 예약 이름 검사에서 거부한다.

Manager는 별도 가변 TaskState/AgentState를 저장하지 않는다. `state()`와 도구 status가 읽는 값은 자식 Channel의 불변 사본이다.
각 Task의 Conversation·Memo·Summary·Plan·Channel·Core·Builder·Runtime·내부 도구 바인딩은 독립적이다.
공유 가능한 것은 Host가 연결한 Provider·Registry·Executor·외부 자원이다. 이 객체들의 동시 사용 적합성과
실제 파일·프로세스·권한 격리는 Host 책임이다. worktree·sandbox·새 프로세스는 요구하지 않는다.

## 3. Host 설정 API

```python
from shs_agent import AgentLimits, ProblemSolver, SubagentConfig
from shs_agent.tool import ToolExecutor, ToolRegistry

child_registry = ToolRegistry()  # 위임을 허용한 Tool만 등록
worker = SubagentConfig(
    provider=child_provider,
    model="host-approved-model",
    options={"temperature": 0},
    registry=child_registry,
    executor=ToolExecutor(child_registry),
    limits=AgentLimits(max_steps=12, max_tool_calls=20),
)
root = await ProblemSolver().start(
    "필요한 조사를 위임하고 결과를 검토해 최종 답변하라.",
    provider=root_provider, model="root-model", options=None,
    limits=AgentLimits(max_total_subagents=2, max_subagent_depth=1),
    subagents={"worker": worker},
)
```

`SubagentConfig`는 keyword-only이며 `registry`, `executor`가 필수다. Provider·model·limits는 `None`일 때 상속한다.
options는 **생략**하면 상속하고 명시적 `None`은 None을 그대로 적용한다. 내부 sentinel은 LLM 스키마에 노출하지 않는다.
system_prompt는 기본 TaskRequest 안내를 쓰고 Host가 바꿀 수 있다. 부모 Conversation이나 부모 system_prompt를 자동 복제하지 않는다.
자식 질의/재귀 미지원 안내를 자식 지시에 추가한다. 부모가 전달한 instruction/context는 일반 user 작업 데이터이고
자식 결과·Memo·Plan은 system 지시로 승격되지 않는다.

```python
# 상속을 쓰더라도 외부 도구는 반드시 명시한다.
default_worker = SubagentConfig(registry=child_registry, executor=ToolExecutor(child_registry))
# 별도 Provider·모델 또는 다른 허용 도구 부분집합은 다른 config_id로 등록한다.
```

이름은 Host가 정하는 실행 설정 식별자이며 고정 역할 Agent가 아니다. LLM에 Provider 객체·인증·Python 코드·임의 옵션을 받는
도구 인자는 없다. 자식 생성 시 Host가 미리 등록한 다른 설정을 선택하여 모델 등을 바꿀 수 있다.
별도 자식 AgentLimits에는 Root 한도 필드를 넣을 수 없다. 기본 상속도 Root 한도 두 필드를 제거한 개별 제한만 복사한다.
실제 Root 예산은 기존 Manager의 값이다. Provider/도구 객체는 공유하되 옵션·설정 사전은 사본으로 고정한다.
승인 뒤 Registry 스키마가 바뀌면 새 자식 생성을 거부한다. 기존 Task 실행 중 도구 변경 검사는 기존 계약을 유지한다.
동적 도구 승인 기능이나 별도 권한 프레임워크는 추가하지 않았다.

## 4. 내부 도구의 정확한 계약

실제 예제의 첫 Root LLM 호출에서 제공한 스키마를 내보냈다.

- [subagent_tools.schema.json](subagent_tools.schema.json): 새 내부 도구 5개.
- [internal_tools.schema.json](internal_tools.schema.json): Root 내부 도구 14개 전체. 예제의 외부 `parent_work`만 제외.

이 예제의 `config_id.enum`은 `["worker"]`다. 실행 때 Host 설정 사전의 키로 생성되므로 다른 Root에서는 달라진다.
모든 새 도구는 `additionalProperties: false`이고 handler의 인자 계약도 알 수 없는 키를 거부한다.
아래 output은 기존 Runtime의 `{"ok": true, "output": ...}` 안에 들어간다.
도구 오류는 `{"ok": false, "error": {"type": "SubagentError 또는 기존 오류 타입", "message": "..."}}`다.
스키마 누락/추가 인자는 TypeError, 구성/한도 검증은 ValueError/SubagentError, Plan 자체 검증은 PlanError가 될 수 있다.
성공·실패 모두 기존 tool_calls/tool_errors와 개별 max_tool_calls 규칙을 따른다.

| 도구 | 필수 인자 | 선택 인자 | output |
|---|---|---|---|
| `subagent_spawn` | `instruction: string`, `context: string[]`, `config_id: string` | `plan_ref: {plan_id, revision, item_id}` | task_id, state, done, result_available, trace_ref |
| `subagent_status` | 없음 | `task_id: string` | `{children: [현재 상태...]}`. 지정하면 한 자식, 생략하면 직접 자식 전체 |
| `subagent_result` | `task_id: string` | 없음 | 상태 + available. 종료 결과가 있으면 output/status/stop_reason/memos/task_refs |
| `subagent_wait` | `task_ids: string[]` (빈 배열·중복 불가) | 없음 | `{reason, children: [현재 상태...]}` |
| `subagent_cancel` | `task_id: string` | 없음 | `{accepted, message_id, ...현재 상태}` |

instruction/config_id/task_id는 공백 없는 내용이 필요하다. context는 빈 배열도 허용한다.
`plan_ref`를 주면 세 필드 모두 필요하며 실제 **현재** 부모 Plan의 plan_id/정수 revision/item_id인지 확인한다.
생성 당시 Plan Artifact를 생성 기록에 연결한다. 자식 완료·실패가 부모 Plan을 바꾸지는 않는다.
parent/root/source Task ID는 도구 인자로 받지 않는다. Runtime의 SessionTools 바인딩을 사용한다.
다른 Root·형제·Root 자신의 ID·없는 ID를 직접 자식으로 조회/취소할 수 없다.

```json
{"name":"subagent_spawn","arguments":{"instruction":"A 조사","context":["필요한 배경"],"config_id":"worker"}}
```

spawn output의 구조 예시 (ID·시각·상태는 매 실행의 실제 값):

```json
{
  "task_id":"child-A",
  "state":{
    "task":{"task_id":"child-A","parent_task_id":"root","root_task_id":"root","depth":1,"status":"created"},
    "agent":{"activity":"preparing","steps":0,"tool_calls":0},
    "last_event_sequence":2
  },
  "done":false,"result_available":false,"trace_ref":{"task_id":"child-A"}
}
```

여기서 state의 부수 필드는 읽기 편하게 생략했다. 실제 state는 기존 SessionState 전체 작은 필드를 포함한다.
spawn은 자식 종료를 기다리지 않는다. `created/preparing`은 등록·예약된 현재 상태이며 실행 완료를 뜻하지 않는다.
한 응답의 spawn 두 개도 일반 도구로 순차 실행하지만 각각 별도 asyncio Task를 예약하므로 동시에 진행한다.
일반 외부 도구 배치를 일괄 병렬화하지 않았다.

```json
{"task_id":"child-A","available":false,"done":false,"result_available":false,"state":{"...":"실제 현재 상태"},"trace_ref":{"task_id":"child-A"}}
```

result의 종료 사례:

```json
{
  "task_id":"child-A","available":true,"done":true,"result_available":true,
  "state":{"...":"실제 종료 상태"},"trace_ref":{"task_id":"child-A"},
  "status":"completed","stop_reason":"task_complete","output":"A 조사 결과",
  "memos":[{"memo_id":"m1","task_id":"child-A","content":"발견","kind":"finding","source_refs":[],"created_step":1}],
  "task_refs":[{"task_id":"child-A","artifact_id":"final-context-id"}]
}
```

실행 중 result는 종료를 기다리지 않는다. Channel이 닫힌 자식에 한해서 이미 끝난 실행의 결과 관찰을 마친다.
상태 종료와 결과 보관 사이의 짧은 경계에서는 status의 done=true/result_available=false 조합도 가능하다.
실패 자식도 available=true와 status=failed로 반환한다. 전체 Conversation·Tracer는 결과에 자동 주입하지 않는다.
Memo에는 생성 task_id를 유지한다. 큰 도구 결과는 기존 bounded tool content 정책을 그대로 따른다.

wait는 지정한 자식 **모두**의 종료를 기다린다. reason은 `terminated`, `superseded_by_host_instruction`, `host_cancelled`다.
대기 핸들 자체가 취소되면 `wait_cancelled`를 기록하고 호출자의 CancelledError를 유지한다.
child runner/관찰 핸들은 shield하여 대기 취소로 취소되지 않는다. 종료는 result로 별도 읽는다.
향후 질문 수신을 연결할 수 있는 interrupt 경계만 있으며 질문 프로토콜/빈 통신 계층은 만들지 않았다.
부모의 남은 도구 배치는 기존 지시 revision/취소 규칙에 따라 미실행으로 정산한다.

cancel은 선택 자식의 기존 Session.cancel을 호출한다. `accepted=true`와 message_id는 취소 **접수**다.
이미 종료한 자식에는 accepted=false/message_id=null과 실제 상태를 반환한다. 다른 자식에는 영향을 주지 않는다.

## 5. Host의 실시간 조회와 결과 회수

```python
manager = root.subagents                  # 비활성 Root/자식은 None
for item in await manager.children():      # 빠른 현재 상태 사본 목록
    child = manager.child(item["task_id"])
    state = await child.state()
    print(state.task.parent_task_id, state.task.root_task_id, state.task.depth)
    if state.agent.operation_artifact_id:
        print((await child.artifact(state.agent.operation_artifact_id)).data)

    # 실제 Host에서는 별도 coroutine에서 자식 이벤트를 구독한다.
    async for event in child.events(after_sequence=state.last_event_sequence):
        for artifact_id in event.artifact_ids:
            detail = await child.artifact(artifact_id)
        print(child.task_id, event.sequence, event.type)

    # 필요한 경우 명시적 지시·취소. 자동 전파가 아님.
    # await child.send_instruction("수정된 조사 범위")
    # receipt = await child.cancel()
    child_result = await child.result()

root_result = await root.result()
tree = await manager.collect()  # 현재 등록된 자식들을 명시적으로 join하고 사본 내보내기
```

`children()`은 Task 진행이 없는 시점에는 빈 목록일 수 있다. 생성 알림은 Root의 `subagent_created` 이벤트로 찾는다.
`manager.child(task_id)`는 실제 기존 TaskSession을 반환하므로 private 필드 접근이 필요 없다.
`await root.trace()`와 `await manager.snapshot()`은 실행 중에도 읽을 수 있고 자식 완료를 기다리지 않는다.
`collect()`는 호출 시 등록된 자식만 join한다. Root 실행 자체를 완료시키거나 이후 생성될 자식까지 미리 기다리지 않는다.
완전한 사후 자료는 정상적으로 Root 결과를 기다린 다음 collect하거나 `root_result.tree`를 사용한다.
`solve()`의 결과에도 tree가 들어가므로 Session을 보관하지 않은 호출자가 자식 기록을 잃지 않는다.

Root 상태에 자식 전체 상태를 저장하지 않는다. Root가 waiting_for_subagents여도 자식은 tool_execution/llm_call일 수 있다.
state.last_event_sequence는 해당 Task의 cursor다. 서로 다른 Task의 sequence=10끼리 전체 순서를 비교하지 않는다.
생성/전달은 명시적 참조 관계로 연결하고 시간은 기존 UTC timestamp를 사용한다.

## 6. Tracer 연결과 사후 저장

각 Task는 기존 상세 Tracer 수준을 유지한다. 공통/Adapter LLM 요청·관찰된 응답·공개 thinking·도구 인자/원본 결과·
Conversation/Memo/Summary/Plan 및 상태 이력은 해당 Task에서 보존한다. 이번 Fake Provider는 SDK Adapter 관측이 없으며
이를 별도 SDK 호출인 것처럼 꾸미지 않는다. 기존 Adapter 관측 테스트는 전체 suite에서 유지한다.

| Event/Artifact | 실제 의미 |
|---|---|
| 부모 `subagent_spawn_requested` | instruction/context/config_id/선택적 plan_ref, 생성 요청을 한 tool_request·LLM 호출 참조 |
| 부모 `subagent_spawn_rejected` | 등록 전 오류 또는 생성 한도 거부와 당시 누적 수. 성공 생성 사건을 만들지 않음 |
| 부모 `subagent_created` | 실제 등록, 관계, 설정 ID, 누적 수, 자식 task_input/configuration을 가리키는 task_refs |
| 자식 `subagent_configuration` | 적용 Provider 클래스·모델·옵션·개별 한도·Root 한도·승인 도구 스키마. 접속/인증 객체 자체를 저장하지 않음 |
| 자식 `subagent_origin` / `subagent_bound` | 부모 생성 Artifact를 Task 식별자와 함께 참조, 관계 확정 |
| 부모 `subagent_result_pending/delivered` | 결과 조회와 부모 전달 연결. 해당 도구 요청·LLM/도구 실행 ID 및 자식 final_context/최종 Plan 참조 |
| 부모 `subagent_wait_started/finished` | 대기 대상과 실제 해제 사유 |
| 부모 `subagent_cancel_requested` | 선택 자식 취소 접수 ID, 실제 종료와 구분 |
| 부모 `subagent_terminated` | 관찰한 실제 자식 결과 상태/종료 이유와 상세 자료 참조 |

status 조회, 인자 형식 오류, 존재하지 않는 자식 요청도 기존 tool_request/dispatch/tool_result 경로에 남는다.
plan_ref를 SessionTools에서 거부한 경우에는 그 일반 오류 기록이 남고 Manager의 성공 생성 사건은 없다.
실제 종료는 자식의 finished에도 기록된다. 부모 Tracer의 로그를 닫기 전에 관찰 핸들을 모두 회수한다.

동일 Task의 Event.artifact_ids와 Artifact.references 검증은 바꾸지 않았다.
**Task 간 연결**은 Sub-agent Artifact 본문의 `task_refs: [{task_id, artifact_id}]`로 별도 표현한다.
TaskTreeSnapshot이 모든 해당 링크를 검증한다. 자식 상세 본문을 부모 Artifact에 복사하지 않는다.
부모와 자식이 같은 tool_call_id를 사용해도 task_id와 각 Task의 operation_id·실제 요청 Artifact로 구분된다.
부모에게 전달된 출력은 일반 도구 결과로 기록되지만 자식의 전체 대화/Tracer는 매 결과마다 반복 복제하지 않는다.

```python
from pathlib import Path
from shs_agent import TaskTreeSnapshot

Path("run-tree.json").write_text(root_result.tree.to_json(indent=2), encoding="utf-8")
# 여기부터 Provider, Session, Manager 없이 읽을 수 있다.
saved = TaskTreeSnapshot.from_json(Path("run-tree.json").read_text(encoding="utf-8"))
for task_id, trace in saved.traces.items():
    print(trace.state().task, saved.result(task_id))
    for artifact in trace.artifacts.values():
        if artifact.kind.startswith("subagent_"):
            for reference in artifact.data.get("task_refs", []):
                linked_detail = saved.artifact(reference)
                print(reference["task_id"], linked_detail.kind)
```

Tree export는 schema_version=1, scope=root_and_direct_children이고 내부 각 Tracer는 기존 schema_version=2다.
기존 Tracer schema 1/2 읽기와 complete/details_complete 의미를 유지한다. 새 선택 필드가 없는 과거 State도 기존 reader로 읽는다.
새 Tree 포맷은 이번 관계 필드와 등록 사건이 있는 자료를 대상으로 하며 과거 단일 Task 자료를 재귀 트리로 추정하지 않는다.
JSON에서는 tuple이 배열로 변환되는 기존 규칙을 유지한다. 사본 변경은 실행 중 원본이나 다른 소비자에게 영향을 주지 않는다.

- 각 trace.complete: 해당 Task 로그가 닫혔음.
- tree.complete: 등록된 Root와 모든 직접 자식 trace가 닫혔음. 자식 trace를 빼면 읽기 검증에서 오류.
- details_complete: 선언된 관찰 범위의 수집 성공. Task 성공·결과 사실성·GPU 실행 성공을 뜻하지 않음.

부모만 닫혔고 자식이 살아 있는 시험 자료에서는 tree.complete=false다. 현재 승인된 정상/비정상 종료 경로는
자식을 회수한 뒤 부모 결과를 반환하므로 최종 tree는 모두 닫힌 상태다. 실행 중 snapshot은 부분 자료다.
Host가 프로세스/이벤트 루프를 강제로 종료하는 상황의 자료 보존·재개는 보장하지 않는다.

## 7. 검증 결과

저장소 Python 3.12.9, 표준 unittest와 Fake Provider/Tool을 사용했다. PATH Python 3.10은 지원 범위 밖이며 src가
설정되지 않은 최초 실행은 import 실패였다. 제한 환경에서 asyncio 테스트가 정지하여 해당 시도를 중단하고
자동 승인된 일반 실행 환경에서 수정 전/후 suite를 실행했다. 모델 다운로드·서버 설치·설정 변경은 하지 않았다.

| 검증 | 결과 |
|---|---|
| 수정 전 전체 unittest | 116개 통과 |
| 신규 Sub-agent 검증 | 25개 통과 |
| 수정 후 전체 unittest | 141개 통과 |
| Gate 통합 예제 | 두 자식 실행 진입 + 부모 도구 동시 진행 + 상태/대기/결과/Plan/최종 회수 확인 |
| JSON 사후 자료 | Task 관계·상세 cross-task 참조·결과 재읽기, 기존 same-task 검증, 사본 독립성 통과 |
| wheel/sdist 빌드 | `uv build --offline` 성공. 새 의존성 설치/네트워크 없이 빌드 |
| wheel 격리 실행 | `-I -S`, site-packages 없이 141개 통과 |
| sdist 격리 실행 | 압축된 현재 소스·테스트를 별도 임시 경로에서 실행, 141개 통과 |
| 배포 내용·경량 import | 모든 현행 소스/테스트 bytes 일치, old/_deprecated·옛 단일 파일 제외, torch/ollama/openai/sqlalchemy 미로드 확인 |
| 예제·스키마 | sdist 예제 원문 일치. 내보낸 5개 스키마가 실제 상수 + Host config_id enum과 일치 |
| 실제 모델 후속 검증 | 기존 로컬 Gemma/Qwen 각각 1회. 실제 부모의 두 spawn·wait·각 result 선택 확인. 상세 결과와 한계는 §7.1 |

주요 테스트는 asyncio.Event gate를 사용한다. A/B 모두 실제 외부 도구에 진입한 뒤 부모 도구가 이를 확인하고,
Host가 실행 중 State/Artifact를 읽은 후 완료 gate를 연다. 짧은 sleep이나 전체 시간 비교로 병렬성을 주장하지 않는다.
성공 예제 외에도 활성 자식이 있는 부모 완료 거부, 부모 실패/취소/step 한도 회수, 선택 자식 취소, 부모 지시로 wait 해제,
직접 wait/collect 대기 핸들 취소와 자식 보존, 실패 자식 결과에 따른 부모 Replan을 독립 검증했다.
서로 다른 Root 격리, 누적 상한과 동시 요청, 등록 전 실패/등록 후 예약 실패, 지원 깊이, 잘못된 ID,
미승인 설정/권한 인자, 옵션 None과 기본 상속, Registry 변경·Executor 불일치, Plan 참조 오류도 검증했다.
자동 tests는 전체 공개 thinking/Adapter 등 기존 검증 의도를 그대로 유지한다.

배포 검증은 기존 `tests/verify_distribution.py`를 그대로 사용했다. wheel suite는 8.172초,
sdist suite는 8.413초였으며 소스 suite는 9.984초였다. 이 시간은 성능 비교 자료가 아니다.
최초 제한 환경의 uv 실행은 sandbox 캐시 파일 접근 오류로 실패했다. 일반 실행 환경의
`uv build --offline`으로 재시도하여 두 배포물을 생성하고 위 격리 검증을 완료했다.

실행 예:

```powershell
$env:PYTHONPATH = 'src'
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
& '.\.venv\Scripts\python.exe' examples/subagents.py
uv build --offline
& '.\.venv\Scripts\python.exe' tests/verify_distribution.py
```

최초 성공 예제 자료: `build/subagents_demo/20261017T142407Z-a92b842a/`의 tree.json과 internal_tools.schema.json.
매 실행은 새 이름의 디렉터리를 만들고 이전 자료를 덮어쓰지 않는다. 이 build 자료는 기존 Git 제외 정책을 따른다.
검토용 스키마는 위 보고서 폴더에도 별도 보관했다.

### 7.1 기존 로컬 Ollama 실제 모델 검증

사용자의 추가 요청에 따라 2026-10-17 23:37–23:38 KST에 기존 `http://localhost:11434`에서
`gemma4:e4b`, `qwen3.5:9b`를 순서대로 각각 한 번 실행했다. Ollama 서버는 **0.32.15**, Python SDK는 **0.6.2**다.
`/api/tags`로 설치 여부를 먼저 확인했으며 모델 다운로드·서버 설치·환경/서버 설정 변경·commit/push는 하지 않았다.
두 모델을 동시에 실행한 실험은 아니며, 각 실행의 부모와 두 자식이 같은 모델을 사용했다.

| 모델 | 설치된 digest | 양자화 |
|---|---|---|
| `gemma4:e4b` | `c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb` | Q4_K_M |
| `qwen3.5:9b` | `6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7` | Q4_K_M |

요청 옵션은 기존 검증과 같은 `temperature=0`, `max_tokens=4096`, `think=true`, `num_ctx=16384`다.
이는 개별 호출 옵션이며 서버의 기존 `OLLAMA_CONTEXT_LENGTH=16384`나 병렬 설정을 변경하지 않았다.
Root는 14 steps/28 tool calls, 각 자식은 8 steps/12 tool calls, Root 누적 생성 수 2/깊이 1로 제한했다.
이번 작은 시나리오는 compaction을 끄고 실행했다. HTTP 클라이언트 timeout 180초, gate timeout 120초,
전체 실행 watchdog 360초를 두었으며 실제 timeout·취소·자동 재시도는 없었다.

**검증 방식.** 프롬프트는 첫 응답에서 A/B용 `subagent_spawn` 두 개, 이후 부모 작업·status·wait·각 result·최종 완료를
명시적으로 요구한다. 실제 모델이 도구 호출과 인자를 생성하고 일반 Registry/Executor 경로로 실행한다.
Host가 `Manager.spawn()`이나 결과 도구를 대신 호출하거나 모델 응답을 주입하지 않는다.
따라서 이 검증은 **지시된 프로토콜을 실제 모델이 도구 선택으로 수행하는지**를 확인한다.
도구 순서를 지시하지 않은 자유 과제에서 모델이 스스로 위임 필요성을 발견하는 능력까지 검증한 것은 아니다.

각 자식에게 허용된 외부 도구 `read_partition`은 A=`[13, 8, 21]`, B=`[5, 17, 9]`와 매 실행 생성한 출처 식별자를 반환한다.
자식 둘이 도구에 진입하면 부모가 선택한 `parent_probe`가 당시 자식 상태를 읽고 gate를 연다.
이후 자식은 실제 LLM 판단으로 Memo와 최종 답을 만들며, 부모는 회수한 결과로 A=42, B=31, 합계=73과 출처를 제출한다.
gate는 Task/도구 실행 구간의 중첩을 관찰하기 위한 장치다. 외부 작업 처리량이나 추론 속도 측정용 부하는 아니다.

| 관측 | `gemma4:e4b` | `qwen3.5:9b` |
|---|---|---|
| 부모/자식 A/자식 B 종료 | 모두 completed | 모두 completed |
| 부모 step / tool calls | 6 / 8 | 5 / 9 |
| 자식 각각 step / tool calls | 3 / 3 | 3 / 3 |
| 전체 Task 도구 오류 | 0 | 0 |
| 첫 실제 부모 응답의 spawn | 같은 응답에 2개 | 같은 응답에 2개 |
| 두 spawn 반환 때 자식 | 모두 done=false | 모두 done=false |
| 부모가 선택한 회수 | status, 두 ID wait, 각 ID result | 각 ID status, 두 ID wait, 각 ID result |
| 최종 판단 요청에 포함된 result 메시지 | 2개 | 2개 |
| 자식/부모 최종 합계와 출처 | 모두 정확 | 모두 정확 |
| 검증 항목 | **25/26**, Memo 출처 1건 불일치 | **26/26** |
| 전체 경과 시간 | 37.637초 | 38.686초 |
| SDK 요청 최대 동시 대기 수 | 3 | 3 |
| 서로 다른 Task의 SDK 구간 중첩 쌍 | 12 | 10 |
| 이번 runner의 `n_seq_max` | 1 | 1 |

경과 시간에는 모델 로드, 서버 대기, 추론, 도구와 관찰 비용이 포함된다. 모델 성능 순위나 병렬 속도 향상을 뜻하지 않는다.
Gemma의 wait는 자식이 이미 종료한 뒤 즉시 반환했다. Qwen의 wait는 14:38:13.003900–14:38:15.948113 UTC,
약 2.944초 동안 자식 종료를 기다린 후 `reason=terminated`로 반환했다.

**Gemma의 불일치.** B 자식의 Memo `source_refs`는 `["tool:read_partition"]`였다.
부모의 위임 지시는 실제 반환 출처를 쓰라고 명시했으며 기대 값은 `["fixture:ffc99fc3cfd6:B"]`다.
Memo 소유 `task_id`는 올바르고 자식 최종 output 및 부모 최종 output의 출처는 정확했다.
그러므로 이는 모델이 작성한 Memo 출처 내용의 오류이며 Task 간 소유 관계나 전달 기록의 손상은 아니다.
검증기 `child_memo_provenance`가 이를 실패로 판정했고 전체 실행 exit code도 1로 남겼다.
결과를 수정하거나 통과할 때까지 재실행하지 않았다. 현재 Memo 계약은 임의 출처 문자열의 의미적 정확성까지 강제하지 않는다.

**Task 동시 진행과 실제 추론 병렬성.** 다음 세 관측을 구분한다.

1. Task/도구 동시 진행은 확인했다. Gemma 14:37:26.973550 UTC, Qwen 14:38:06.719238 UTC의 `parent_probe`에서
   두 자식 모두 `running/tool_execution`, `done=false`, 두 gate 진입 완료·해제 전이었다.
   그 상태에서 부모 도구가 실행되었으므로 부모가 자식 하나의 전체 종료까지 직렬로 기다리지 않음을 보여준다.
2. 클라이언트 SDK 호출 중첩도 확인했다. 실제 `adapter_request`→`adapter_response` 관측 시각을 연결하면 두 모델 모두
   최대 3개 요청 구간이 겹친다. 예를 들어 Gemma 첫 자식 A는 14:37:22.953970–14:37:24.366020 UTC,
   자식 B는 14:37:22.956060–14:37:25.607653 UTC이며, 부모의 다음 호출도 14:37:22.959738에 시작했다.
   이 구간에는 전송·스케줄링·서버 queue 대기가 포함된다. 토큰 계산이 겹친 시간으로 해석할 수 없다.
3. 다중 요청의 실제 추론 병렬성은 확인되지 않았다. 기존 서버 설정은 `OLLAMA_NUM_PARALLEL=1`이고,
   이번 각 실행에서 새로 기록된 runner 명령은 `-np 1`, context는 `n_seq_max=1`, 생성된 slot은 `id 0`이었다.
   [Ollama 공식 FAQ](https://docs.ollama.com/faq#how-does-ollama-handle-concurrent-requests)는
   `OLLAMA_NUM_PARALLEL`을 모델별 동시 처리 요청 수의 상한으로 설명한다.
   이 실험은 모델별 단일 요청 슬롯에서 실행됐으므로 위 SDK 중첩을 동시 토큰 생성의 증거로 삼을 수 없다.
   GPU 레이어 offload 기록도 병렬 요청 처리나 속도 향상을 증명하지 않는다. GPU kernel 타임라인·처리량 비교는 수행하지 않았다.

**상세 기록.** 모든 Task에서 공통 요청→Adapter 요청→Adapter 응답→공통 응답→정규화 응답의 다섯 관측 경계와 공개 thinking을
보존했다. 실시간 event 구독과 최종 trace의 일치, 실행 중 Artifact 조회, state cursor 재생,
TaskTree JSON round-trip 및 cross-task 참조, `complete/details_complete` 모두 통과했다.
이는 수집 완전성 검증이며 Gemma의 Memo 내용까지 정확하다는 뜻은 아니다.
부모의 `subagent_created` source는 실제 부모 응답의 ToolCall에 연결되고,
`subagent_result_delivered`는 각 자식의 종료 결과와 최종 판단 전 도구 메시지에 연결됨을 확인했다.

원본 자료는 [실행 디렉터리](../../../build/ollama_subagents/20261017T143706Z-87177c99/)에 보관했다.

- [metadata.json](../../../build/ollama_subagents/20261017T143706Z-87177c99/metadata.json): 프롬프트 원문·서버/SDK·설치 모델 정보.
- [summary.json](../../../build/ollama_subagents/20261017T143706Z-87177c99/summary.json): 26개 항목의 원래 판정·Task 통계·SDK 시간 구간·서버 증거.
- `gemma4_e4b.*.json`, `qwen3_5_9b.*.json`: 모델별 tree/result/summary/server_evidence. tree는 전체 상세 원본,
  result는 최종 결과·Memo·gate 관측, server_evidence는 서버 로그의 관련 부분만 담는다. 환경 설정 전체 줄은 복사하지 않았다.
- Gemma Root `c09fa0005a354832b065b698009af120`, 자식 A `8180c0ffa97b4472bb919c2597f04812`, B `987dd12441ad4a80ae1e66a66543cdfa`.
- Qwen Root `a08f595230d14807a9df4e4857e27260`, 자식 A `b9584774fe9f40b9b533e477ab8ba9c9`, B `c8386eff9f38438aa1646aae8f71b4b4`.

재현 명령은 다음과 같다. 모델이 없으면 다운로드 대신 오류로 종료한다. 기본 실행은 두 모델을 차례로 검사하며,
`--model gemma4:e4b` 또는 `--model qwen3.5:9b`로 선택할 수 있다. Windows 로컬 서버 로그는
현재 사용자 홈의 `AppData/Local/Ollama/server.log`에서 읽는다. 로그를 읽지 못하면 서버 병렬성 근거는 미확인으로 남는다.

```powershell
$env:PYTHONPATH = 'src'
& '.\.venv\Scripts\python.exe' tests/verify_ollama_subagents.py
```

후속 검증에서 런타임 구현은 변경하지 않았다. 전체 unittest 재실행은 **141개 통과, 9.113초**였다.
검증 스크립트를 포함해 `uv build --offline`과 배포 검증도 다시 실행했다.
wheel 격리 환경은 **141개 통과, 8.247초**, sdist 격리 환경은 **141개 통과, 8.344초**였으며
현재 소스·테스트와 배포물의 내용 일치 및 보관용 코드 제외를 확인했다.

## 8. 남은 범위

이번 단계는 단일 부모와 직접 자식만 지원한다. 재귀 생성·부모↔자식 질문/답변·형제 중재·동적 도구 승인/추가·
동시 실행 슬롯 스케줄러·DB/Resume·Host Memory·시각화 앱은 구현하지 않았다.
질문을 자식의 host_ask로 우회하지 않으며 미지원 내부 도구를 제공하지 않는다.
미래 재귀 트리의 종료/전파/집계, 질문 우선순위와 응답 대기, 분리 실행/강제 프로세스 종료·재개 정책은 아직 확정하지 않았다.
이번 합의는 인메모리 직접 자식에 대한 종료·상속·집계 경계만 정한다.

위 실제 모델의 각 1회 프로토콜 이외 자유 위임 판단·장시간 동작·공유 파일 부작용·공정성·실제 서버 병렬 처리량·
반복 강제 인프라 취소는 이번 검증 밖이다.
비동기 Task 동시 진행은 추론 서버의 동시 처리나 GPU 속도 향상과 별개다.
새 런타임 의존성은 없으며 기존 SQLAlchemy/선택 SDK 선언은 유지했다. 기존 미커밋 변경과 과거 보관본을 보존했고 commit/push하지 않았다.
