# 6단계: 재귀 Sub-agent와 Root 공통 AgentLimits

작성일: 2026-10-18 (Asia/Seoul).

기존 TaskSession / TaskRuntime / AgentCore 조립 경로를 재사용하여 모든 깊이에서 생성·통신·종료·회수가
동작하도록 확장했다. Root마다 Manager 하나와 누적 예산 하나를 사용한다. 수정 전 **167개**, 수정 후
**199개** unittest가 통과했다. wheel/sdist 격리 환경에서도 각각 **199개**가 통과했다.

기준 문서는 [목표 아키텍처](../../architecture/SHS_AGENT_TARGET_ARCHITECTURE.md),
[4단계 보고서](../phase_4_subagents/phase_4_subagents.md),
[5단계 보고서](../phase_5_communication/phase_5_communication.md)와 현재 소스다.
5단계 실제 Ollama 보고서의 모델 응답 오류는 이번 범위에서 수정하거나 재검증하지 않았다.

## 1. 변경 파일과 모듈 책임

경로의 `agent/`는 `src/shs_agent/agent/`를 뜻한다. 기존 작업 전체와 이번 변경을 구분한 목록이다.

| 파일 | 이번 변경 |
| --- | --- |
| `agent/subagent/manager.py` | Root 전체 등록표·예산, 직접 관계 검증, 호출 Task별 기록·대기, 재귀 회수, 전체 자손 조회 |
| `agent/subagent/scope.py` (신규) | 호출 Task가 고정된 관리 인터페이스. Host의 전체 트리 권한을 LLM 도구에 전달하지 않음 |
| `agent/subagent/models.py` | Host의 하위 설정 허용 목록, Tree schema 2와 schema 1 읽기, 재귀 관계·참조·종료 검증 |
| `agent/solver.py` | 기존 공통 조립 함수에 같은 Manager의 scope 연결, 실제 직계 부모 실행 설정 상속 |
| `agent/communication/endpoint.py`, `__init__.py` | 중간 Agent의 부모/자식 양쪽 통신 역할 결합 |
| `agent/tools/session_tools.py` | 권한·깊이에 맞는 도구 등록, LLM에 호출자 ID를 받지 않는 scope 호출 |
| `agent/session/channel.py` | 깊이 2 이상 관계 초기화, 답변 대상이 실제 부모인지 자식인지에 따른 활동 상태 |
| `agent/session/handle.py` | Root의 공개 Manager 설명 갱신. 기존 Session API 유지 |
| `agent/runtime/runner.py` | Task별 scope로 완료 정리, Root 결과에만 전체 Tree 연결 |
| `agent/types.py` | 깊이 0/1 고정 제한 제거. Host 지정 비음수 정수 검증 유지 |
| `tests/test_recursive_subagent.py` (신규) | 재귀 실행·통신·한도·권한·회수·아카이브 32개 테스트 |
| `tests/test_subagent.py` | 이제 유효한 깊이 2를 오류 사례에서 제외, 자식도 자신의 직접 자식 목록을 조회할 수 있게 기대값 갱신 |
| `examples/recursive_subagents.py` (신규) | 서버 없는 Depth 3 실행, Live/최종 Tree와 실제 도구 스키마 내보내기 |
| `README.md`, `MANIFEST.in` | 사용 계약 안내와 새 예제의 sdist 포함 |
| 이 보고서와 `.schema.json` 5개 | 구현 계약과 실제 Provider에 제공한 내부 도구 스키마 |

Planning, ContextBuilder, Conversation/Memo, Core, Provider, Tracer의 실행·원본 수집 경로는 재사용한다.
새 실행 루프, 메시지 버스, 스케줄러, 런타임 의존성은 추가하지 않았다.

## 2. Root 하나에 Manager 하나

```text
Solver.create_task → Root Channel + SubagentManager 1개
  → _assemble_task → Context / Builder / Planning / Core / Tools / Runtime / Session

각 Task의 subagent_spawn
  → 호출 Task에 고정된 SubagentScope
  → 같은 Root Manager: 관계·권한·전체 한도 검증
  → Solver에서 주입한 create_child → 같은 _assemble_task
  → 자식 Channel / 독립 실행 상태 + 같은 Manager의 새 scope
  → 등록·집계 → Session.start → 기존 Runtime 실행
```

Manager는 Root Channel과 모든 자손의 Session/Channel, 실행 관찰 핸들, 회수 결과, 승인 설정과
Task별 허용 ID, 종료 정리 여부를 보관한다. TaskState/AgentState 사본을 상태 저장소로 중복 유지하지 않는다.
관계는 각 Channel의 `task_id`, `parent_task_id`, `root_task_id`, `depth`에서 읽는다.
Solver의 설정 표는 Provider·옵션·개별 한도의 상속 자료이며 실행 상태가 아니다.

Runtime은 Solver를 import하지 않는다. 생성 callback은 처음 Root를 조립할 때 주입한다.
Manager 전체를 Agent의 SessionTools에 주지 않고 `SubagentScope`를 연결한다. scope의 spawn/status/result/
wait/cancel은 실제 Task ID를 내부에 고정한다. 전체 자손 열람·Session 접근은 Host의 Root Manager API다.
통신 endpoint도 실제 호출자에 바인딩되며 `subagent_ask/send`는 자신의 직접 자식만 받는다.
부모와는 `parent_ask/notify`로 통신한다. 형제·조부모·다른 Root를 직접 제어하거나 통신할 수 없다.

Root Session의 `subagents`는 Manager이며, 자손 Session의 `subagents`는 이전처럼 `None`이다.
자손의 생성 권한은 내부 scope에 연결되어 있다. Host는 Root Manager의 `child(task_id)`로 모든 깊이의
Session을 얻는다. Python 자체의 sandbox나 Host 외부 도구 격리를 새로 제공하는 구조는 아니다.

## 3. 공통 예산과 Host 승인 설정 상속

```python
from shs_agent import AgentLimits, ProblemSolver, SubagentConfig
from shs_agent.tool import ToolRegistry, ToolExecutor

research_tools = ToolRegistry()  # Host가 허용한 외부 도구 등록
leaf_tools = ToolRegistry()      # 부모 도구의 자동 복사 없음
configs = {
    "research": SubagentConfig(
        registry=research_tools, executor=ToolExecutor(research_tools),
        allowed_subagent_config_ids=("leaf",),
    ),
    "leaf": SubagentConfig(registry=leaf_tools, executor=ToolExecutor(leaf_tools)),
}
root = await ProblemSolver(provider).start(
    "조사를 나누고 결과를 통합해줘.", model=model,
    limits=AgentLimits(max_total_subagents=10, max_subagent_depth=3),
    subagents=configs,
)
```

`allowed_subagent_config_ids`가 최소 추가 인터페이스다. 기본값 `()`는 추가 생성 권한 없음이다.
Host가 등록한 ID만 참조할 수 있고, 중복·빈 ID·문자열 단독·미등록 ID는 거부한다.
입력 list도 불변 tuple로 복사한다. 자기 설정을 다시 허용하는 순환 프로필은 가능하며 실제 실행은
Root 한도로 제한된다. Root는 Host catalog 전체에서 선택하지만 각 자손에게는 해당 목록만 enum으로 보인다.

| 설정/동작 | 계약 |
| --- | --- |
| `max_total_subagents` | Root 제외 전체 자손의 누적 등록 수. 완료/실패/취소 후 차감 없음 |
| `max_subagent_depth` | Root=0, 생성할 자식 깊이=부모 깊이+1. Host가 지정한 비음수 정수까지 허용 |
| 한도 미지정 | Sub-agent 활성화 시 공통 한도 두 값 모두 필요. `None`을 무제한으로 해석하지 않음 |
| 등록 전 실패 | 도구/설정/조립/관계 검증 실패는 집계하지 않음 |
| 등록 후 실패 | 실행 예약 실패를 포함해 집계 유지, 실제 실패 결과·Tracer 회수 |
| 동시 요청 | 단일 event loop에서 한도 검사→조립→관계 기록→등록 사이에 await 없음. 모든 부모가 같은 표를 갱신 |
| 개별 `max_steps/max_tool_calls` | 해당 Task에만 적용. Root 공통 예산과 분리 |
| Provider/모델/개별 한도 | 생략 시 **직계 부모** 값 사용. 모델은 생성 시 부모 Channel의 현재 모델 |
| 옵션 | 생략 시 직계 부모 옵션 사본. 명시적 `None`은 상속 없이 None 적용 |
| 외부 도구 | 각 설정의 Registry/Executor 필수. 상위 도구 자동 상속 없음 |
| 자식 설정의 Root 한도 | 값을 넣으면 생성 전 거부. 상속할 때도 Root 한도 필드를 제거 |

설정의 옵션·ID 목록은 Host 승인 시 고정한다. 승인 후 Registry 스키마 변경은 기존처럼 새 생성에서
거부한다. LLM spawn 인자에는 Provider 객체, Python 코드, 인증정보, 한도 변경, 추가 권한을 받지 않는다.
생성 상한 오류는 `SubagentError` 도구 결과로 돌아가며 호출 Agent가 직접 수행·Replan할 수 있다.
이미 상한을 썼다고 전체 Task를 자동 실패시키지 않는다. 새 동시 실행 슬롯 한도는 없다.

## 4. 깊이별 도구와 재귀 통신

실제 예제의 첫 Provider 요청에서 외부 `leaf_work`만 제외하여 내보냈다.

| 대상 | 내부 도구 수 | 실제 스키마 |
| --- | --- | --- |
| Root, 깊이 여유 있음 | 18 | [root_internal_tools.schema.json](root_internal_tools.schema.json) |
| Depth 1 A, 위임 허용 | 19 | [depth_1_internal_tools.schema.json](depth_1_internal_tools.schema.json) |
| Depth 2 A-1, 위임 허용 | 19 | [depth_2_internal_tools.schema.json](depth_2_internal_tools.schema.json) |
| 최대 Depth 3 A-1-1 | 12 | [depth_3_internal_tools.schema.json](depth_3_internal_tools.schema.json) |
| Depth 2 A-2, 위임 미허용 | 12 | [nondelegating_internal_tools.schema.json](nondelegating_internal_tools.schema.json) |

중간 Agent는 기존 Task 도구 8개(host_ask 제외), 부모 통신 `parent_ask/notify`, 공통
`agent_reply/wait_reply`, 자식 관리 `subagent_spawn/status/result/wait/cancel`, 자식 통신
`subagent_ask/send`를 함께 사용한다. 최대 깊이 또는 위임 권한 없음이면 자식 관련 7개를 제공하지 않는다.
최대 깊이에서도 자신의 LLM/도구 작업과 부모 통신은 유지된다. Root의 최대 깊이가 0이면 자식 관련
도구를 숨긴다. 숨긴 이름을 호출하면 기존 `unknown_tool` 오류이며 외부 도구의 예약 이름 등록도 거부한다.

예제의 A는 `A-1`, `A-2`만, A-1은 `A-1-1`만 선택한다. A-1-1 설정에는 재위임 허용 ID가 있지만
Root 깊이 한도에 의해 spawn이 숨겨져 있음을 함께 검증했다. 모든 설정을 자손에게 자동 공개하지 않는다.

```text
A-1 → parent_ask(q1) → A
A   → parent_ask(q2, related_request_id=q1) → Root
Root → agent_reply(q2) → A
A    → agent_reply(q1) → A-1 → 원래 작업 계속
```

질문 ID는 각 요청마다 독립적이며 `related_request_id`는 송신 Task가 실제로 알고 있는 요청만 연결한다.
Root→A→A-1 하향 질문도 같은 경로로 처리한다. 수신 대기열·안전한 적용 경계·원본 Conversation·
ContextBuilder는 기존 5단계 구현을 사용한다. 수신 내용은 user 작업 데이터, 송신 기록은 assistant이며
system 지시로 승격하지 않는다. 다른 Task의 Conversation을 자동 합치지 않는다.

`subagent_wait`의 현재 대기 future는 Task ID별로 분리했다. Root와 중간 부모가 동시에 기다려도 서로
덮어쓰지 않는다. 새 질문은 대기를 해제하지만 원래 요청/자식은 유지한다. 중간 Agent가 자신의 부모
답변을 기다릴 때는 `waiting_for_parent_reply`, 자기 자식 답변을 기다릴 때는 `waiting_for_child_reply`다.
단순히 depth>0인지로 활동을 정하지 않는다. 기존 host_ask의 질문 보존·협력적 해제 규칙도 유지한다.

## 5. 재귀 완료·취소·회수

정상 `task_complete`는 **자신의 직접 자식** 중 실제 종료하지 않은 Task가 있으면 거부한다.
다른 가지의 실행 중인 Task는 해당 부모의 완료를 막지 않는다. 자식 종료는 부모 Plan을 자동 갱신하지 않는다.

```text
Root 비정상 종료
  → Root의 직접 자식 취소 접수
    → 각 자식의 기존 Runtime이 자신의 종료 경계에 도달
      → 그 자식의 직접 자식 취소·join
        → … 각 외부 작업의 기존 취소 경계까지 대기
      ← 실제 결과·Tracer 회수, 부모 Tracer에 subagent_terminated 기록
  ← 모든 직접 자식 회수
Root 최종 상태/finished 기록 → 전체 TaskTreeSnapshot 반환
```

FAILED, CANCELLED, LIMIT_REACHED, INCOMPLETE 모두 같은 계약을 적용한다.
중간 부모가 실패한 경우에도 자신의 하위 트리를 정리하며 Root는 그 실패 결과를 검토하고 정상 완료할 수 있다.
Manager는 종료 정리를 시작한 Task를 표시하여 그 Task 또는 그 자손의 늦은 추가 생성을 거부한다.
종료 상태를 중복 저장하는 것이 아니라 생성과 회수 경계를 보호하는 표시다.

취소 접수는 종료가 아니다. 진행 중 외부 도구는 기존 정책대로 기다리고, 실제 종료 이후에만 회수 결과를
등록한다. 완료한 자손의 결과·Tracer는 보존한다. 다른 Root에는 전파하지 않는다.
강제 종료 시간, 자동 재시작, 프로세스 강제 종료, TTL, 분리 실행은 추가하지 않았다.

## 6. Host의 실시간 전체 트리 관찰

```python
manager = root.subagents
direct = await manager.children()                  # 기존 API: Root 직접 자식
all_descendants = await manager.descendants()       # 신규: 전체 깊이
for item in all_descendants:
    session = manager.child(item["task_id"])         # 모든 자손 Session을 공개 API로 얻음
    state = await session.state()
    print(state.task.parent_task_id, state.task.root_task_id, state.task.depth)
    print(state.task.status, state.agent.activity, state.agent.waiting_request_id)
    print(state.agent.incoming_request_ids, state.agent.outgoing_request_ids)
    if state.agent.operation_artifact_id:
        detail = await session.artifact(state.agent.operation_artifact_id)
    if state.task.plan_artifact_id:
        plan = await session.artifact(state.task.plan_artifact_id)
    own_children = await manager.children(session.task_id)
    # 별도 관찰 coroutine에서 이어받기:
    # async for event in session.events(after_sequence=state.last_event_sequence): ...
    # session.send_instruction / answer / cancel / result / trace는 기존 API 그대로

live_tree = await manager.snapshot()                # 완료를 기다리지 않는 현재 기록
root_result = await root.result()                   # 실제 최종 결과 및 전체 트리
final_tree = await manager.collect()                # 현재 등록된 자손 join 후 사본
```

상태와 cursor는 각 Channel에서 읽는다. 서로 다른 Task의 sequence를 하나의 전역 순서처럼 비교하지 않는다.
`descendants()`는 작은 상태 목록이고 상세 이력은 필요한 Session에서 조회한다.
`snapshot()`은 await 없이 모든 Channel 기록을 복사하므로 등록이나 메시지 링크의 절반만 담지 않는다.
`collect()`는 호출 당시 등록된 실행을 기다리며 그 부모들의 재귀 정리도 기다린다. 실행 중 Root가 나중에
새 가지를 생성할 수 있으므로 최종 자료가 필요하면 먼저 `root.result()`를 기다린다.

## 7. Tracer와 TaskTreeSnapshot 사후 재구성

Tree는 평평한 `traces[task_id]` 표와 각 TaskState의 부모 관계로 트리를 표현한다.
각 Task의 원본 Tracer는 한 번씩 들어간다. 자식 결과/부모 이벤트에 하위 Tree 전체를 반복 복사하지 않는다.
각 자손의 독립 TaskResult에는 자기 tracer가 있으며 전체 tree는 Root TaskResult에만 붙는다.

```json
{
  "schema_version": 2,
  "scope": "root_and_descendants",
  "root_task_id": "root-id",
  "complete": true,
  "details_complete": true,
  "traces": {"root-id": {}, "A-id": {}, "A-1-id": {}}
}
```

위 `{}`는 설명용 생략이며 실제 파일은 각 Task의 Event/Artifact 전체를 담는다.
개별 Tracer schema 2는 변경하지 않았다. 기존 Tree schema 1 / `root_and_direct_children`은 읽을 수 있고,
재저장하면 Tree schema 2가 된다. schema 1 파일에 깊이 2 이상을 섞으면 거부한다. 실제 기존
`build/communication_demo/task_tree.json`(schema 1, Task 3개) 읽기와 완전성도 확인했다.

관계 검증은 부모 존재·정확한 깊이 차·동일 Root·부모의 단일 등록 사건·누락 없는 자손 집합을 확인한다.
순환/고아/중복 등록, 다른 Root, 잘못된 생성 관계를 거부한다. 모든 깊이의 `subagent_*`, `agent_*`
Artifact에 있는 `{task_id, artifact_id}` 참조를 해결하며 기존 동일 Task 참조 검증을 유지한다.
complete를 주장하는 개별 trace에는 실제 terminal 상태와 마지막 finished 이벤트가 있어야 한다.

```python
from pathlib import Path
from shs_agent import TaskTreeSnapshot

Path("tree.json").write_text(root_result.tree.to_json(indent=2), encoding="utf-8")
# 이 시점 이후 Session/Manager/Provider가 없어도 된다.
tree = TaskTreeSnapshot.from_json(Path("tree.json").read_text(encoding="utf-8"))
for task_id, trace in tree.traces.items():
    state = trace.state()
    print(task_id, state.task.parent_task_id, state.task.depth, tree.result(task_id))
    for artifact in trace.artifacts.values():
        if artifact.kind.startswith(("subagent_", "agent_")):
            for ref in artifact.data.get("task_refs", []):
                original = tree.artifact(ref)
```

생성 요청·선택 설정·부모 Plan 참조·결과 전달·취소·회수는 실제 부모의 Tracer에 기록한다.
자식의 `subagent_configuration`에는 적용 설정/개별 한도/Root 한도/승인 도구/허용 하위 ID가 남고,
`subagent_origin`은 실제 부모의 생성 Artifact를 참조한다. `subagent_bound`는 정확한 Root·깊이를 기록한다.
기존 LLM 요청/응답·제공된 thinking/content/tool calls·도구 인자/원본 결과·계획·상태 이력은 그대로 수집한다.
Fake 실행을 실제 SDK 관측이나 모델의 비공개 추론 기록으로 표현하지 않는다.

Tree `complete`는 모든 Task 로그의 실제 종료 여부이며 성공 여부가 아니다. 취소/실패로 회수된 Tree도
complete=true일 수 있다. `details_complete`는 기존 각 Tracer의 상세 수집 완전성을 결합한다.
진행 중 snapshot은 미종료 trace를 포함하므로 complete=false다. JSON의 배열과 Python 원본 tuple 차이는
JSON 의미 기준으로 비교하며, 상태 재구성은 기존 SessionState 타입을 복원한다.

## 8. 검증 결과와 재현

Python 3.12.9 / 기존 `.venv` 사용. 기본 `python`은 3.10이고 패키지 경로가 없어 처음 테스트 로드에 실패했다.
가상환경에서도 Windows 샌드박스의 asyncio 초기화가 정체되어 정상 실행 권한으로 기준 테스트를 확인했다.
정체된 본 작업의 테스트 프로세스만 정리했다. uv도 샌드박스 캐시 쓰기가 거부되어 정상 권한의 기존 캐시로
offline 빌드했다. 제품에 환경 우회나 timeout을 추가하지 않았다.

| 검증 | 결과 |
| --- | --- |
| 수정 전 전체 unittest | **167/167**, 13.639초 |
| 신규 재귀 unittest | **32/32**, 9.382초 |
| 변경 후 전체 unittest | **199/199**, 21.792초 |
| wheel / sdist | `uv build --offline` 성공, `dist/`에 생성 |
| wheel 격리 suite | **199/199**, 21.253초 |
| sdist 격리 suite | **199/199**, 20.689초 |
| 배포 내용 | 현재 Python 소스·테스트와 byte 단위 일치, old/_deprecated 제외 |
| 경량 import | `-I -S` 성공, torch/ollama/openai/sqlalchemy/보관용 Agent 미로드 |
| 독립 재귀 예제 | Root + 자손 6개, 모두 completed, 모두 도구 오류 0 |
| Tree 저장/복원 | Live false / 최종 complete·details_complete true, 모든 정규 Task/Artifact 참조 해결 |
| 실제 이전 Tree 읽기 | 5단계 schema 1 파일의 3개 Task 복원 성공 |
| `git diff --check` | 통과, 기존 LF/CRLF 안내만 출력 |

신규 테스트는 Root→A/B→A-1/A-2/B-1→A-1-1 실행·결과 통합, 독립 Memo/Plan/Conversation/State,
실시간 Session·호출 Artifact·cursor·이벤트 관찰, 누적 생성 수와 동시 요청, 깊이, 설정/도구 위임,
직계 부모 상속, 등록 전/후 실패, 두 Root 독립, 직접 자식 완료 검사, 중간 부모의 조회/취소/대기/회수,
상·하향 통신과 관련 요청 ID, 원래 질문 대기 중 추가 질문 처리, 부모 지시의 대상 한정 적용을 포함한다.
Root 및 중간 부모 각각 FAILED/CANCELLED/LIMIT_REACHED/INCOMPLETE의 깊이 3 정리, 개별 tool call 한도,
이미 완료한 손자 기록 보존, 잘못된 Tree·참조 거부와 schema 1 호환도 확인한다.
기존 167개 회귀에는 단일 Task/Sub-agent 비활성/Depth 1/Host 질문·통신/Planning/Provider 관측이 포함된다.

동시성은 `asyncio.Event`, 도구/LLM gate, 실제 이벤트 관측으로 순서를 정했다.
테스트 하네스의 watchdog은 제품 실행 정책이 아니다. 개발 중 발견한 테스트의 Root 한도 상속 설정,
송신 메시지 역할 및 JSON tuple/list 비교 단언을 수정한 후 위 검증을 통과했다.

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe examples/recursive_subagents.py --schema-dir docs/reports/phase_6_recursive_subagents
uv build --offline
.\.venv\Scripts\python.exe tests/verify_distribution.py
```

예제 결과: [summary.json](../../../build/recursive_subagents_demo/summary.json),
[live_tree.json](../../../build/recursive_subagents_demo/live_tree.json),
[task_tree.json](../../../build/recursive_subagents_demo/task_tree.json).
최종 출력은 `Root(A(A-1(A-1-1),A-2),B(B-1))`다. 예제는 명시된 출력 경로를 재실행 시 갱신한다.

## 9. 남은 제약과 보존

단일 asyncio event loop 안의 인메모리 트리다. Host가 공유한 Provider/외부 도구의 동시 사용 적합성,
실제 파일·프로세스 격리는 Host 책임이다. 상세 이력과 Session은 메모리에 보관하며 자동 삭제·영속화·Resume는 없다.
협력적 취소이므로 끝나지 않는 외부 도구가 있으면 최종 회수도 계속 기다린다. 의미적 순환 질문, 모델의
잘못된 도구 선택·답변 품질은 엔진이 자동 해결하지 않는다.

Ollama 실추론은 이번 작업에서 수행하지 않았다. Fake 통과를 실제 모델 품질이나 서버 병렬 추론 검증으로
주장하지 않는다. 동적 권한 추가, 새 스케줄러, Host 장기 Memory/MindMap, UI, 추론 서버 최적화는 구현하지 않았다.
기존 미커밋 변경·과거 보고서·old/_deprecated 보관본을 유지했으며 commit/push하지 않았다.
**6단계 구현과 검증을 완료하고 이 범위에서 멈춘다.**
