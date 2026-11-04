# 2단계 보완: TaskState / AgentState와 상세 Tracer

작성일: 2026-10-17. 단일 Task의 상태 조회와 관찰된 실행 입출력 보존을 구현했다.
현재 소스·호출 경로를 기준으로 기존 70개 테스트를 먼저 실행했고 모두 통과했다.
기존 1·2단계 보고서는 당시 기록으로 보존한다. 이 보고서가 상세 Tracer의 현재 계약을 설명한다.

## 1. 구조와 실제 상태 소유자

| 파일 | 책임 |
|---|---|
| `agent/state.py` | frozen TaskState, AgentState, SessionState 및 AgentActivity |
| `agent/session/channel.py` | 현재 TaskState/AgentState의 단일 소유자, 명령·질문·독립 이벤트 구독, 원자적인 상태+cursor 조회 |
| `agent/session/handle.py` | 기존 공개 Session API와 신규 `state()`, `artifact(id)` |
| `agent/runtime/runner.py` | 실제 실행 경계에서 상태 갱신 요청, LLM/도구/압축 관측, 종료 Snapshot 연결 |
| `agent/core.py` | 기존 Provider 호출 1회와 응답 검증 유지, 공통 요청·수신 응답·정규화 응답 관측 |
| `agent/tracer/tracer.py` | 단일 append-only Event 로그와 Artifact 저장·조회·종료 사본 |
| `agent/tracer/models.py` | AgentEvent, TraceArtifact, schema 2 TracerSnapshot, JSON 내보내기·재읽기·과거 상태 조회 |
| `agent/tracer/serialization.py` | 독립적인 구조화 사본, 지원 값의 명시적 인코딩, 인증정보 제외와 수집 실패 경로 표시 |
| `llm/observation.py` | 호출별 ContextVar hook과 명시적인 응답 필드 projection; SDK·Agent 객체를 소유하지 않음 |
| `llm/provider/openai.py`, `ollama.py` | 기존 변환 유지, SDK 호출 직전 인자·반환 직후 응답의 관측 hook |
| `agent/solver.py`, 공개 `__init__.py` | 입력·모델·Provider 식별을 Channel에 전달하고 새 데이터 모델 export |

Runtime의 `ExecutionState`를 제거했다. 실행 카운터는 AgentState에, TaskStatus는 TaskState에만 둔다.
Runtime에 남은 `_revision`과 `_pending`은 기존 지시 적용 제어 정보이며 다른 모듈에 중복 소유하지 않는다.
`session.status`는 Channel의 TaskState를 참조한다. Channel의 종료 여부는 Tracer의 로그 종료에 위임한다.
Conversation/Memo/Summary는 기존 TaskContext 소유를 유지한다. State에는 전체 본문을 복사하지 않는다.

- **TaskState:** task_id, 최초 instruction, input_artifact_id, status, created_at/started_at/finished_at, stop_reason, cancel_requested.
- **AgentState:** 실제 activity, operation_id/kind/artifact_id, model/provider, steps/tool_calls/tool_errors, waiting_for/question_id.
- **SessionState:** 위 두 읽기용 값과 같은 실행 시점의 last_event_sequence.

활동은 preparing, llm_call, tool_execution, waiting_for_host, context_compaction, finished다.
Provider는 클래스의 모듈·이름으로 식별하며 접속 키나 Client 객체를 State에 넣지 않는다.
카운터는 완료율이 아니고, LLM의 내부 판단을 활동 상태로 추정하지 않는다.
현재 Task의 상위 요청자는 Host이며 사용자 부모 Task나 가상의 Plan/Sub-agent를 만들지 않는다.

## 2. 상태 → 기록 → 조회 흐름

```text
실제 실행/명령/질문 경계
  → TaskChannel의 현재 상태 교체
  → 같은 루프에서 await 없이 해당 Event 기록
  → Tracer가 Task별 sequence/UTC timestamp 부여
  → 기존 asyncio.Event로 구독자 깨우기
  → 각 구독자는 자기 cursor로 같은 로그의 독립 사본 읽기

상세 관측 → 독립 Artifact 등록 → 그 Artifact를 참조하는 Event 발행

종료 → final_context 등록 → 최종 상태·finished 기록 → 로그 닫기
     → Event+Artifact TracerSnapshot → TaskResult.tracer
```

별도 이벤트 버스나 구독자별 복제 로그를 추가하지 않았다.
상태 갱신과 이벤트 추가, `state()`의 상태/cursor 읽기는 각각 루프를 양보하지 않는다.
따라서 상태를 읽은 뒤 그 cursor 이후를 구독하면 중간 이벤트도 재조회된다.
Artifact는 참조 이벤트보다 먼저 등록한다. `artifact(id)`는 중첩 데이터까지 독립적인 사본이다.
State는 작은 불변 필드만 가지므로 전체 Conversation/Tracer 복사 없이 조회한다.

취소 **접수** 시 `cancel_requested=True`를 기록한다. 기존 명령 처리 경계에서 CANCELLING으로 전환한다.
진행 중인 외부 도구는 계속 `tool_execution`과 같은 operation ID로 나타난다.
실행 여부 판단은 이전과 동일한 Runtime 경계에서 수행하며, Tracer가 취소나 완료 정책을 정하지 않는다.
Host 소비·화면 렌더링 완료는 기다리지 않는다. 같은 루프에서 Host가 오래 동기 실행하면 루프가 지연되는 제약은 남는다.

## 3. Event와 상세 Artifact 계약

기존 AgentEvent의 앞 네 위치 인자(task_id, sequence, type, payload), 이벤트 이름·주요 payload,
timestamp/step/event_id 및 기존 Session 제어 API를 유지한다. `artifact_ids`를 추가했다.
기록 순서는 sequence, 시간 분석은 UTC timestamp와 `perf_counter()` 기반 duration_ms를 사용한다.
Worker에서 SDK 정보를 관찰하면 그 시점의 `observed_at`도 보관하고, 로그 추가는 소유 루프로 넘긴다.

TraceArtifact에는 artifact_id, task_id, kind, timestamp, operation_id, data, issues, references가 있다.
Event.artifact_ids와 Artifact.references는 동일 Task 내 실제 보관된 자료를 가리킨다.
일반 Event마다 전체 Context나 원본 도구 결과를 복제하지 않는다.

| Artifact 종류 | 내용과 경계 |
|---|---|
| `task_input` | 최초 TaskRequest: instruction·system prompt·Host Context |
| `llm_common_request` | Core가 Provider.chat에 전달한 model, 전체 messages/tools, options. `context_built`가 같은 자료 참조 |
| `llm_adapter_request` | 기존 Adapter가 SDK에 전달한 kwargs와 `boundary="sdk_kwargs"` |
| `llm_adapter_response` | SDK 응답에서 명시적으로 선택한 필드와 `boundary="sdk_response_projection"` |
| `llm_common_response` | Core 검증 전 이미 수신한 content/thinking/reasoning/관련 필드, tool calls, finish reason, usage/metadata |
| `llm_normalized_response` | Core 검증·정규화를 통과한 실제 실행용 LLMResponse |
| `tool_request` | llm_call_id, tool_call_id, 요청 인자, 선택 근거 응답 Artifact |
| `tool_dispatch` | SessionTools.execute에 실제 전달한 인자, 요청 Artifact 연결 |
| `tool_raw_result` | 기존 executor/local handler가 반환한 결과. Conversation의 표시용 직렬화·절단 이전에 수집 |
| `tool_result` | 요청/dispatch/raw 결과 연결, LLM용 bounded 표현, 오류·실행 여부·상태·시간 |
| `context_summary` | 매번 실제 적용한 Summary와 원본 범위, 응답 Artifact, 적용 시 절단 여부 |
| `final_context` | 최종 전체 Conversation·Memo·Summary·압축 횟수. 기존 message_index/memo 참조도 자체 해결 |

Host 질문·답변·추가 지시·접수/적용/거부와 Memo 원문·출처는 기존 이벤트에 보존한다.
Host 질문은 tool_execution_id/llm_call_id와 question_id로 연결한다.
Memo를 별도의 이중 이벤트로 다시 발행하지 않는다. 최종 Context에도 같은 Memo를 포함한다.

### 실제 관측 범위

공통 요청과 Adapter 요청은 별개의 경계다. OpenAI는 도구 인자를 JSON 문자열로 변환하고,
Ollama는 `max_tokens`를 `options.num_predict`로 전달하며 실행용 도구 ID를 생성할 수 있다.
각 경계의 실제 값을 기록하므로 변환 전후를 구분할 수 있다. 과거 요청을 최신 Conversation으로 재조립하지 않는다.

SDK 반환 projection은 content와 공개된 thinking/reasoning, raw tool arguments, 모델·응답 ID·종료 이유,
제공된 토큰 통계·시간 및 명시적인 메타데이터 필드를 보존한다. 임의 SDK 객체 전체 dump를 수행하지 않는다.
OpenAI의 raw argument 문자열과 `_raw` fallback 또는 정상화된 인자를 구분한다.
일반 content와 thinking, tool calls가 동시에 있어도 각각 보존한다.
Adapter 변환 실패 전에 받은 응답도 남고, Core 검증 실패 전에 받은 공통 응답도 남는다.

`llm_call_finished`의 status, response_received, validation_succeeded, artifacts, adapter_observation을 함께 읽는다.
호출 성공과 실행 반영은 다르다. 실제 반영은 `response_recorded`, 폐기는 `response_discarded`와 사유로 확인한다.
압축 응답은 `context_compacted`, 실패·취소 이벤트로 적용 여부를 확인한다.
응답이 없으면 응답 Artifact를 만들지 않는다. 빈/length 응답, 취소 후 수신, 지시 변경으로 폐기한 응답도 상세 자료가 남는다.

Tool의 succeeded/failed/cancelled/skipped/rejected와 executed를 유지한다.
미실행 호출은 요청과 사유만 남기며 dispatch·raw 결과와 시작 이벤트를 만들지 않는다.
예외와 직렬화 오류는 기존 실행용 오류 관찰을 유지하고, 직렬화 실패 전의 원본은 가능한 범위에서 별도 수집한다.
도구 성공 duration은 원본 수집 전에 측정하며 질문 도구의 시간은 Host 답변 대기를 포함한다.
LLM duration은 SDK의 순수 추론 시간과 다르며 관측·정규화를 포함한 라이브러리 호출 경계 시간이다.

## 4. 최종 자료의 독립성과 완전성

`result.tracer`는 실행 객체가 아닌 schema_version=2의 독립 데이터다.
events, artifacts, recording_scope를 함께 내보내므로 result.messages/memos를 별도로 저장할 필요가 없다.
구독자가 없어도 기록하며 Session 참조를 제거한 후에도 읽을 수 있다.
각 `session.result()`와 실시간 Event/Artifact는 독립 사본이다. 중첩 사전을 수정해도 실행 원본·다른 소비자에게 영향이 없다.

| 필드 | 의미 |
|---|---|
| `complete` | Task 로그가 종료 이벤트까지 닫혔음. 성공 또는 모든 원본 무손실을 뜻하지 않음 |
| `details_complete` | 선언한 관찰 범위 내 Artifact에 `unavailable` 수집 실패가 없음 |
| `recording_scope` | 요청·응답·도구의 관측 경계, 기록 수명과 미수집 영역 |
| Artifact `issues` | path, reason, category와 필요한 python_type. excluded와 unavailable 구분 |

JSON 값·선언된 dataclass 필드는 독립 복사한다. tuple은 데이터 배열로 보존하며 Python 실행 객체를 복원하지 않는다.
bytes는 base64, 날짜/시각은 ISO 8601, 비문자열 dict 키는 mapping_entries로 명시적으로 인코딩한다.
순환 참조·비유한 숫자·지원하지 않는 객체·필드 접근 실패는 경로와 사유를 표시하고 `details_complete=False`다.
repr이나 잘린 문자열로 원본 보존을 가장하지 않는다. 본문에 우연히 `$unavailable`이라는 키가 있어도 수집 오류로 오인하지 않는다.

연결 옵션의 API Key, Authorization, Cookie 등 인증 키는 제외하고 `excluded` 경로를 기록한다.
Client 객체·접속 인증정보 자체는 관측 인자로 넘기지 않는다. messages·도구 스키마·extra_body의 본문은 임의 마스킹하지 않는다.
제외된 인증정보는 recording_scope 밖이므로 이것만으로 details_complete=False가 되지는 않는다.

`from_dict/from_json`은 schema 1/2를 읽고 Task·sequence·명시적 Artifact 참조를 확인한다.
schema 1에는 상세 원본/상태 이력이 없을 수 있으므로 상세 완전성을 주장하지 않는다.
`state(at_sequence=...)`는 초기·변경·종료 Event에서 과거 상태를 재구성한다. 도구/LLM 실행이나 Resume는 수행하지 않는다.

## 5. Host API 예시

```python
import asyncio
from pathlib import Path
from shs_agent import ProblemSolver, TracerSnapshot

async def observe(session):
    state = await session.state()
    print(state.task.status, state.agent.activity, state.last_event_sequence)
    if state.agent.operation_artifact_id is not None:
        current_input = await session.artifact(state.agent.operation_artifact_id)
        print(current_input.kind, current_input.data)
    async for event in session.events(after_sequence=state.last_event_sequence):
        print(event.sequence, event.timestamp, event.type)
        # 상세 화면을 열 때 필요한 자료만 조회해도 됩니다.
        for artifact_id in event.artifact_ids:
            detail = await session.artifact(artifact_id)
            print(detail.kind, detail.issues)

async def run(provider, model, registry):
    session = await ProblemSolver(provider).start("작업 목표", model=model, registry=registry)
    monitor = asyncio.create_task(observe(session))
    # Host 입력 흐름은 별도로 session.answer/send_instruction/cancel 호출.
    result = await session.result()
    await monitor
    path = Path(f"{result.task_id}.trace.json")
    path.write_text(result.tracer.to_json(indent=2), encoding="utf-8")
    return path

# Session/Provider 없이 저장된 자료 읽기
def inspect_saved(path):
    trace = TracerSnapshot.from_json(Path(path).read_text(encoding="utf-8"))
    print(trace.complete, trace.details_complete, trace.state().task.status)
    for event in trace.events:
        if event.type == "tool_finished":
            outcome = trace.artifact(event.payload["result_ref"]["artifact_id"])
            raw_id = outcome.data["raw_result_artifact_id"]
            if raw_id is not None:
                print(trace.artifact(raw_id).data, outcome.data["llm_result"])
        if event.type == "state_changed":
            print(trace.state(at_sequence=event.sequence))
```

현재 상태를 먼저 표시하는 Host가 과거 이력도 필요하면 `events()`를 0부터 별도로 재조회한다.
상태 cursor 이후 이벤트만 읽는 것은 과거 이벤트를 다시 표시하지 않는 연결 방식이다.
질문 UI와 모니터 구독은 Host에서 별도 흐름으로 운영할 수 있다.

## 6. 검증 결과

환경: Windows, Python 3.12.9, unittest/Fake Provider/Fake Tool/Mock SDK.
기존 sandbox의 asyncio socketpair 대기 문제로 승인된 실행 환경에서 테스트했다.

| 검증 | 결과 |
|---|---|
| 수정 전 전체 자동 테스트 | 기존 70개 통과 |
| 최종 전체 자동 테스트 | 91개 통과: 기존 70개 + 신규 21개 |
| `uv build` | sdist와 sdist 기반 wheel 생성 성공 |
| `tests/verify_distribution.py` | 배포물과 현재 소스·테스트 바이트 일치, old/_deprecated 및 폐기 모듈 제외 확인 |
| wheel 격리 실행 (`-I -S`) | 전체 91개 통과 |
| sdist 격리 실행 (`-I -S`) | 전체 91개 통과 |
| 경량 import | 선택 SDK·SQLAlchemy·torch·보관 Agent를 로드하지 않고 실행 |
| 기존 자료 보존 | 보관 Agent·이전 보고서·과거 아키텍처 16개 파일 해시 동일, 목표 문서 18절 이후 본문 동일 |

기존 두 Tracer 검증은 이번 새 요구에 맞게 확장했다. thinking과 큰 결과 뒷부분이 **Event**에 반복 저장되지 않는지
검증하는 의도를 유지하면서, 이제 **Artifact**에는 온전히 남는지도 확인한다. 전체 Snapshot에서 없다고 주장하던
이전 단계의 부재 검사만 새 수집 계약으로 바꾸었다. 기존 실행·취소·도구 오류 검증은 유지했다.

신규 검증은 상태+cursor 이후 누락 없는 재조회, 모든 상태 시점의 재생, 실행 중 Artifact 접근·수정 격리,
취소 접수와 진행 중 도구 활동, Host 질문 대기, 공개 thinking/content/calls 동시 보존, 과거 요청/응답의 외부 수정 격리,
중간 Summary·큰 도구 결과 보존, 수신/검증/반영의 구분, 직렬화 실패 표시, 미실행·한도 종료,
Session 해제 후 독립 자료, JSON round-trip·참조 무결성, 공유 동기 Provider의 호출별 hook 격리를 다룬다.
Adapter Mock은 실제 SDK 인자 변환·응답 메타데이터·인증정보 제외·변환 오류 이전의 원응답 보존을 검증한다.

작업 시작 시 보관 디렉터리가 이미 `_deprecated/`였고 설정에는 `old/`만 배포 제외되어 있었다.
내용은 수정하지 않고 두 이름 모두 wheel/sdist에서 제외하도록 패키징과 검증을 보강했다.
런타임 의존성은 추가하지 않았고 기존 의존성 설정도 유지했다.

### 이 PC의 실제 Ollama 검증

사용자 요청에 따라 로컬 `http://localhost:11434`의 두 설치 모델을 실제 호출했다.
`tests/verify_ollama_tracer.py`로 순차 실행했으며 모델 다운로드 없이 기존 SDK를 사용했다.
temperature=0, max_tokens=2048, think=True, max_steps=5, max_tool_calls=5로
`add_numbers(a=19, b=23)` 실행 → 반환 결과 읽기 → 단독 `task_complete(output="42")`를 검증했다.

| 모델 | 결과 | LLM 판단 / 도구 실행 | Event / Artifact | 공개 thinking 길이 | 관찰 소요 시간 |
|---|---|---|---|---|---|
| `gemma4:e4b` | completed, `42` | 2 / 2 | 43 / 20 | 248자, 224자 | 55.657초 |
| `qwen3.5:9b` | completed, `42` | 2 / 2 | 43 / 20 | 169자, 97자 | 10.914초 |

각 두 호출의 prompt/completion token은 Gemma `(646,99), (786,86)`, Qwen `(902,92), (1033,55)`였다.
두 모델 모두 다섯 LLM 경계 Artifact가 남았고 `complete=True`, `details_complete=True`였다.
실시간 Event와 최종 Event 일치, 이벤트 수신 시 Artifact 조회, 현재 상태 조회, JSON 재읽기와 최종 상태 일치를 확인했다.
소요 시간은 모델 로딩과 도구·기록 처리를 포함한 해당 실행의 관측값이며 모델 성능 비교 실험이 아니다.

생성 자료는 Git에서 제외되는 작업용 `build/ollama_state_tracer/`에 남겼다:
`summary.json`, `gemma4_e4b.json`, `qwen3_5_9b.json`.
실제 OpenAI/vLLM 서버는 호출하지 않았고 OpenAI Adapter는 Mock으로 검증했다.

## 7. 확인된 제약과 후속 연결 경계

1. 상세 기록은 인메모리다. 호출마다 전체 요청과 관측 경계별 응답, 원본 도구 결과, 마지막 Context를 보존하므로
   이벤트 전용 단계보다 RAM·사본 생성 비용이 늘어난다. 입력이 반복되는 긴 작업은 요청 크기의 누적 비용을 가진다.
   임의 삭제·절단·TTL·DB 자동 영속화를 추가하지 않았다. 프로세스 종료 후 보려면 Host가 내보내기를 저장해야 한다.
2. HTTP 전송 bytes, SDK 내부 재시도/내부 상태, 공개하지 않은 모델 추론은 수집하지 않는다.
   사용자 Provider는 공통 경계까지 보장하며 Adapter hook이 없으면 `adapter_observation="unavailable"`이다.
   도구도 SessionTools/executor 반환 경계이며 도구 내부 파일·네트워크 부작용이나 내부 하위 호출을 자동 추적하지 않는다.
3. 선언된 응답 projection 안의 지원 불가 SDK 값은 수집 실패 경로로 표시한다. Python 객체 그래프·클래스 정체성을
   직렬화해 실행 가능한 객체로 복원하는 기능은 아니다. 미제공 usage/thinking/비용을 생성하지 않는다.
4. 공개 취소는 기존 협력적 정책을 유지한다. Runtime 자체가 강제 취소된 뒤 살아 있는 동기 worker가 뒤늦게 반환한 값은
   닫힌 Snapshot에 추가하지 않는다. 그 호출은 cancelled와 당시 수신 여부로 남으며 이 수명 경계는 recording_scope에도 표시한다.
5. Planning은 실제 Plan/revision Artifact와 변경 Event를 같은 경로에 연결할 수 있다. Sub-agent는 Task별 sequence를
   유지하며 부모/Root 관계·요청 식별·별도 집계 순서를 연결해야 한다. 현재는 관계나 가상 Task, 빈 확장 모듈을 만들지 않았다.
   부모 완료·자손 취소·Root Snapshot 확정 등 미확정 정책은 그대로 남겼다.

UI, Planning/Replanning, Sub-agent, Agent 통신, Root 집계, Workspace, Storage 자동 연결,
checkpoint/Resume, 토큰 스트리밍, 장기 Memory를 구현하지 않았다.
기존 미커밋 변경과 보고서를 보존했고 commit/push하지 않았다. 이번 State·상세 Tracer 보완에서 작업을 종료한다.
