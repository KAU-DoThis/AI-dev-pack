# shs-agent Host 사용 계약

현재 공개 API 기준, Python 3.11 이상. Host는 Provider·모델·도구 구현·권한·입력·출력 검증을 소유한다.
`ProblemSolver`가 Root와 모든 자손의 동일 실행 엔진을 조립한다. Host에서 Runtime이나 Manager를 직접
생성하지 않는다. 서버·UI·새 실행 프레임워크·자동 장기 Memory는 필요하지 않다.

## 실행 가능한 예제

저장소에서 사용하는 Python 환경에 `python -m pip install -e .`로 설치한다. 이미 준비된 Windows 환경은
`.venv\Scripts\python.exe`다. 설치 없이 소스를 확인할 때 PowerShell에서 `$env:PYTHONPATH = 'src'`를
설정할 수 있다. 새 모델이나 SDK가 없어도 아래 Fake 예제를 실행할 수 있다.

```text
python examples/full_workflow_host.py --review-memory
python examples/replay_task_tree.py build/full_workflow_demo/task_tree.json
python examples/replay_task_tree.py build/full_workflow_demo/task_tree.json --details
python examples/full_workflow_host.py --control instruction --output-dir build/phase8_instruction
python examples/full_workflow_host.py --control cancel --output-dir build/phase8_cancel
```

기본 결과는 정산 완료 거래 3건/2,500원이다. 이 정답은 Host 평가에만 있으며 최초 Agent Context에는
주입하지 않는다. Fake Provider도 실제 `ledger_read` 결과를 읽고 계산한다. 지시 예제는 A-1에만
`Only include T1.`을 보내 1건/1,200원으로 변경한다. 취소 예제는 진행 중인 A-1 handler를 완료시킨 뒤
협력적으로 취소하고 부모가 미완료 결과를 회수한다. 이때 업무 수용 실패를 표시하므로 CLI 종료 코드는 1이다.

각 실행은 지정 폴더의 `task_tree.json`, `live.jsonl`, `summary.json`을 갱신한다. 실행을 보존하려면 서로
다른 `--output-dir`을 지정한다. `--quiet`은 터미널 이벤트 표시만 생략한다. 기록 수집은 계속된다.
`--review-memory`는 후보를 summary에 넣으며 장기 Memory에 저장하지 않는다.

외부 프로젝트에서는 빌드된 wheel을 `python -m pip install <wheel 경로>`로 설치한다. wheel의 예제와
문서는 Python 설치 prefix의 `share/shs-agent/`에 포함된다. `sysconfig.get_path('data')` 아래에서 찾거나
`importlib.metadata.files('shs-agent')`로 조회할 수 있다. 예제의 `full_workflow_host.py`,
`workflow_support.py`, `replay_task_tree.py`를 같은 폴더에 복사해 실행해도 된다.

## solve, start, create_task

```python
from shs_agent import AgentLimits, ProblemSolver, TaskRequest

# provider: chat(model, messages, tools, options) -> LLMResponse를 구현한 Host 객체
solver = ProblemSolver(provider)
result = await solver.solve("주어진 자료를 요약해줘.", model="host-selected-model")

# 실행 중 질문/승인/제어가 필요한 Host는 start를 사용한다.
root = await solver.start(
    TaskRequest("거래를 조사해줘.", context=("합성 fixture를 사용한다.",)),
    model="host-selected-model",
    limits=AgentLimits(max_steps=30, max_tool_calls=60),
)
# 별도 coroutine에서 root.events() 구독과 Host 입력 처리를 연결한다.
result = await root.result()
```

`solve()`는 종료 결과까지 기다린다. `start()`는 실행을 시작하고 `TaskSession`을 반환한다.
`create_task(request=TaskRequest(...), model=...)`는 아직 실행하지 않은 Session을 반환하며
`await session.start()`가 필요하다. Provider는 Solver 기본값 또는 각 호출의 `provider=`로 지정한다.
`options=`는 Provider에 전달할 옵션이다. 기존 Ollama/OpenAI adapter에는 `shs_agent.llm.ChatOptions`를
사용한다. `model_selector(step)`는 기존 Task별 모델 선택 hook이며 자동 모델 변경 정책은 없다.

사용자에게 묻는 `host_ask`는 `solve()`에도 발생할 수 있다. 답변할 Host가 없으면 기본 300초 후
응답 제한으로 종료하며 `host_response_timeout=None`이면 계속 기다린다. 예제는 별도 응답 coroutine을
두기 때문에 None을 명시했다. 단순 Task는 Sub-agent·Tool Integration·Plan 없이 같은 엔진으로 실행된다.

## 도구와 실행 한도

```python
from shs_agent.tool import Tool, ToolSpec, ToolRegistry, ToolExecutor

def read_total():
    return {"total": 42}  # 실제 프로젝트에서는 Host의 허용된 자료만 조회

registry = ToolRegistry()
registry.register(Tool(ToolSpec("read_total", "Read the Host total", {
    "type": "object", "properties": {}, "additionalProperties": False,
}), read_total))
root = await solver.start("합계를 조회해줘.", model="host-selected-model",
                          registry=registry, executor=ToolExecutor(registry))
```

기본 `ToolRegistry`에는 executor를 생략해도 Solver가 연결한다. 커스텀 Registry는 명시적 executor가
필요하다. 동기/비동기 handler를 지원한다. Host는 handler의 파일·DB 범위와 인증, 공유 객체의 동시 사용을
관리한다. 실행 중 Registry를 직접 변경하지 않는다. 추가 도구는 승인 API로 적용한다.

`AgentLimits` 기본값 중 `max_steps=20`, `max_tool_calls=50`, `max_tool_output_chars=12000`이며
Context 기본 압축 트리거는 확인된 Provider context 초과다. `context_compact_threshold_chars=None`이
기본이며 숫자를 명시하면 이전 문자 수 기반 선제 압축을 선택한다. 예제의 단계 예산 60, 호출 90은 규모를
위한 Host 설정이며 라이브러리 기본값을 바꾸지 않는다. 전체 자손 한도 `max_total_subagents`,
`max_subagent_depth`는 Root에서 설정한다. 기본값은 None이지만 Sub-agent를 활성화할 때는 두 값을
모두 명시해야 한다. 단계·도구·문맥 한도와
전체 생성 수·깊이는 다른 기준이다. 종료한 자손도 누적 생성 수에 포함된다.

## 재귀 구성과 Communication

```python
from shs_agent import SubagentConfig

empty = ToolRegistry()
profiles = {
    "analyst": SubagentConfig(registry=empty, executor=ToolExecutor(empty),
                              allowed_subagent_config_ids=("leaf",)),
    "leaf": SubagentConfig(registry=empty, executor=ToolExecutor(empty),
                           allowed_tool_ids=("ledger_read",)),
}
root = await solver.start("조사를 위임하고 결과를 통합해줘.", model="host-selected-model",
    subagents=profiles, host_tools=catalog,
    limits=AgentLimits(max_total_subagents=3, max_subagent_depth=2))
```

`catalog`는 Host가 도구를 등록한 `ToolRegistry`다. 모델은 `subagent_spawn`의 `config_id`로
승인된 profile만 선택한다. profile의 `provider`, `model`, `options`, Task별 limits는 생략 시
부모 설정을 상속한다. `options=None`은 명시적으로 상속을 끈다. 외부 도구 집합과 동적 허용 목록은
각 profile에 명시한다. 동적 승인은 부모나 미래 자손의 권한으로 자동 전파되지 않는다.

Host는 `await root.subagents.descendants()`와 `root.subagents.child(task_id)`로 모든 자손을 찾는다.
`children(parent_task_id)`는 특정 Task의 직접 자식이다. Root Session에만 전체 Manager가 노출되며
자손 Session의 `subagents`는 None이다. 자손 안의 Agent는 바인딩된 scope로 직접 자식만 제어한다.

Agent의 `parent_ask`/`subagent_ask`는 `request_id`를 반환한다. `agent_reply`는 받은 질문 ID를 사용한다.
`agent_wait_reply`와 `subagent_wait`는 새 질문·제어 등으로 일찍 돌아올 수 있으므로 `reason`을 확인한다.
기존 질문은 그 자체로 완료되지 않는다. 형제 간 질의는 부모를 거친다. 전체 예제에서는
A-1 → A → Root → B → Root → A → A-1을 실제 메시지와 `related_request_id`로 연결한다.

## 질문과 제어

```python
state = await target.state()
receipt = await target.answer(question_id=question_id, text="승인된 처리 방식")
instruction_receipt = await target.send_instruction("해당 범위만 다시 검토해줘.")
cancel_receipt = await target.cancel()
```

위 명령은 필요할 때 각각 호출하는 별도 동작이다. 반환된 ID는 접수 확인이다.
`command_applied`/`command_rejected`를 확인해야 적용을 알 수 있다. 닫힌 질문에 대한 답변은 실행 중에는
`no_matching_pending_question`으로 거부되고, 종료된 Session에 새 명령을 보내면 예외가 발생한다.
Host 질문은 `question` 이벤트의 정확한 `question_id`와 발생한 Session으로 답한다.
Agent 간 질문은 `agent_message_received`와 `request_id`로 구분하며 Host.answer로 대신 답하지 않는다.

관찰 coroutine에서는 입력을 기다리지 않는다. 전체 예제는 `TreeObserver`가 관찰하고,
`host_decisions`가 별도 Queue를 소비한다. 특정 대상의 지시·취소는 추가 coroutine에서 보낸다.
대화형 입력이 필요하면 Host가 별도 입력 스레드/async UI를 연결해야 한다. 예제는 고정된 데모 정책으로
자동 응답하며 `input()`을 사용하지 않는다. 새 지시는 다른 Task에 자동 전달되지 않는다.

진행 중 LLM/도구는 강제 중단하지 않는다. 새 지시가 있으면 기존 응답을 폐기하는 기존 규칙을 유지하고,
이미 시작한 도구 결과는 기록한 뒤 안전한 경계에서 지시를 적용한다. 취소도 외부 호출 반환을 기다린다.
부모의 비정상 종료는 기존 계약에 따라 직접 자식 취소·회수를 수행하며 모든 깊이에서 재귀적으로 이어진다.
부모 정상 완료는 실행 중인 자식이 있으면 거부된다. 끝나지 않는 외부 호출을 강제로 회수하는 보장은 없다.

## 동적 Tool Integration

Agent의 `tool_request` → 각 부모의 `tool_forward` → Root의 `tool_host_approval_requested` 순으로 전달한다.
Host는 이벤트의 `requesting_task_id`로 최초 요청 Session을 찾고, 원래 `request_id`로 결정한다.
hop의 질문 ID 또는 `host_request_id`를 `grant_tool`에 넣지 않는다.

```python
payload = event.payload  # event.type == 'tool_host_approval_requested'
target = root if payload["requesting_task_id"] == root.task_id else root.subagents.child(payload["requesting_task_id"])
receipt = await target.grant_tool(request_id=payload["request_id"], tool_id="ledger_read")
assert receipt["status"] == "approved"
# 거부를 선택하는 경우에는 grant_tool 대신 호출한다.
# await target.deny_tool(request_id=payload['request_id'], reason='범위 밖의 자료')
```

| 상태 | 의미 |
|---|---|
| requested | Agent가 요청했고 아직 Host까지 전달되지 않음 |
| awaiting_host | Root가 Host 결정 요청을 기록함 |
| approved | Host가 승인함. 아직 실행 가능한 도구라는 뜻은 아님 |
| applied | 요청 Task의 도구 집합에 실제 적용됨 |
| denied | Host가 거부함 |
| apply_failed | 승인 후 적용이 실패했거나 적용 전에 Task가 종료됨 |
| closed | 승인 전 미해결 요청이 Task 종료로 닫힘 |

현재 도구 배치 중간에는 스키마를 변경하지 않는다. `tool_access_applied` 다음
`tool_set_offered`의 revision/LLM Call ID와 실제 `llm_common_request` Artifact의 tools를 확인한다.
마지막으로 `tool_finished`의 succeeded/executed와 원본 결과를 조회해야 handler 실행을 확인할 수 있다.
`tool_state()`는 도구 revision과 `last_offered_revision`, 요청 상태를 반환한다. 승인만으로 성공을 기록하거나,
자연어 `agent_reply`를 권한 부여로 해석하지 않는다. 실패·거부 후 Replan은 모델이 실제 계획 도구를 호출한다.

## 상태, Planning, 전체 Tree 관찰

`TaskState`는 작업 상태·종료 이유·Task/Parent/Root ID·깊이·Plan 참조를 가진다. `AgentState`는 현재
모델·활동·operation·질문·대기 대상·호출 수를 가진다. 업무 상태와 현재 실행 활동은 다른 차원이다.
`await session.state()`는 두 상태와 같은 시점의 `last_event_sequence`를 원자적으로 반환한다.
`session.events(after_sequence=cursor)`는 그 다음 사건부터 이어받는다.

Plan은 선택 사항이다. `plan_create/update/revise`의 실제 이력은 `plan_created/updated/revised`와
Artifact로 보존된다. 현재 상세는 `state.task.plan_artifact_id`로 조회하며 마지막 Plan은 `result.plan`이다.
Plan의 completed는 Agent의 평가다. Task 완료나 업무 검증을 대신하지 않는다. Replan은 권한·자손·목표를
자동 변경하는 정책이 아니다.

전체 관찰은 예제의 작은 Host helper `TreeObserver`가 담당한다. 공개 라이브러리 API 추가는 없었다.

1. Root Manager의 `snapshot()`으로 현재 트리의 일관된 단면을 읽는다.
2. 각 Task의 Trace prefix를 한 번 읽고 `trace.state().last_event_sequence` 다음 이벤트를 구독한다.
3. 기존/새 `subagent_created`를 읽으면 실제 Session을 찾아 등록한다. 이미 등록한 Task ID는 중복 등록하지 않는다.
4. 새 자손도 자기 Trace prefix와 cursor를 사용하므로 등록 전에 끝난 자식의 사건을 놓치지 않는다.
5. `asyncio.TaskGroup`은 진행 중 추가된 자손 watcher까지 기다린다. Root만 끝났다고 관찰을 닫지 않는다.

`live.jsonl`에는 task-local Event Sequence와 해당 사건 시점의 State, Artifact ID,
Host 수집 순서 `receipt_index`, prefix 여부 `historical`이 들어간다. 수집 순서는 실제 저장되는 순서이며
여러 Task 사이의 인과 순서를 뜻하지 않는다. `timestamp` 정렬도 인과관계를 보장하지 않는다.
Task/LLM/Tool Call/Request ID와 `task_refs`를 사용해 실제 관계를 추적한다. 현재 state()를 과거 이벤트의
상태로 잘못 붙이지 않고 상태를 포함한 이벤트로 순서대로 복원한다.

## TaskResult와 Replay

```python
from pathlib import Path
from shs_agent import TaskTreeSnapshot

result = await root.result()
print(result.status, result.stop_reason, result.output, result.memos, result.plan)
Path("task_tree.json").write_text(result.tree.to_json(indent=2), encoding="utf-8")
restored = TaskTreeSnapshot.from_json(Path("task_tree.json").read_text(encoding="utf-8"))
for task_id, trace in restored.traces.items():
    print(trace.state(), restored.result(task_id))
    for event in trace.events:
        if event.type == "tool_finished":
            detail = trace.artifact(event.payload["result_ref"]["artifact_id"])
            raw_id = detail.data["raw_result_artifact_id"]
            if raw_id is not None:
                print(trace.artifact(raw_id).data)
```

재귀 기능을 활성화한 Root의 `tree`는 전체 자손을 한 번씩 포함한다. 단일 Task는 `tree=None`이며
`result.tracer` / `TracerSnapshot.from_json()`을 사용한다.
`TaskTreeSnapshot.result(task_id)`는 최종 output·상태·Memo·Plan을 읽는다.
`trace.state(at_sequence=n)`은 과거 상태를 복원한다. 각 Event의 같은 Task 참조는 `trace.artifact(id)`,
교차 Task 참조는 `tree.artifact({"task_id": ..., "artifact_id": ...})`로 해결한다.

Replay는 JSON 데이터를 읽는 작업이다. Provider/Session/Registry/Manager 없이 가능하며 LLM이나 handler를
재실행하지 않는다. 실행 재개·Checkpoint·자동 Storage 저장과 다르다. `complete=True`는 로그가 정상적으로
닫혔다는 뜻이다. failed/cancelled도 complete일 수 있다. `details_complete`는 기록 범위 안의 상세 수집
완전성이고 제외된 HTTP 원문·숨은 추론·인증정보까지 수집했다는 뜻이 아니다. `issues`와 `recording_scope`를 읽는다.

## 엔진 종료, 업무 수용, Memory

실제 엔진 종료 상태 completed/failed/cancelled/limit_reached/incomplete를 그대로 보존한다.
Host의 `evaluate()`는 JSON 출력·fixture 정답·실제 조회 성공을 따로 검증한다. `task_complete`를 호출해도
잘못된 답이면 `business_review.accepted=false`다. 평가 실패를 이유로 Trace나 TaskStatus를 고치지 않는다.

`memory_candidates()`는 수용된 결과의 일부와 원래 Task/Tool-result Artifact를 추출한다. Memo·Plan·자식 결과와
원본 도구 결과도 `TaskResult`/Tree에 있어 Host가 검토할 수 있다. 임시 정보나 미검증 추론을 확정 사실로
자동 저장하지 않는다. 기본 예제는 후보도 추출하지 않으며, 명시한 옵션으로 후보를 검토할 때도 실제 장기
Memory 저장은 하지 않는다.

## 실제 Ollama와 검증

```text
python examples/ollama_workflow_host.py --model gemma4:e4b
python -m unittest discover -s tests -v
uv build --offline
python tests/verify_distribution.py
```

Ollama 예제는 이미 설치된 SDK·모델과 실행 중 서버가 있어야 한다. `--host`로 서버를 지정한다. 모델 태그는
정확히 존재해야 하며 없으면 종료한다. 한 번의 작은 Root+자식 집계 실행이며 다운로드·서버 설정 변경·
강제 재시도는 없다. Fake 전체 시나리오와 달리 깊이 2·동적 승인 등을 강제하지 않는다. 실제로 발생하지 않은
기능은 `not_observed`다. 성공 여부와 기록을 그대로 저장한다.

한계: 인메모리 상세 기록 비용, 모델 인자 오류·빈 응답, Provider가 알리지 않는 context 잘림은 남아 있다.
Token Streaming, Pause/Resume, 자동 영속화, Workspace 격리, 플러그인 설치, 장기 Memory,
분산 Scheduler, GUI는 이번 공개 사용 예제에 포함하지 않는다.

## v1.1 Workspace와 공통 부모 연결

```python
from shs_agent import Mount, Workspace

workspace = Workspace(project_path, delegable=True,
    mounts=(Mount("/reference", reference_path, read_only=True),))
session = await solver.start("코딩하고 테스트해줘.", model=model, workspace=workspace,
                             registry=command_tools, delegable_tool_ids=("run_python",))
```

물리 경로는 기존 디렉터리여야 한다. 논리 경로 기본값은 `/workspace`, cwd는 첫 mount다.
상대 경로는 Task의 cwd 기준이며 `os.chdir()`는 호출하지 않는다. `file_list/read/search/write/edit/delete`
모두 같은 resolver를 사용한다. mount 밖 경로·드라이브 경로·읽기 전용 쓰기·심볼릭 링크 탈출을 거부한다.
기본 파일 도구는 UTF-8 텍스트를 다룬다. 삭제는 파일 또는 빈 디렉터리만 지원한다.
논리 접근 제어는 OS 격리가 아니다. 임의 코드·명령 실행의 격리, mount 안 동시 파일 변경,
hard link 같은 OS별 추가 보안, 생성과 보관·정리는 Host가 책임진다.

`Workspace(root, delegable=True)`의 사용 권한과 자식 배정 권한은 다르다. 기본 `delegable=False`다.
`subagent_spawn(workspace_paths=[...], tool_ids=[...], role=..., result_requirements=...)`는
해당 부모의 위임 가능한 범위만 전달한다. Workspace가 파일 도구를 자동 제공하므로 `tool_ids`에는
`run_python` 같은 **추가 도구만** 적는다. `redelegate=True`일 때만 받은 자식도 다시 위임한다.
부모와 공유하거나 하위 경로만 선택할 수 있고, 별도 공간은 Host의 `SubagentConfig(workspace=...)`로 제공한다.
중첩 mount가 있는 상위 범위의 통째 위임은 거부하며 leaf 범위를 명시해야 한다.
별도 executor의 동작을 잃지 않도록 동적 도구 복사는 표준 ToolExecutor만 허용한다.

모든 Agent는 같은 ParentConnection을 쓴다. Host는 Task로 만들지 않으며 Root의 `parent_task_id`는 None이다.
`parent_ask(message)`는 즉시 요청 ID를 반환한다. `agent_wait_reply(request_id)`는 새 질문으로
돌아올 수 있으며, 같은 ID로 다시 기다릴 수 있다. `await session.answer(request_id=..., content=...)`가
Host의 답변이고 `await session.ask(message)`는 Host에서 Agent로 보내는 질문이다.
답변은 `parent_reply` 이벤트와 `requests()`에서 확인한다. Root의 여러 질문도 각각 별도 ID로 상관시킨다.
기존 `host_ask`는 호환용 전송+대기 도구로 유지하며, 새 코드는 분리된 API를 사용한다.

`plan_read(scope="parent", revision=...)`는 직계 부모 Plan의 사본만 읽는다. Plan이 없으면 `{"plan": None}`이다.
조상·형제의 Plan을 조회하거나 부모 Plan을 수정하는 권한은 주지 않는다.

## 실행 중 자원 요청과 적용

Agent는 `tool_request` 또는 `workspace_request(paths, reason)`으로 필요한 자원을 요청한다.
현재 부모는 `resource_grant`로 실제 위임 가능한 자원을 제공하거나 `resource_deny`로 거절하고,
권한이 부족하면 `resource_forward`로 상위에 전달한다. 기존 `tool_forward/wait/status`도 유지한다.
`resource_wait`는 결정이나 새 입력을 기다리고, `resource_status`는 적용 상태를 보여준다.
일반 `agent_reply("허용")`는 권한을 부여하지 않는다.

Host까지 올라온 요청은 `tool_host_approval_requested` 이벤트의 `resource_kind`, `requesting_task_id`,
`request_id`로 식별한다. Host는 **원래 요청 Task의 Session**에서 다음 중 하나를 호출한다.

```python
await target.grant_workspace(request_id=request_id, workspace=additional_workspace)
await target.supply_tool(request_id=request_id, tool=actual_tool, redelegate=False)
await target.grant_tool(request_id=request_id, tool_id="catalog_tool")  # 기존 catalog+allowlist 방식
await target.deny_resource(request_id=request_id, reason="사용 가능한 범위 밖")
```

승인은 `approved`이며 실행 가능하다는 뜻이 아니다. 현재 LLM 응답의 tool batch가 끝난 안전한 경계에서
적용하고 Workspace/tool revision을 올린다. 적용 실패는 `apply_failed`로 보이며 실패한 handler는 노출하지 않는다.
적용 이후 다시 조립된 system 안내와 실제 tools가 같은 환경을 가리킨다.
Workspace grant는 Task의 Workspace 구성을 교체한다. 기존 mount를 유지하려면 Host가 합친 구성을 전달한다.
`resource_state()`/`tool_state()`/`workspace()`는 현재 상태의 사본을 반환한다.

## Context 계측과 복구

고정 지시·설명·자료 템플릿은 `agent/prompts.py`, 동적 system 블록 조합은 ContextBuilder에 있다.
원래 TaskRequest/Conversation은 변경하지 않는다. 요약·Memo·Plan·통신은 system으로 승격하지 않는다.
마지막 메시지가 assistant 자료이면 다음 행동을 요청하는 user 메시지를 조립해
일부 Provider 템플릿의 assistant 이어쓰기 해석을 피한다.

`AgentLimits(context_window_tokens=..., context_window_source="host")`로 실제 사용 한도를 제공할 수 있다.
미제공 한도·남은 토큰은 null/unknown이다. 현재 입력의 `json_chars_div_3` 추정은 정확한 tokenizer 결과가 아니며,
호출 후 Provider `usage`와 모든 호출의 `cumulative_usage`를 별도로 기록한다.
Provider 옵션과 모델은 실제 요청 Artifact에서 확인한다. 모델 이름만으로 한도를 추측하지 않는다.

Provider는 `shs_agent.llm.ContextOverflow`를 던질 수 있다. OpenAI Adapter는 명시적 구조화 초과 코드만
정규화한다. `length`/`max_tokens`, 빈 응답, 네트워크 오류, 취소는 초과로 처리하지 않는다.
초과 후 완결된 tool call/result 묶음을 보존해 작은 요약 요청으로 나누고, 요약도 초과하면 다음 요청을
더 작게 만든다. 줄일 기록이 없거나 필수 입력만으로 넘치면 `incomplete/context_unrecoverable`이다.
한도 미제공 시 chunk 크기는 실패 요청의 절반과 32000자 중 작은 값인 복구용 추정치이며 모델 한도가 아니다.
한도가 제공되면 2배 문자 수를 보수적 출발점으로 사용한다. 이 추정이 맞지 않으면 실제 초과 응답으로 축소한다.
원본·파일·완료한 도구 결과는 보존하고, 최신 지시와 승인된 환경을 적용한 뒤 같은 논리 단계를 재호출한다.
`steps`는 논리 판단 단계, `llm_call_started.attempt`는 요약을 포함한 실제 시도 횟수다.
임의의 자동 재시도 횟수·외부 작업 재실행·로그 삭제 정책을 추가하지 않았다.

각 decision에는 `prompt_composition` Artifact가 있고, 블록 내용/해시·변수·Workspace revision·usage 추정치를
실제 요청과 연결한다. `context_overflow` → `context_compaction_started/context_compacted.recovery_of`
→ `llm_call_started.retry_of`로 복구 경로를 조회한다.

## 트리 구독과 실제 코딩 검증

`session.events(scope="tree", after_sequence=cursor)`는 `{"tree_sequence": n, "event": AgentEvent}`를 반환한다.
Task별 `event.sequence`와 구분한다. 집계 index는 원본 사건의 `(task_id, sequence)`만 보관한다.
`TaskTreeSnapshot.event_order`를 포함해 내보내면 `tree.events(after_sequence=...)`로 같은 순서를 재생한다.
기존 schema 1/2 파일은 계속 읽지만 event_order가 없는 과거 파일의 정확한 집계 순서는 복원한다고 주장하지 않는다.
기존 `events()`의 Task 범위는 그대로다.

```text
python examples/workspace_coding_host.py --workdir build/coding --model qwen3.5:9b
python examples/context_recovery_host.py --workdir build/recovery --model qwen3.5:9b
```

첫 예제는 Host가 불완전한 CLI와 CSV를 만들고, 두 자식의 구현/테스트·실제 Python 실행·독립적인
Host 수용 검사를 확인한다. 결과와 전체 트리는 해당 폴더에 보존한다. 기존 폴더를 덮어쓰지 않는다.
두 번째 예제는 **Host가 초과 신호를 한 번 주입**하고 실제 Ollama로 요약/재호출/파일 작업을 확인한다.
Ollama 서버에서 자연적으로 초과 오류가 발생했다는 검증과 구분한다.
기존 취소·자식 종료 정책과 Host 응답 기본 timeout은 유지한다. OS 격리·영속화·Resume는 별도 범위다.
