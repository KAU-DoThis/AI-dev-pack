# shs-agent

외부 LLM과 Host 도구를 이용하는 경량 Python 문제해결 라이브러리. Python 3.11 이상을 지원하며,
모델 추론·작업 환경·권한·UI는 Host와 외부 서버가 담당합니다.

v1.1 후속 구현은 선택적 Workspace, 공통 ParentConnection, 부모의 자원 위임,
직계 부모 Plan 조회, context 초과 복구, 트리 이벤트 구독을 제공합니다.
기본 설치에는 필수 외부 의존성이 없으며 SQL Storage는 `pip install "shs-agent[storage]"`로 선택합니다.
설계와 검증 기록은 [v1.1 구현 보고서](docs/dev/reports/architecture_v1_1.md)를 참고하세요.

```python
from shs_agent import Workspace

# 이미 존재하는 프로젝트 경로. 라이브러리는 기본 폴더를 자동 생성하지 않습니다.
workspace = Workspace(project_path, delegable=True)
session = await solver.start("프로젝트를 수정하고 테스트해줘.", model=model,
                             workspace=workspace, registry=host_command_tools)
async for item in session.events(scope="tree"):
    print(item["tree_sequence"], item["event"].task_id, item["event"].type)
```

파일 도구 6개는 Workspace를 받으면 연결됩니다. 명령 실행 도구와 프로세스 격리는 Host가 제공합니다.
부모는 `workspace_paths`, `tool_ids`로 자식에게 필요한 범위만 주며, 직접 사용할 수 있는 권한과
다시 위임할 권한은 구분됩니다. Root도 `parent_ask`로 Host에 질문하고 `agent_wait_reply`로 따로 기다립니다.
실제 코딩 예제: `python examples/workspace_coding_host.py --workdir build/coding --model qwen3.5:9b`.

Python 3.11+ 환경에서 설치하고 오프라인 전체 실행을 확인할 수 있습니다.

```text
python -m pip install -e .
python examples/full_workflow_host.py --review-memory
python examples/replay_task_tree.py build/full_workflow_demo/task_tree.json
```

Fake Provider가 실제 LLM↔Tool 경로로 Root → A → A-1과 B를 실행합니다. 계획·질문·도구 거부·승인·
실제 handler 조회·결과 회수를 거쳐 3건/2,500원을 계산하고, Host가 별도로 정답을 검증합니다.
실시간 관찰과 Host 응답은 다른 coroutine에서 처리합니다. Memory 옵션은 출처가 있는 후보만 추출합니다.
자세한 공개 API와 실행 방법은 [Host 사용법](docs/dev/HOST_USAGE.md), 구현 및 검증 범위는
[8단계 보고서](docs/dev/reports/phase_8_integration/phase_8_integration.md)를 참고하세요.

간단한 작업은 `await solver.solve(task=..., provider=..., model=...)`로 결과를 받습니다.
`start()`는 실행을 시작한 Session을 반환하고, `create_task(request=TaskRequest(...), ...)`는
시작 전 Session을 반환합니다. Provider·모델·옵션·도구는 작업마다 지정할 수 있습니다.
외부 도구를 생략해도 작업·계획·부모 통신·자원 요청 도구를 사용할 수 있습니다.

- 완료는 단독 `task_complete` 도구 호출로만 확정합니다. 일반 텍스트는 중간 메시지입니다.
- `session.send_instruction(...)`, `answer(...)`, `cancel()`로 실행에 개입합니다.
- `session.events(after_sequence=...)`는 다중 구독과 세션 수명 내 재조회를 지원합니다.
- `await session.state()`는 TaskState·AgentState와 같은 시점의 `last_event_sequence`를 반환합니다.
  `await session.artifact(id)`로 진행 중 호출이나 과거 동작의 상세 사본을 조회합니다.
- `result.tracer`는 `finished`까지 포함한 독립적인 Tracer Snapshot입니다. 실시간 구독 없이도
  기록이 남으며, `events`, `artifacts`, `state()`, `to_dict()`, `to_json()`으로 사후 조회·내보내기합니다.
- 취소는 협력적입니다. 진행 중 외부 호출은 기다리며, Host 질문 대기는 즉시 해제합니다.
- `solve()`에도 질문이 발생할 수 있습니다. 기본 응답 제한은 300초이고,
  `AgentLimits(host_response_timeout=None)`이면 답변이 올 때까지 기다립니다.
- Conversation은 현재 Task의 메시지·도구 호출·결과 원본 기록입니다. `conversation_search/read`는
  요약되거나 LLM 입력에서 잘린 원문도 검색·부분 조회하며, Host의 장기 Memory를 검색하지 않습니다.
- `memo_write(content, kind, source_refs)`로 남긴 작업 메모는 다음 판단에 반영되고
  `TaskResult.memos`의 `TaskMemo`로 반환됩니다. `memo_id`, `task_id`, `created_step`, `source_refs`로
  출처를 확인할 수 있습니다. 장기 Memory의 선택·저장은 Host 책임입니다.
- Context는 한 번의 LLM 호출에 전달하는 메시지 묶음입니다. `ContextBuilder`가 TaskContext의
  읽기용 스냅샷에서 최초 지시·Host Context·Summary·Memo·현재 Plan·추가 지시·최근 Conversation을 조합합니다.
  Summary·Memo·Plan은 assistant 역할을 유지하고 반환 사본을 수정해도 원본 기록은 바뀌지 않습니다.
- LLM은 필요할 때만 `plan_create`, `plan_read`, `plan_update`, `plan_revise`로 Task별 계획을 관리합니다.
  계획 변경은 실제 작업을 실행하지 않으며, 항목 상태도 Task의 완료·실패를 결정하지 않습니다.
  현재 계획은 `result.plan`, 실시간 상세는 TaskState의 `plan_artifact_id`로 조회합니다.
- Host가 `subagents={설정명: SubagentConfig(...)}`와 Root 한도를 명시하면 직접 자식을 병렬 실행할 수 있습니다.
  Root에는 `subagent_spawn/status/result/wait/cancel`과 `subagent_ask/send`, `agent_reply/wait_reply`가 추가됩니다.
  자식은 `parent_ask/notify`, `agent_reply/wait_reply`로 실제 부모와 통신하며 같은 Task 엔진과
  독립적인 Context·Plan·상태·Tracer를 사용합니다. Host가 설정별 재위임을 승인하면 자손도 같은 엔진으로
  실행하며 Root 전체 누적 생성 수·깊이 한도를 공유합니다. 추가 도구는 아래의 Host 승인 API로 적용합니다.
- Workspace는 선택적으로 주입합니다. 장기 Memory·Storage 자동 연동은 제공하지 않습니다.

직접 자식 실행 예제:

```python
from shs_agent import AgentLimits, SubagentConfig, TaskTreeSnapshot

# child_registry/child_executor는 Host가 위임을 허용한 외부 도구만 담습니다.
child_config = SubagentConfig(
    provider=provider, model="qwen3.5:9b", options=None,
    registry=child_registry, executor=child_executor,
    limits=AgentLimits(max_steps=12, max_tool_calls=20),
)
root = await ProblemSolver().start(
    "필요한 독립 조사를 위임하고 결과를 검토해줘.", provider=provider, model="qwen3.5:9b",
    limits=AgentLimits(max_total_subagents=2, max_subagent_depth=1),
    subagents={"research": child_config},
)
for item in await root.subagents.children():
    child = root.subagents.child(item["task_id"])
    print(await child.state())  # 기존 events/artifact/result/send_instruction/cancel도 사용 가능
result = await root.result()
saved = result.tree.to_json()  # 단일 Root + 전체 자손, Session 없이 보관 가능
restored = TaskTreeSnapshot.from_json(saved)
print(restored.result(root.task_id)["output"])
```

설정별 Provider·모델·옵션·개별 한도는 생략하면 부모 값을 사용합니다. 옵션을 명시적으로 `None`으로
주면 상속하지 않습니다. 외부 도구 집합은 항상 Host가 명시하며, LLM은 승인된 설정명만 선택합니다.
실행 중 자식이 있으면 부모 완료를 도구 오류로 거부합니다. 부모의 비정상 종료에서는 자식에게
협력적 취소를 요청하고 외부 호출 종료·결과 회수를 기다립니다. 새 부모 지시는 자식에게 자동 전달하지 않습니다.
표준 라이브러리/Fake Provider로 실행하는 전체 예제는 `python examples/subagents.py`입니다.

재귀 위임은 `SubagentConfig.allowed_subagent_config_ids`에 Host가 승인한 하위 설정 ID를 지정합니다.
기본값 `()`는 재위임 권한이 없다는 뜻입니다. 아래에서 `research` 자식은 `leaf`만 생성할 수 있습니다.
각 설정의 Registry/Executor는 명시해야 하며 부모 외부 도구를 자동 복사하지 않습니다.

```python
research = SubagentConfig(
    registry=research_registry, executor=research_executor,
    allowed_subagent_config_ids=("leaf",),
)
leaf = SubagentConfig(registry=leaf_registry, executor=leaf_executor)
root = await ProblemSolver().start(
    "필요하면 조사를 세분화하고 결과를 통합해줘.", provider=provider, model=model,
    limits=AgentLimits(max_total_subagents=10, max_subagent_depth=3),
    subagents={"research": research, "leaf": leaf},
)
for item in await root.subagents.descendants():
    descendant = root.subagents.child(item["task_id"])
    print(await descendant.state())
# 특정 Task의 직접 자식만 조회: await root.subagents.children(parent_task_id)
result = await root.result()
```

누적 생성 수는 완료한 자손도 포함합니다. 최대 깊이에 도달하면 생성 도구를 숨기고 직접 작업과 부모
통신은 유지합니다. 중간 Agent의 관리 도구는 자신의 직접 자식만 제어하며, 비정상 종료 시 각 부모가
자식 취소·회수를 수행하므로 모든 깊이로 이어집니다. Host는 Root Manager에서 모든 자손 Session을
조회합니다. 각 자손의 `session.subagents`는 기존처럼 `None`이고, Agent 내부에는 Task에 바인딩된 관리
인터페이스가 별도로 연결됩니다. `TaskResult.tree`는 Root 결과에 한 번 포함됩니다.

Tree JSON은 `schema_version=2`, `scope="root_and_descendants"`이며 이전 직접 자식 Tree schema 1을
읽을 수 있습니다. 각 Task의 Tracer schema와 독립 sequence는 유지됩니다.
`python examples/recursive_subagents.py`로 Depth 3의 7개 Task, Live/최종 Tree, 실제 제공 스키마를
`build/recursive_subagents_demo/`에 내보낼 수 있습니다.

질문 도구는 `request_id`를 즉시 반환합니다. `agent_wait_reply(request_id)`와 `subagent_wait(task_ids)`는
새 수신 질문·정보·제어가 있으면 `reason`, `request_ids`, `message_ids`를 반환하여 LLM 판단을 재개합니다.
원래 요청과 자식 작업은 유지됩니다. 질문·알림은 진행 중 LLM/도구를 취소하지 않고 묶음이 끝난 뒤 반영합니다.
`subagent_send(kind="instruction")`만 기존 추가 지시 규칙을 사용하며 부모 출처를 보존합니다.
Host 질문 대기 중 자식 질문이 도착하면 `host_ask`는 `pending=true`, `question_id`로 대기를 해제합니다.
Host 질문은 열려 있으며 나중 답변은 같은 ID로 다음 안전한 경계에 반영됩니다. 같은 question/context로
`host_ask`를 다시 호출하면 기존 질문의 대기를 재개합니다. 질문을 바꾸거나 종료하여 덮어쓰지 않습니다.
기존 Host 응답 제한은 최초 질문부터 유지되며 Agent 요청에는 새 timeout을 추가하지 않습니다.
`python examples/communication.py`는 A→부모→B→부모→A 흐름을 gate로 재현하고,
`build/communication_demo/`에 TaskTree와 실제 내부 스키마를 저장합니다. 실제 모델 검증과 구분되는 Fake 예제입니다.

작업 메모 API는 `TaskMemory` → `TaskMemo`, `TaskResult.memory` → `TaskResult.memos`,
`memory_write` → `memo_write`로 변경했습니다. 이벤트는 `task_memo_written`과
`finished.payload.memo_items`, 설정은 `AgentLimits.max_memo_item_chars`(4,000)와
`max_task_memo_chars`(24,000)를 사용합니다. 구 이름의 호환 별칭은 제공하지 않으며,
LLM에는 내부 메모 작성 도구를 `memo_write` 하나로 노출합니다.

설치와 검증:

```text
pip install -e .
pip install -e ".[ollama]"   # Ollama SDK를 사용하는 경우
python -m unittest discover -s tests -v
uv build
python tests/verify_distribution.py
```

자동 테스트는 Fake Provider와 Fake Tool만 사용합니다. 테스트 자체는 표준 라이브러리로 실행되며,
실제 Ollama 예제는 `python test.py`로 별도 실행합니다.
`OLLAMA_HOST`, `OLLAMA_MODEL` 환경 변수로 서버와 모델을 지정합니다.
로컬 `gemma4:e4b`, `qwen3.5:9b`가 설치되어 있으면 `python tests/verify_ollama_tracer.py`로
실제 LLM↔Tool과 상세 Tracer를 검증합니다. 이 스크립트는 모델을 다운로드하지 않습니다.
같은 로컬 모델로 `python tests/verify_ollama_subagents.py`를 실행하면 실제 부모의 두 자식 생성,
상태/대기/각 결과 회수, 자식 LLM↔Tool↔Memo, Task 동시 진행과 상세 기록을 검증합니다.
`--model gemma4:e4b` 또는 `--model qwen3.5:9b`로 하나만 선택할 수 있습니다.
기본 서버는 `http://localhost:11434`이며 모델 다운로드·서버 설정 변경 없이
`build/ollama_subagents/`에 실행별 원본과 판정 결과를 저장합니다. SDK 요청 중첩은 추론 병렬성의 증거가 아닙니다.
실측 결과와 알려진 모델 응답 오류는 [4단계 보고서](docs/dev/reports/phase_4_subagents/phase_4_subagents.md#71-기존-로컬-ollama-실제-모델-검증)에 기록했습니다.

```python
from shs_agent import TracerSnapshot

state = await session.state()
print(state.task.status, state.agent.activity)
if state.task.plan_artifact_id is not None:
    print((await session.artifact(state.task.plan_artifact_id)).data)
async for event in session.events(after_sequence=state.last_event_sequence):
    print(event.sequence, event.type)
    for artifact_id in event.artifact_ids:
        detail = await session.artifact(artifact_id)
        # 상세 화면에서 detail.data 조회. Host 질문 입력은 별도 경로에서 처리합니다.

result = await session.result()
trace_json = result.tracer.to_json(indent=2)
restored = TracerSnapshot.from_json(trace_json)
print(restored.state().task.status)
for event in restored.events:
    if event.type == "tool_finished":
        detail = restored.artifact(event.payload["result_ref"]["artifact_id"])
        if detail.data["raw_result_artifact_id"] is not None:
            print(restored.artifact(detail.data["raw_result_artifact_id"]).data)
```

이벤트 순서는 Task별 `sequence`로 판단합니다. `timestamp`는 UTC 시각이며 호출의
`duration_ms`는 단조 시계로 측정합니다. 기록은 인메모리이며 이벤트를 자동 삭제하지 않으므로
사용 메모리는 상세 이력의 크기에 비례합니다. schema 2 내보내기는 상세 Artifact와 최종 Context를 포함하여
`result.messages/memos`를 별도 저장하지 않아도 원본 참조를 해결합니다. 공통 Provider 요청·응답과
기존 Adapter의 SDK 인자·명시적 응답 필드를 구분하고 공개된 thinking/content/tool calls를 함께 보존합니다.
HTTP 원문·비공개 추론·연결 인증정보는 수집하지 않습니다. `complete`는 로그 종료 여부,
`details_complete`는 `recording_scope` 내 수집 성공 여부이며 누락·제외는 Artifact의 `issues`로 확인합니다.

구조·호출 흐름·검증 범위는 [1단계 보고서](docs/dev/reports/phase_1/report.md)와
[2단계 Tracer 보고서](docs/dev/reports/phase_2_tracer/phase_2_tracer.md),
[2단계 State·상세 Tracer 보완 보고서](docs/dev/reports/phase_2_state_tracer/phase_2_state_tracer.md),
[3단계 Planning/Replanning 보고서](docs/dev/reports/phase_3_planning/phase_3_planning.md),
[4단계 Sub-agent 보고서](docs/dev/reports/phase_4_subagents/phase_4_subagents.md),
[5단계 Agent Communication 보고서](docs/dev/reports/phase_5_communication/phase_5_communication.md),
[6단계 재귀 Sub-agent 보고서](docs/dev/reports/phase_6_recursive_subagents/phase_6_recursive_subagents.md),
[7단계 Tool Request·Host Integration 보고서](docs/dev/reports/phase_7_tool_integration/phase_7_tool_integration.md), 장기 목표는
[목표 아키텍처](docs/dev/architecture/SHS_AGENT_TARGET_ARCHITECTURE.md)를 참고하세요.

실행 중 추가 도구는 `start/create_task/solve(host_tools=catalog, allowed_tool_ids=(...))`로
활성화합니다. `catalog`는 Host가 구현한 `ToolSpec`과 handler를 등록한 `ToolRegistry`이며,
자손의 허용 목록은 `SubagentConfig.allowed_tool_ids`에 따로 지정합니다. 허용 목록은 실제 사용 승인이
아닙니다. Agent의 `tool_request` → 부모의 `tool_forward` → Root의 Host 승인 요청 이벤트를 보고
Host가 최초 요청 Session에 `await target.grant_tool(request_id=..., tool_id=...)` 또는
`await target.deny_tool(request_id=..., reason=...)`를 호출합니다.

`grant_tool`은 `approved`를 반환합니다. 현재 LLM/도구 호출 묶음이 끝난 뒤 실제 handler를 해당 Task의
전용 Registry/Executor에 연결하고 `applied`를 기록합니다. 다음 판단의 스키마 제공은
`tool_set_offered` 이벤트로 따로 확인합니다. `await target.tool_state()`는 적용 revision·스키마 식별자·
도구 목록·요청 상태를 반환합니다. 자연어 승인·부모 도구 보유·다른 Task 승인은 권한을 부여하지 않습니다.
활성 Task의 기본 Registry를 직접 변경하면 감지해 거부하므로, 실행 중 추가는 승인 API를 사용하세요.

```text
python examples/tool_integration.py --schema-dir docs/dev/reports/phase_7_tool_integration
```

이 예제는 Fake Provider로 Root → A → A-1 요청을 전달하고 Host의 명시적 승인 후 실제 handler를 호출합니다.
관찰 coroutine과 Host 결정 처리를 분리하며, 전체 Tree는 `build/tool_integration_demo/task_tree.json`에 저장합니다.
새 UI·외부 서버·임의 코드 실행·패키지 설치·자동 권한 확대는 추가하지 않습니다.

실제 Ollama 모델 4개의 결제 사고 조사 시나리오는 `python tests/verify_ollama_tool_integration.py`로 실행합니다.
각 모델을 한 번씩 실행하고 로컬 파일·SQLite, 재귀 위임, Host 권한 요청과 상세 trace를 검증합니다.
기록 분석만 할 때는 `python tests/review_ollama_tool_integration.py <실행 폴더>`를 사용합니다.
실측 결과는 [Gemma 보고서](docs/dev/reports/phase_7_tool_integration/ollama_gemma_report.md),
[Qwen 보고서](docs/dev/reports/phase_7_tool_integration/ollama_qwen_report.md),
[성능 비교·아키텍처 보고서](docs/dev/reports/phase_7_tool_integration/ollama_comparison_architecture_report.md)에 있습니다.

8단계의 작은 실제 모델 실험은 `python examples/ollama_workflow_host.py --model <설치된 모델 태그>`로
별도 실행합니다. 다운로드나 자동 재시도 없이 한 번 실행하며, 미실행 기능은 `not_observed`로 기록합니다.
Fake 검증과 실제 모델의 업무 수용 결과는 [8단계 Ollama 보고서](docs/dev/reports/phase_8_integration/ollama_report.md)에
구분했습니다. wheel에도 예제와 사용 문서를 `share/shs-agent/` 데이터 파일로 포함합니다.
