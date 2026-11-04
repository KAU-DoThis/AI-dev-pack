# 3단계: 단일 Task Planning / Replanning

작성일: 2026-10-17. 현재 소스의 기존 91개 테스트를 수정 전에 확인하고,
선택적 Planning과 네 내부 도구를 기존 LLM↔Tool 루프에 연결했다.
새 테스트 25개를 포함한 총 116개가 통과했다. 초기 구현의 모델 흐름은 Fake Provider로 검증했으며,
후속 요청으로 수행한 실제 로컬 Ollama 검증은 §7.1에 별도로 기록한다.
이전 보고서와 목표 아키텍처 문서는 당시 내용 그대로 보존했다.

## 1. 파일별 책임과 소유권

| 파일 | 책임 |
|---|---|
| `agent/planning/plan.py` | `Planning`, `PlanSnapshot`, `PlanItem`, `PlanItemStatus`, `PlanError`. 변경 검증·현재 계획·불변 revision 이력의 단일 소유자 |
| `agent/planning/__init__.py` | Planning 패키지 export |
| `agent/solver.py` | Task마다 Planning 하나를 생성. `channel.publish_plan`과 SessionTools·Runtime에 연결 |
| `agent/tools/schemas.py` | 네 계획 도구의 실제 Provider 독립 스키마 |
| `agent/tools/session_tools.py` | 기존 로컬 ToolRegistry/ToolExecutor로 도구 등록·실행, 호출 출처 전달 |
| `agent/context/state.py` | `ContextSnapshot.plan` 선택 필드. `TaskContext.snapshot(plan=...)`로 읽기용 값만 전달 |
| `agent/context/builder.py` | 현재 Plan을 assistant 역할의 작업 데이터로 한 번 조합 |
| `agent/runtime/runner.py` | 일반 판단·Context 크기 계산에 현재 Snapshot 공급, 도구 요청 Artifact ID 전달, 종료 시 `TaskResult.plan` 반환 |
| `agent/state.py` | TaskState에 작은 계획 식별·참조 필드 3개 추가 |
| `agent/session/channel.py` | TaskState/AgentState의 기존 단일 소유자 유지. Plan revision Artifact 등록·상태 갱신·변경 Event 발행 |
| `agent/tracer/models.py` | 계획 Event를 과거 상태 재구성에 포함. recording_scope에 Planning 관찰 범위 명시 |
| `agent/types.py`, 공개 `__init__.py` | 선택적 결과 필드·데이터 모델 export·기본 도구 사용 지침 |
| `tests/test_planning.py` | 11개 단위 테스트와 14개 통합 테스트 |
| `tests/test_context_builder.py`, `tests/test_contracts.py` | 노출 도구·예약 이름을 9개로 확장. 기존 실행 검증 의도 유지 |
| `tests/verify_ollama_planning.py` | 후속 실제 모델 검증용 opt-in 스크립트. 메모리 fixture·의도적 환경 오류·29개 관찰 조건 확인 및 원본 보존 |
| `README.md` | 기능 범위·도구 개수·실시간 Plan 조회 안내 갱신 |

Planning은 불변 Snapshot의 revision 이력을 보유하며 현재 값은 마지막 revision이다.
Context·TaskState에는 별도로 수정 가능한 Plan을 저장하지 않는다. ContextSnapshot과 TaskResult는
읽기용 값이며, Tracer의 각 revision Artifact는 독립적인 과거 기록 사본이다.
`Planning.snapshot()`의 값은 frozen dataclass/tuple/string/Enum으로 구성되고 `to_dict()`는 새 중첩 사본을 반환한다.
Session의 result/artifact 사본 격리도 기존 방식으로 유지한다.

Planning에는 LLM 호출·추론 루프·도구 실행·파일 작업·자동 스케줄러가 없다.
의존성은 구조 검증 대상인 관계 데이터이며, 선행 작업 완료나 동시 진행 개수를 강제하지 않는다.

## 2. 데이터와 변경 계약

`PlanSnapshot` 필드:

| 필드 | 타입·의미 |
|---|---|
| task_id, plan_id | 문자열. Task별 독립 계획. plan_id는 최초 생성 시 UUID hex이며 이후 유지 |
| revision | 1부터 시작하는 정수. 실제 항목 변경마다 1 증가 |
| items | 순서 있는 `tuple[PlanItem, ...]` |
| change_kind | `created`, `updated`, `revised` |
| reason | 변경 요청자가 제공한 비어 있지 않은 문자열 |
| source_refs | 변경 이유의 근거 참조 문자열 tuple |

`PlanItem` 필드:

| 필드 | 타입·기본값 |
|---|---|
| item_id | 비어 있지 않은 안정적인 문자열 |
| description | 비어 있지 않은 작업 설명 |
| status | `pending` 기본값. `in_progress`, `completed`, `blocked`, `failed`도 허용 |
| depends_on | 선행 item_id tuple, 기본 `()` |
| source_refs | 결과·근거 참조 tuple, 기본 `()` |
| status_note | 상태 설명 문자열, 기본 `""` |

JSON/도구 표현에서는 tuple이 배열, Enum이 문자열이 된다. 상태는 LLM이 기록한 판단이다.
`completed`는 테스트 실행이나 엔진의 독립 검증 성공을 보장하지 않으며 `failed`도 Task 실패가 아니다.
모든 항목이 완료되어도 Task가 자동 완료되지 않는다. 단독 `task_complete`는 계속 정상 완료의 유일한 경로이고,
종료 시 pending/blocked/failed 항목을 임의로 완료 처리하지 않는다. 계획 상태는 완료의 필수 조건도 아니다.

변경 전 전체 후보를 검증한다. 중복 ID·없는 선행 항목·자기 참조·순환 의존성·중복 의존성·잘못된 상태·필드·타입은
`PlanError`로 거부한다. 변경 요청의 `expected_revision`이 현재 정수 revision과 다르면 거부한다.
의존성 검증은 반복형 위상 탐색이며 긴 유효 체인도 재귀 깊이 제한에 의존하지 않는다.
기존 Plan에 대한 create도 오류이며 덮어쓰지 않는다.

조회·거부된 변경·동일한 항목 목록의 재전송·빈 update는 revision과 변경 Event를 만들지 않는다.
항목이 그대로이면 reason/source_refs만 다른 요청도 no-op이다. 그 요청 자체는 일반 도구 기록에 남는다.
빈 Plan 생성과 모든 항목을 제외하는 빈 revise 목록은 허용하지만 Task 상태를 바꾸지 않는다.

기존 ID를 유지한 revise 항목은 생략한 필드 전체를 보존한다. 따라서 `{"item_id": "research"}`만 전달해도
기존 완료 상태·설명·결과 참조가 유지된다. 새 작업이나 의미가 바뀐 대체 작업에는 새 ID를 사용한다.
제외된 ID는 과거 revision에 남으며 다른 작업에 재사용할 수 없다. 다시 도입할 작업에는 새 ID와 이유를 지정한다.
기존 완료 항목의 재작업은 status를 명시적으로 변경하고 필수 reason에 이유를 기록한다.
엔진은 자연어 의미의 동일성이나 근거의 유효성을 독립적으로 판정하지 않는다.

## 3. 정확한 도구 스키마와 예시

실제 상수에서 내보낸 전체 Provider 독립 스키마는 [plan_tools.schema.json](plan_tools.schema.json)에 있다.
상위 인자와 항목 객체는 모두 `additionalProperties: false`다.
문자열 ID·설명·이유·참조에는 `minLength: 1`을 선언하며 Runtime은 공백만 있는 문자열도 거부한다.
ToolExecutor는 기존대로 handler를 호출하고, 구조·타입 검증은 Planning이 수행한다.

| 도구 | 필수 인자 | 선택 인자 |
|---|---|---|
| `plan_create` | `items: array`, `reason: string` | `source_refs: string[]` (생략 시 `[]`) |
| `plan_read` | 없음 | `revision: integer >= 1` (생략 시 현재) |
| `plan_update` | `updates: array`, `expected_revision: integer >= 1`, `reason: string` | `source_refs: string[]` |
| `plan_revise` | `items: array`, `expected_revision: integer >= 1`, `reason: string` | `source_refs: string[]` |

- create의 각 항목은 item_id와 description 필수. 나머지는 위 PlanItem 표의 선택 필드다.
- update의 각 항목은 item_id 필수. description, status, source_refs, status_note만 부분 갱신할 수 있다.
  항목 추가·제외·순서·depends_on 변경에는 revise를 사용한다.
- revise의 각 항목은 item_id 필수. 기존 항목의 생략 필드는 보존된다. 새 ID에는 description도 필요하며
  나머지는 create 기본값을 사용한다. 스키마에는 기존/신규 ID를 알 수 없어 이 조건을 Planning에서 검증한다.
- 배열 필드를 명시하면 해당 필드 전체를 대체한다. `[]`는 참조/의존성을 비우는 뜻이다.
  `depends_on` 배열에는 `uniqueItems: true`를 선언한다.
- update/revise 인자에 최신 revision을 사용한다. 정수 대신 bool/실수/문자열을 전달하는 경우도 거부한다.

변경 성공 output은 `{"changed": true, "plan_id": "...", "revision": 2, "artifact_id": "..."}`다.
no-op은 동일한 참조와 `changed: false`를 반환한다. 과거 계획 전체를 일반 도구 결과에 반복 복제하지 않는다.
read output은 `{"plan": {...}}`이며 생성 전 현재 조회는 `{"plan": null}`이다.
존재하지 않는 과거 revision은 오류다. 모든 결과는 기존 Runtime의 `{"ok": true, "output": ...}`로 감싼다.
계획 오류는 `{"ok": false, "error": {"type": "PlanError", "message": "..."}}`로 다음 LLM 판단에 전달한다.
누락/알 수 없는 상위 인자는 기존 handler 계약에 따라 TypeError로 반환될 수 있다.
실행된 계획 조회·변경·실패·no-op도 일반 도구와 같은 tool_calls/tool_errors 및 한도 규칙을 따른다.

다음은 LLM이 보내는 tool name과 arguments 예시다. 설명을 위해 순서대로 적었으며 각 호출 사이에는 일반 판단이 있다.

```python
("plan_create", {
    "reason": "조사·수정·검증을 구분한다",
    "items": [
        {"item_id": "research", "description": "원인 조사"},
        {"item_id": "edit", "description": "수정", "depends_on": ["research"]},
        {"item_id": "test", "description": "테스트 실행", "depends_on": ["edit"]},
    ],
})

("plan_update", {
    "expected_revision": 1, "reason": "조사 도구의 결과를 확인했다",
    "updates": [{"item_id": "research", "status": "completed",
                 "source_refs": ["tool:actual-research-call-id"], "status_note": "원인 함수 확인"}],
})

("plan_revise", {
    "expected_revision": 2, "reason": "실행 환경 부족. 허용된 정적 검토 후 테스트 미실행을 보고한다",
    "source_refs": ["tool:actual-failed-test-call-id"],
    "items": [
        {"item_id": "research"}, {"item_id": "edit"},
        {"item_id": "review", "description": "정적 검토 및 남은 검증 제약 기록", "depends_on": ["edit"]},
    ],
})

("plan_read", {"revision": 2})  # 제외된 test 항목도 조회 가능
```

위 `actual-...`는 실제 도구 호출 ID로 대체한다. Plan 생성·변경만으로 조사/수정/검토 도구가 실행되지는 않는다.
재작업 예시는 `plan_update(updates=[{"item_id": "research", "status": "in_progress"}],
expected_revision=3, reason="새 소스로 기존 결론을 다시 확인해야 한다")`다.
완료 당시 상태와 결과는 이전 revision에서 계속 조회할 수 있다.

### 근거 참조

`source_refs`는 변경 전체 또는 개별 항목에 둘 수 있다. 기존 Memo처럼 메시지·도구·Memo·외부 근거를 표시한다.
`message:<index>`는 최종 Conversation의 0 기반 인덱스, `tool:<tool_call_id>`는 해당 호출/결과,
`memo:<memo_id>`는 Memo를 가리키는 관례다. 원본은 Tracer의 tool 요청/결과와 final_context에 남는다.
이 문자열들과 파일/URL 등 외부 참조의 실제 의미·존재·내용은 엔진이 검증하지 않는다.

`artifact:<artifact_id>`는 예외적으로 같은 Task에 이미 등록된 Artifact인지 적용 전에 검증한다.
존재하지 않거나 다른 Task의 Artifact면 변경 전체를 거부한다. 검증된 ID는 `TraceArtifact.references`와
변경 Event의 artifact_ids에도 넣으므로 내보낸 Tracer의 기존 참조 검증을 적용한다.
외부 파일의 bytes를 이 참조만으로 자동 수집하지 않으며, 참조가 있다는 이유로 검증 성공으로 표시하지 않는다.

## 4. Context·상태·Tracer 연결

```text
Solver: Task별 Planning(publish=channel.publish_plan) 생성
  → LLM이 실제 계획 도구 호출
  → Runtime의 tool_request / tool_dispatch 등록 및 일반 tool_started
  → SessionTools의 로컬 Registry / Executor
  → Planning 후보 구성·형식/ID/revision/의존성/이유 검증
  → Channel에서 명시적 Artifact 근거 참조 검증
  → plan_revision Artifact 등록 (이전 Artifact·요청·근거 참조)
  → TaskState의 작은 계획 참조 교체 + plan_* Event 발행
  → Planning 불변 revision 이력에 적용
  → 기존 도구 원본 결과·Conversation·tool_finished 보존
  → 다음 일반 판단의 ContextBuilder에 현재 Snapshot 전달
```

검증 이후 Artifact 등록·상태/cursor 변경·이벤트 발행·Planning 반영까지 await가 없다.
단일 소유 asyncio 루프에서 Host 조회가 이 경계 중간에 끼어들지 않는다.
상세 내용은 이벤트 발행 전에 등록되며, 오류는 현재 Plan과 작은 상태 참조를 그대로 유지한다.
Plan 사건 하나에 갱신된 state를 포함하므로 별도 state_changed로 같은 변경을 중복 발행하지 않는다.
과거 변경 전 상세는 `before_artifact_id`로 재사용한다.

TaskState의 새 선택 필드는 `plan_id`, `plan_revision`, `plan_artifact_id`이며 생성 전에는 모두 None이다.
AgentState 활동은 기존 실제 LLM/도구/Host 대기 등을 계속 표시한다. Replan 사고를 추측하는 활동은 추가하지 않았다.
변경 사건 당시에는 실제 계획 도구의 tool_execution과 operation ID를 볼 수 있다.

ContextBuilder는 현재 Plan 한 개만 assistant 메시지로 삽입한다. Plan이 없으면 메시지를 넣지 않는다.
현재 항목·의존성·상태·근거·최근 실제 변경 이유가 포함된다. 별도 전체 revision 이력은 주입하지 않는다.
과거 plan 호출 인자나 명시적으로 읽은 과거 Plan은 일반 Conversation 기록으로 남으며 기존 압축 규칙을 따른다.
Summary·Memo·Host 지시의 역할/순서·출처는 유지하고 Plan을 Memo로 자동 복사하지 않는다.
압축 이후에도 현재 Plan은 Planning에서 다시 읽는다. Planning 자체가 압축이나 LLM 호출을 수행하지 않는다.

Host의 `send_instruction()`은 기존 command 적용/응답 폐기 경계를 유지한다.
새 지시만으로 계획을 고치지 않고, LLM이 새 지시와 기존 Plan을 읽어 계획 도구를 선택하도록 한다.

### Event와 Artifact

| Event | 의미 |
|---|---|
| `plan_created` | 최초 revision 1 적용. previous_revision/before_artifact_id는 null |
| `plan_updated` | 기존 항목의 진행·설명·상태 설명·근거 갱신 적용 |
| `plan_revised` | 구성·순서·의존성·접근 방법 수정 적용 |

각 payload는 task_id, plan_id, previous_revision, revision, change_kind, reason, source_refs,
before_artifact_id, after_artifact_id, llm_call_id, tool_execution_id, tool_call_id, request_artifact_id와
갱신 시점의 state/cursor를 포함한다. `plan_revision.data`는 전체 `PlanSnapshot.to_dict()`다.
Artifact.operation_id는 실제 도구 실행 ID이며 references에는 이전 revision·도구 요청·명시적 Artifact 근거가 연결된다.
요청 Artifact는 이를 선택한 LLM의 정규화 응답으로 연결되고 llm_call_id로 당시 공통/Adapter 요청·응답을 조회한다.
기존 공개 thinking/content/tool calls와 실제 Context 보존 경로를 바꾸지 않았다.

거부된 변경에는 도구 요청과 오류 결과가 남지만 성공한 계획 Event/Artifact는 만들지 않는다.
생성 이전의 state는 None, 이후 state는 당시 revision의 Artifact를 가리킨다.
모든 후속 상태 이벤트에도 현재 계획 Artifact를 명시적 참조로 연결한다.

## 5. Host 조회·결과·과거 계획 예시

```python
from shs_agent import TracerSnapshot

# 진행 중: state와 cursor를 먼저 읽고, 그 시점의 불변 Artifact를 조회한다.
state = await session.state()
if state.task.plan_artifact_id is not None:
    plan_at_cursor = (await session.artifact(state.task.plan_artifact_id)).data
    assert plan_at_cursor["revision"] == state.task.plan_revision

# 그 사이 새 revision이 생겨도 위 Artifact는 같은 과거 값을 유지한다.
async for event in session.events(after_sequence=state.last_event_sequence):
    if event.type in {"plan_created", "plan_updated", "plan_revised"}:
        plan_after_event = (await session.artifact(event.payload["after_artifact_id"])).data
        print(plan_after_event)

result = await session.result()
if result.plan is not None:
    print(result.plan.revision, result.plan.to_dict())

# messages/memos/session/provider 없이 result.tracer만 내보내고 다시 읽는다.
restored = TracerSnapshot.from_json(result.tracer.to_json())
for event in restored.events:
    if event.type == "plan_revised":
        before = restored.artifact(event.payload["before_artifact_id"]).data
        after = restored.artifact(event.payload["after_artifact_id"]).data
        at_event = restored.state(at_sequence=event.sequence)
        assert at_event.task.plan_artifact_id == event.payload["after_artifact_id"]
        print(event.payload["reason"], before, after)
```

동일 방식으로 실패·취소·한도 종료·Host 답변 시간 초과 결과의 당시 Plan을 읽을 수 있다.
`plan_read(revision=...)`는 LLM의 내부 조회 도구이며 Host를 위한 새 변경 API나 통신 계층은 만들지 않았다.

## 6. API·직렬화 호환성

- `TaskResult` 마지막에 `plan: PlanSnapshot | None = None` 추가. 기존 위치/키워드 생성은 유지한다.
- TaskState 마지막에 위 세 필드를 기본 None으로 추가. 기존 `SessionState.from_dict`는 새 필드가 없는 상태도 읽는다.
- ContextSnapshot과 TaskContext.snapshot의 선택적 Plan 전달을 추가했다. 기존 호출은 그대로 사용할 수 있다.
- `PlanSnapshot`, `PlanItem`, `PlanItemStatus`, `PlanError`를 `shs_agent`와 `shs_agent.agent`에서 export한다.
  조립용 `Planning`은 `shs_agent.agent.planning`에서 제공한다.
- Tracer schema_version은 2 유지. 기존 schema 1/2 읽기, complete/details_complete, Artifact 수집 실패 표시,
  Event/Artifact 명시적 참조 검증을 유지한다. 세 사건과 선택 State 필드는 추가적 데이터다.
  새 reader는 이전 자료를 읽는다. 새 State 필드를 모르는 구버전 reader의 순방향 읽기까지 보장하지 않는다.
- `complete`는 로그 종료, `details_complete`는 관찰 범위의 수집 완전성이다. 계획의 사실성·작업 검증 성공 여부가 아니다.
- Solver의 내부 도구는 5개에서 9개로 늘어난다. 새 이름들은 외부 Registry의 예약 이름 충돌 검사 대상이다.
  기본 시스템 안내와 스키마 길이가 늘어 모델 선택이나 기존 Context 크기 임계값 도달 시점은 달라질 수 있다.
- 새 실행 한도·권한·재시도·의존성 패키지를 도입하지 않았다. 기존 SQLAlchemy 의존성 선언도 그대로다.

## 7. 검증 결과와 미검증 범위

Windows, 저장소 `.venv`의 Python 3.12.9, unittest/Fake Provider/Fake Tool/Mock SDK를 사용했다.
최초 PATH의 Python은 3.10이었으며 src import가 설정되지 않아 실패했다. 이후 지원 버전인 저장소 환경을 사용했다.
제한된 실행 환경에서는 기존 asyncio 테스트가 첫 비동기 테스트에서 대기했으므로 중단했고,
자동 승인된 일반 실행 환경에서 수정 전·후 테스트를 실행했다. 아래 통과 수는 그 실제 실행 결과다.

| 검증 | 결과 |
|---|---|
| 수정 전 전체 테스트 | 91개 통과 |
| 수정 후 전체 테스트 | 116개 통과 (기존 91개 + 신규 25개) |
| `uv build --offline` | 기존 캐시로 sdist와 sdist 기반 wheel 생성 성공 |
| `tests/verify_distribution.py` | 모든 현재 소스·테스트와 배포물의 byte 일치, old/_deprecated·폐기 모듈 제외 확인 |
| wheel `-I -S` 격리 실행 | 116개 통과 |
| sdist `-I -S` 격리 실행 | 116개 통과 |
| 경량 import | 위 격리 검증에서 torch/ollama/openai/SQLAlchemy/보관 Agent 미로드 확인 |
| 기존 자료 보존 | 작업 전 기록한 `_deprecated`, 기존 보고서, 모든 아키텍처 문서 19개 파일의 SHA-256 동일 |

기존 테스트에서 바꾼 것은 새 도구 노출/예약 이름 집합뿐이다. 기존 실행·압축·상태·취소·상세 Tracer 검증은 유지했다.
신규 검증은 무계획 실행, 생성/조회/부분 갱신/재구성, revision/no-op, 거부 후 회복, 완료 결과·근거 보존,
명시적 재작업·제외 이력, 의존성 검증, Builder 불변성·압축 후 현재 Plan, 실시간 상태/cursor·사본 격리,
Task별 독립성, Host 지시 적용 경계, 전체 JSON 이력 재구성, 구독자 없는 종료, 계획 상태와 Task 상태 분리,
실패·취소·두 실행 한도·Host 답변 시간 초과 시 결과 보존을 다룬다.

대표 통합 테스트는 조사/수정/검증 계획 → Fake 조사 결과와 Memo → 수정 → 검증 도구의 환경 오류 →
다음 LLM 입력의 오류+현재 Plan 확인 → LLM의 plan_revise → 완료 결과 보존 및 정적 검토로 대체 →
새 Plan으로 다음 판단 → 최종 Plan/Tracer 반환을 확인한다.
정적 검토를 마쳤다는 항목 판단과 런타임 테스트 미실행을 구분하며, 미실행을 테스트 통과로 바꾸지 않는다.

초기 구현 시점에는 실제 Provider 검증을 수행하지 않았고, 이후 아래 실제 실행을 추가했다.
과거 2단계의 Ollama 검증을 이번 기능의 실제 모델 검증으로 재사용하지 않는다.
Python 3.11과 다른 OS는 이번 실행에서도 별도 확인하지 않았다.

### 7.1 후속 실제 로컬 Ollama 검증

2026-10-17 22:44~22:48 KST에 기존 `http://localhost:11434`의 설치 모델 두 개를 실제 호출했다.
`OllamaLLM → ollama.Client.chat` 경로를 사용했고 Fake/Mock Provider나 고정 LLM 응답은 사용하지 않았다.
검증 스크립트 외 엔진·Provider·도구 스키마·기본 시스템 프롬프트는 이 후속 작업에서 수정하지 않았다.

| 환경 | 실제 사용 값 |
|---|---|
| Python / Ollama SDK / 서버 | 3.12.9 / 0.6.2 / 0.32.15 |
| 모델 | `gemma4:e4b`, `qwen3.5:9b`, 모두 기존 설치본 Q4_K_M |
| 설치 모델 digest 앞 12자리 | Gemma `c6eb396dbd59`, Qwen `6488c96fa5fa` (전체 값은 metadata.json) |
| 공통 호출 옵션 | temperature=0, max_tokens=4096, think=True, num_ctx=16384, seed 미지정 |
| Task 한도 | max_steps=18, max_tool_calls=32, host_response_timeout=10초 |
| Context 압축 | 이번 짧은 흐름의 전체 입력 확인을 위해 Task 옵션에서 비활성화 |
| 실행 방식 | 기본 안내로 각 모델 1회, 계약 안내 보강 후 각 모델 1회. 각 묶음은 Gemma → Qwen 순차 실행 |

`num_ctx` 등은 이 검증의 개별 chat 요청 인자다. 서버 설정 파일·환경 변수·Modelfile·모델 등록 상태는 바꾸지 않았다.
로컬 `/api/tags`, `/api/version`을 읽고 설치 여부를 먼저 확인했으며, 모델 다운로드·서버 설치·재시작을 하지 않았다.
SDK Client에 version 메서드가 없는 사전 조회 오류는 `/api/version`의 읽기 요청으로 대체했다. 추론 실패와는 별개다.

#### 실제 모델과 통제된 도구의 경계

이번 검증은 **실제 모델 + 통제된 로컬 도구**의 통합 검증이다. 실제 프로젝트의 환경 장애를 진단한 실험은 아니다.
도구는 다음과 같이 구현했다.

1. `inspect_fixture`: 메모리의 `def add_numbers(a, b): return a - b`와 요구사항을 읽고 `fixture:source:v0`를 반환.
2. `apply_fixture_fix`: 메모리 문자열을 `return a + b`로 바꾸고 `fixture:source:v1`을 반환.
3. `run_fixture_tests`: 의도적으로 `FixtureRuntimeUnavailable`을 발생시켜 `fixture:runtime:missing`을 알림.
   실제 함수 실행이나 런타임 테스트는 하지 않는다.
4. `static_review_fixture`: 표준 라이브러리 `ast.parse/ast.dump`로 수정된 코드와 기대 AST를 실제 비교하고
   `fixture:static-review:v1`을 반환. 정적 구조 비교이며 함수 실행·프로젝트 테스트가 아니다.

LLM에는 research → edit → verify 계획을 먼저 만들고, 관찰 결과로 상태를 갱신하며,
실행 환경 오류 시 완료된 두 항목을 유지하고 verify를 review로 바꾸도록 요청했다.
마지막에 `plan_read(revision=1)`로 제외된 verify 항목을 읽고, `task_complete`의 JSON 출력에서
runtime_tests와 static_review를 구분하도록 했다. 실제 각 도구 선택과 인자 생성은 모델이 수행했다.
완성된 tool call 응답을 주입하거나 Runtime이 Plan을 대신 고치지 않았다.

#### 결과

모든 Task는 명시적 `task_complete`로 `completed`가 되었다. 이것과 검증 조건 충족은 별도다.
각 실행의 최종 확인 항목은 29개이며, 아래 수는 이 특정 흐름의 관찰 조건 수이지 일반 성능 점수나 성공 확률이 아니다.
기본 안내 결과를 본 뒤 보강 안내로 한 번씩만 추가 실행했고 실패 결과도 그대로 보존했다.

| 안내 / 모델 | LLM 판단 / 도구 호출 | 도구 오류 | 최종 revision | 확인 항목 | Event / Artifact | 관측 시간 |
|---|---:|---:|---:|---:|---:|---:|
| 기본 / Gemma | 13 / 13 | 4 (환경 오류 1 + PlanError 3) | 4 | 24/29 | 260 / 119 | 28.335초 |
| 기본 / Qwen | 11 / 11 | 1 (의도된 환경 오류) | 5 | 28/29 | 220 / 105 | 23.381초 |
| 보강 / Gemma | 11 / 11 | 1 (의도된 환경 오류) | 5 | 28/29 | 220 / 105 | 17.745초 |
| 보강 / Qwen | 11 / 11 | 1 (의도된 환경 오류) | 5 | **29/29** | 220 / 105 | 27.998초 |

기본 안내에는 도구 사용 순서·목표·결과 보존 요구가 포함됐다. 보강 안내에는 다음 계약을 명시적으로 추가했다:
새 항목의 description 필수, 완료 근거는 updates 내부 항목의 source_refs에 기록,
유지 항목은 item_id만 전달, 오류 참조를 정확히 복사, 정적 검토 후 review 상태/근거 갱신, 최초 plan_create 단독 호출.
원래 모델 결과를 수정하거나 엔진 검증을 느슨하게 바꾸지 않았다. 전체 실제 프롬프트는 각 metadata.json에 남는다.

**기본 Gemma의 한계:**

- 첫 두 plan_create에서 description 대신 status_note만 전달해 거부됐다. 세 번째 요청에서 스스로 수정하여 revision 1을 만들었다.
  Replan의 새 review 항목에서도 같은 누락으로 한 번 거부된 뒤 수정했다. 거부는 Task 종료를 유발하지 않았고
  그때까지 Plan/revision을 유지했다. 이는 자동 재시도 정책이 아닌, 오류를 읽은 LLM의 후속 도구 호출이다.
- 조사·수정의 완료 상태와 status_note는 유지했지만, 근거를 개별 항목이 아닌 revision의 source_refs에만 기록했다.
  그 근거는 과거 revision에 남아 있으며 엔진이 삭제한 것은 아니다. 최신 Plan 항목 자체에는 연결되지 않았다.
- 변경 원인의 참조로 실제 호출 ID가 아닌 `tool:run_fixture_tests:1`을 작성했다.
  기존 `tool:` 문자열은 의미 검증 대상이 아니므로 수락되었다. 원래 오류·당시 LLM 입력·실제 호출 ID는 Tracer에 별도로 남는다.
- 정적 검토 도구는 실제 실행했지만 review의 완료 상태/근거를 갱신하지 않고 종료했다. 최종 Plan에서 review는 pending이다.
  계획 완료를 Task 완료의 강제 조건으로 만들지 않는 기존 계약이 그대로 드러난 사례다.

**기본 Qwen의 한계:**

- 조사·수정 완료 근거, 오류 원인, 5개 revision, 과거 Plan 조회와 최종 상태/근거 연결을 수행했다.
- 다만 “먼저 계획 생성” 요청과 달리 inspect_fixture를 먼저 호출하고 다음 판단에서 plan_create를 실행했다.
  엔진상 허용되는 선택적 Planning이지만 이번 시나리오의 지정 순서는 충족하지 못했다.
- 최종 설명에 “addition of required fields”라는 부정확한 구절이 포함됐다. 실제 도구가 비교한 것은
  a-b → a+b 함수 AST다. 구조화된 runtime_tests/static_review 값은 정확했지만 자연어 설명 전체의 정확성을 보장하지 않는다.

**보강 안내 결과:**

- Gemma는 필수 필드 누락 없이 계획을 생성·수정했고, 완료 항목과 근거를 유지하고 정적 검토 결과도 revision 5에 반영했다.
  그러나 Replan의 source_refs를 정확한 `fixture:runtime:missing` 대신 `runtime:missing`으로 기록했다.
  reason과 최종 limitations에는 정확한 원인 문자열이 있다. 의미를 추정할 수 있어도 정확한 참조 연결 검사에는 실패로 남겼다.
- Qwen은 이번에 계획 생성 순서까지 지켰고 29개 확인 항목을 모두 충족했다.
  이는 한 번의 보강 안내 실행 결과이며 다른 작업·프롬프트에서도 같은 결과를 보장한다는 뜻은 아니다.

네 실행 모두 최종 JSON의 공통 값은 다음과 같다. limitations에는 런타임 미검증을 명시했다.

```json
{"runtime_tests": "not_run", "static_review": "passed"}
```

이 값은 의도적으로 실패시킨 fixture의 런타임 테스트 상태다.
shs-agent의 별도 오프라인 unittest 116개는 후속 작업에서도 다시 실행하여 통과했다.

#### 엔진과 기록 경로에서 확인한 것

- 계획 도구 네 개가 모두 실제 Registry/Executor 경로에서 실행됐다.
- Replan을 선택한 LLM 요청에는 실제 오류 도구 결과와 당시 Plan이 함께 있었다.
  Replan 이후 일반 판단에는 새 Plan이 들어갔다. 이전 완료 항목의 값은 네 실행 모두 유지됐다.
- plan_read로 revision 1의 verify 항목을 읽었으며, 최종 Result.plan과 최종 Plan Artifact가 같았다.
- 실시간 이벤트와 최종 Tracer Event가 같았고, 이벤트 수신 시 참조 Artifact를 조회할 수 있었다.
  실시간 state/cursor 샘플과 사후 재구성이 같았으며 모든 이벤트 시점의 계획 참조가 해당 revision을 가리켰다.
- LLM 호출마다 common_request / adapter_request / adapter_response / common_response / normalized_response가 보존됐다.
  공개 thinking, content, tool calls, 요청 당시 Context, 도구 요청·결과 및 변경 전후 참조를 확인했다.
- 네 Tracer 모두 `complete=True`, `details_complete=True`, JSON 내보내기/재읽기 일치.
  이 완전성은 기록 수집에 관한 것이며 Gemma의 누락·잘못된 참조가 정확해졌다는 뜻은 아니다.

최초 자동 점검은 25개 조건이었으며 Qwen의 순서 이탈을 검사하지 못했다.
원본을 읽고 순서·완료 항목 자체 보존·실제 도구 근거를 읽은 갱신 등을 추가하여 최종 29개로 보완했다.
위 표의 기본 실행 결과는 저장된 원본을 최종 기준으로 재점검한 `audit_final_checks.json` 기준이다.
최초 summary.json은 변경하지 않았으므로 기본 Qwen의 최초 accepted=true와 최종 재점검의 false를 구분해야 한다.
재점검은 추가 LLM 호출 없이 수행했고 실시간 검사값은 최초 실행에서 수집한 결과를 유지했다.
보강 실행은 처음부터 29개 조건으로 검사했다. 두 실행 묶음의 프로세스 종료 코드는 Gemma의 미충족 조건 때문에 모두 1이다.

호출별 사용량도 원본 summary에 보관한다. 네 실행의 가장 큰 prompt_eval_count는 6,129로 요청 num_ctx 16,384보다 작았고,
응답 길이 한도·Task 실행 한도·Host 답변 대기 종료는 발생하지 않았다. 전체 공개 thinking 문자 수는
기본 Gemma/Qwen 6,436/2,916, 보강 Gemma/Qwen 4,676/6,395다.
측정 시간은 모델 로딩·캐시·도구·기록 처리 영향을 포함하므로 속도 비교 결과로 해석하지 않는다.

#### 재실행과 근거 파일

```powershell
$env:PYTHONPATH = 'src'
& '.\.venv\Scripts\python.exe' tests/verify_ollama_planning.py
& '.\.venv\Scripts\python.exe' tests/verify_ollama_planning.py --contract-reminders
```

기본값은 두 모델 순차 실행이다. `--model gemma4:e4b` 또는 `--model qwen3.5:9b`로 하나만 선택할 수 있다.
각 실행은 새 UTC 시각 디렉터리를 만들고 이미 있는 output-dir을 덮어쓰지 않는다.
스크립트는 unittest discovery에서 실행되지 않으며 자동 모델 다운로드나 실패 시 자동 전체 재실행을 하지 않는다.

- 기본 실행: [`build/ollama_planning/20261017T134446Z/`](../../../build/ollama_planning/20261017T134446Z/)
- 보강 실행: [`build/ollama_planning/20261017T134713Z/`](../../../build/ollama_planning/20261017T134713Z/)
- 각 디렉터리: metadata.json, summary.json, 모델별 `*.trace.json`, `*.result.json`, `*.summary.json`.
  기본 실행에는 최종 기준 재점검 `audit_final_checks.json`도 있다.
- result에는 실제 최종 출력·Plan·fixture 도구 동작이, trace에는 LLM 요청/응답·모든 계획 이력이 있다.
  파일들은 기존 정책대로 Git에서 제외되는 build 아래에 보존한다.

#### 검증의 범위와 남은 한계

이 실험은 안내된 작은 시나리오에서 실제 모델이 계획 도구를 사용할 수 있는지 확인한다.
Planning을 자율적으로 언제 선택하는지, 현실의 여러 환경 실패를 어떻게 분류하는지, 대규모 코드 수정의 정확도는 측정하지 않았다.
수정 도구가 준비된 메모리 변환을 수행하므로 모델의 일반적인 코드 작성 능력을 검증한 것도 아니다.
각 안내에서 모델별 1회 실행이므로 성공률·안정성·성능 순위를 추정할 표본이 없다.
temperature=0도 다른 서버 버전·모델 digest·실행 환경에서의 재현을 보장하지 않는다.

기본 스키마와 설명이 있어도 필수 필드·기록 위치·참조 문자열·순서 준수는 모델마다 달랐다.
이번 결과를 근거로 Runtime이 누락을 자동 보정하거나 완료를 강제하는 정책은 추가하지 않았다.
잘못된 `artifact:` ID를 거부하는 기존 검증과 달리, 일반 `fixture:`/`tool:` 문자열의 의미적 정확성은 보장되지 않는다.
실제 파일·프로세스 부작용·서버 장애·취소 중 Replan·Host 지시 변경·장기 압축 흐름의 실제 모델 검증은 이번 범위 밖이다.
관련 엔진 계약은 기존 Fake 기반 테스트 결과와 구분해서 해석해야 한다.

## 8. 남은 경계

계획·Tracer는 인메모리다. 이력이 길어지면 메모리와 요청 크기가 늘어나며 TTL·삭제·자동 저장·Resume를 추가하지 않았다.
Plan은 항목의 자연어 의미·완료 근거를 독립적으로 검증하지 않는다. 새 ID 사용과 유효한 결과 유지의 의미적 판단은 LLM에 있다.
현재 Plan 전체는 Builder에 들어가므로 큰 계획이 Context 비용을 늘릴 수 있다. 새로운 임의 절단/계획 한도를 만들지 않았다.

후속 자식 Task도 Solver의 Task별 조립에서 같은 Planning을 사용할 수 있다.
공유 실행 트리·Sub-agent 생성/병렬/재귀·통신·권한 부여·Storage·Host Memory·UI는 이 단계에 포함하지 않았다.
이번 코드는 부모 완료/자식 취소 등 미확정 정책을 정하지 않는다.
과거 보고서·아키텍처·보관 Agent를 수정하지 않았으며 commit/push하지 않았다.
