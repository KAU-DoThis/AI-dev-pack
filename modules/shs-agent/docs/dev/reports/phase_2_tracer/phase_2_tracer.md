# shs-agent 2단계: 단일 Task Tracer 구현 보고서

작성일: 2026-10-17 (Asia/Seoul).

단일 Task의 실제 실행 이벤트를 실시간 구독과 최종 결과에서 같은 데이터로 조회하도록 구현했다.
`TaskResult.tracer`는 마지막 `finished` 이벤트까지 포함한 독립적인 `TracerSnapshot`이다.
이번 작업은 2단계에 한정하며 Planning/Sub-agent, UI, Storage 자동 영속화는 구현하지 않았다.

## 1. 조사 및 기존 작업 보존

- 목표 아키텍처, 현재 TaskRuntime/TaskSession/TaskChannel/AgentEvent/TaskResult,
  Conversation/Memo/Compaction/ContextBuilder, Provider·Tool 계약을 확인했다.
- 루트 `report.md`는 존재하지 않아 현재 보고서인 `docs/reports/phase_1/report.md`를 확인했다.
  기존 보고서는 수정하지 않고 이번 보고서를 별도 디렉터리에 작성했다.
- 구현 전에 기존 테스트 53개가 통과하는 것을 확인했다.
- 시작 시 존재한 미커밋 구조 변경을 기반으로 작업했다. 기존 변경을 되돌리거나 staging하지 않았다.
  `agent/old/`, 과거 아키텍처 보관본 v001/v002, 기존 보고서는 수정하지 않았다. commit/push하지 않았다.
- 현재 목표 아키텍처에서 실행 추적 모듈 명칭을 `Tracer`로 통일하고 반환 데이터를
  `Tracer Snapshot`으로 구분했다. 관련 디렉터리·반환 예시를 맞췄으며 다른 설계·미확정 정책은 유지했다.

## 2. 최종 구조와 파일별 책임

| 파일 | 책임 |
|---|---|
| `src/shs_agent/agent/tracer/models.py` | 기존 AgentEvent 계약 확장, UTC timestamp, event_id, 데이터 전용 TracerSnapshot, dict/JSON 내보내기 |
| `src/shs_agent/agent/tracer/tracer.py` | Task별 단일 append-only 이벤트 로그, sequence 부여, 개별 이벤트 사본 조회, 종료 및 스냅샷 생성 |
| `src/shs_agent/agent/tracer/__init__.py` | AgentEvent/Tracer/TracerSnapshot export |
| `src/shs_agent/agent/session/channel.py` | 기존 명령함·질문·구독 유지, 모든 emit을 소유한 Tracer에 기록, 구독자 알림 |
| `src/shs_agent/agent/runtime/runner.py` | 실제 판단·요약 LLM 호출, 도구 실행, 상태·한도·응답 폐기 시점의 이벤트와 측정값 발생, 종료 후 스냅샷 연결 |
| `src/shs_agent/agent/tools/session_tools.py` | Host 질문을 해당 도구 실행 참조와 연결. Memo 이벤트 payload는 기존 형식 유지 |
| `src/shs_agent/agent/context/conversation.py` | 원본 인덱스 참조를 위한 길이 조회 추가. 기존 기록·검색·읽기·압축 동작 유지 |
| `src/shs_agent/agent/types.py` | AgentEvent의 기존 import 경로 유지, TaskResult에 tracer 필드 추가 |
| `src/shs_agent/__init__.py`, `src/shs_agent/agent/__init__.py` | 기존 공개 API를 유지하면서 Tracer/TracerSnapshot export 추가 |
| `tests/test_tracer.py` | Fake Provider/Tool 기반 17개 신규 테스트 |
| `tests/verify_distribution.py` | wheel/sdist 소스·테스트 바이트 비교, 두 배포물 각각의 격리 테스트 |

`TaskSession`의 `start/result/events/send_instruction/answer/cancel` 인터페이스는 그대로다.
`AgentEvent(task_id, sequence, type, payload)` 위치 인자 및 기존 import도 유지한다.
직접 생성하는 과거 `TaskResult` 코드의 호환성을 위해 `tracer`의 기본값은 `None`이다.
Solver/Runtime이 반환하는 결과에는 성공·실패·취소·한도·미완료 모두 스냅샷이 들어간다.

## 3. TaskChannel과 연결한 단일 기록 경로

```text
Runtime / Channel / SessionTools에서 실제 사건 발생
    → TaskChannel.emit(type, payload)
    → Task별 Tracer.record(..., step=현재 판단 step)
    → sequence 및 UTC timestamp 부여, payload 사본 보관
    → TaskChannel의 기존 asyncio.Event로 구독자 깨우기
        → session.events()의 각 커서가 같은 Tracer에서 독립적인 이벤트 사본 읽기

Runtime 종료
    → 최종 status_changed 및 finished 기록
    → Tracer.close(), Channel 종료
    → TracerSnapshot 생성
    → TaskResult.tracer에 연결
    → session.result()가 소비자마다 전체 결과의 독립적인 사본 반환
```

기존 Channel의 `_journal`을 Tracer가 소유한 로그로 대체했다. 별도 이벤트 버스,
구독자별 저장 큐, 최종 결과에서 새로 조립하는 가상 이벤트 이력은 없다.
구독자가 없어도 동일한 경로로 기록하며, `after_sequence`와 재조회 유효성 검사를 유지한다.
구독자별 커서는 독립적이고 이벤트도 사본이다. 한 소비자의 수정·중단이 다른 구독에 영향을 주지 않는다.

emit은 구독자 처리나 UI 렌더링 완료를 기다리지 않는다. 다만 같은 asyncio 루프에서 Host가
동기적으로 긴 계산을 수행하면 루프 전체가 지연되는 일반적인 제약은 남는다.

## 4. 이벤트 모델과 기록 의미

### 공통 필드

| 필드 | 의미 |
|---|---|
| `task_id` | 현재 단일 Task의 식별자 |
| `sequence` | 1부터 증가하는 Task별 기록 순서. 재생·재조회 기준 |
| `timestamp` | timezone을 포함한 UTC ISO 8601 문자열, 마이크로초 자리 표현 |
| `type` | 기존 이벤트 이름 또는 새 관측 이벤트 이름 |
| `step` | 현재까지 시작한 일반 판단 step. 시작 전은 0, 해당 step의 도구도 같은 값 |
| `payload` | 해당 사건의 기존 주요 필드, 측정값과 참조 |
| `event_id` | `task_id:sequence`로 얻는 안정적인 속성. `to_dict()`에도 포함 |

시계가 뒤로 조정되어도 순서는 sequence를 따른다. 호출의 `duration_ms`는 `perf_counter()`로
측정하므로 wall clock 차이를 사용하지 않는다. UTC 문자열의 소수 자릿수가 실제 시계 정확도를 뜻하지는 않는다.

요약 LLM은 `purpose="context_compaction"`으로 구분하며 판단 횟수에는 추가하지 않는다.
예를 들어 2번째 판단 후 3번째 판단 전에 수행하는 압축의 step은 2다.

### 기록 대상

| 대상 | 이벤트 및 주요 정보 |
|---|---|
| Task | 기존 `created`, `status_changed`, `finished`; `started`에 모델 기본값과 실행 한도. 상태 변경에 previous_status 추가 |
| 판단 | 기존 `step_started`에 llm_call_id 추가 |
| LLM | `llm_call_started`, `llm_call_finished`: 호출 ID, purpose, 실제 선택 모델, Provider 클래스명, 시작/종료·오류, duration_ms, 제공된 사용량 |
| 응답 반영 | `response_recorded`에 Conversation 원본 위치, 기존 `message`에 content와 원본 위치, `response_discarded`에 호출 ID와 폐기 이유 |
| 도구 | 기존 `tool_started`, `tool_finished`: tool_call_id/name/result 유지, tool_execution_id·LLM 호출 ID·인자/결과 참조·실행 여부·상태·소요 시간 추가 |
| Host 통신 | 기존 `command_received/applied/rejected`, `question/question_closed`; 명령 본문은 received에만 추가, 질문에는 도구 실행 참조 |
| Context | 기존 `context_compaction_started/context_compacted`에 LLM 호출 연결·소요 시간, 실패/취소 시 별도 `context_compaction_failed/cancelled` |
| Memo | 기존 `task_memo_written` payload 그대로 유지. memo_id와 공통 step으로 연결 |
| 실행 한도 | `limit_reached`: 한도명, 설정값, 당시 실행 카운터 |
| 최종 결과 | 기존 `finished`의 상태·이유·출력·통계·경고 유지 |

LLM 종료의 `status`는 `succeeded/failed/cancelled`다. 응답 검증도 호출 경계에 포함된다.
정상 응답이어도 새 Host 지시나 취소로 적용하지 않을 수 있으므로, 호출 성공과 응답 반영을 별도 사건으로 구분한다.
`duration_ms`는 라이브러리가 관찰한 호출 시간이며 순수 서버 추론 시간으로 해석하지 않는다.
사용량은 Provider가 제공한 유한한 숫자와 중첩 숫자 사전만 보관한다. 미제공 값은 `None`이며 0으로 추정하지 않는다.
문자열·객체·비유한 수·순환 참조·16단계를 넘는 중첩은 사용량 측정값에서 제외한다.
Provider 설정·인증정보·전체 요청·응답 객체·`thinking`을 Tracer의 별도 기록 필드로 수집하지 않는다.
Conversation 자체의 기존 원본 보존 계약은 바꾸지 않았다.

도구 종료의 `status`는 `succeeded/failed/cancelled/skipped/rejected`다.
실행 전에 차단되거나 한도 때문에 생략된 호출에는 `tool_started`를 만들지 않으며,
`executed=False`, `duration_ms=None`과 기존 오류 결과를 기록한다.
일반 도구 예외·직렬화 오류는 기존처럼 관찰 결과가 되어 LLM이 후속 판단할 수 있다.
Host 질문 도구의 시간에는 답변 대기도 포함된다.

협력적 취소는 진행 중인 외부 작업을 기다리는 기존 정책을 유지한다.
취소 요청 후 실제 완료된 도구는 성공 기록을 남기고 나머지 호출은 미실행 처리한다.
진행 중인 질문은 중단 이유를 남긴다. Runtime 자체의 취소는 호출 종료를 cancelled로 기록한다.
이후 실행 여부나 LLM의 판단은 Runtime이 결정하며 Tracer는 관여하지 않는다.

### 큰 데이터와 참조

- `arguments_ref = {message_index, tool_call_id}`는 `TaskResult.messages[message_index]`의
  해당 도구 호출을 가리킨다. Provider가 다른 판단에서 같은 call ID를 재사용해도
  llm_call_id/tool_execution_id/원본 인덱스로 구분할 수 있다.
- `result_ref = {message_index}`는 원본 tool 메시지를 가리킨다.
  기존 `tool_finished.result`는 `max_tool_output_chars` 한도 내의 표시용 사본이다.
- `conversation_ref = {message_index}`는 실제 기록된 응답을 가리킨다.
  폐기한 응답에는 존재하지 않는 원본 참조를 만들지 않는다.
- 압축의 `from_message/to_message`는 원본 Conversation의 반개구간이다.
  Memo는 `memo_id`로 `TaskResult.memos`에서 찾는다.
- 원본 인덱스는 0부터 시작하며 같은 Task 결과 안에서 해석한다.
  전체 Conversation과 큰 도구 결과를 매 이벤트에 중복 넣지 않는다.

## 5. Host 사용 예시

### 실시간 관찰과 결과 수집

```python
import asyncio
from shs_agent import ProblemSolver

async def observe(session):
    async for event in session.events():
        print(event.sequence, event.timestamp, event.step, event.type, event.payload)

async def run(provider, model, registry):
    session = await ProblemSolver().start(
        "작업을 수행해줘.", provider=provider, model=model, registry=registry,
    )
    observer = asyncio.create_task(observe(session))
    # Host 입력 경로는 별도로 session.answer/send_instruction/cancel을 호출한다.
    result = await session.result()
    await observer

    print(result.output)
    for event in result.tracer.events:
        print(event.type, event.payload)
    return result
```

질문 UI를 구현하는 Host는 `question.payload.question_id`를 보관하고,
답을 얻은 입력 처리 경로에서 `await session.answer(question_id, text)`를 호출한다.
`request_id/content` 별칭과 기존 질문 timeout도 유지한다.
다른 모니터는 같은 Session에 `events(after_sequence=마지막으로_처리한_sequence)`로 독립적으로 연결할 수 있다.

실시간 관찰이 필요 없으면 `await solver.solve(...)`만 호출해도 같은 `result.tracer`를 받는다.

### 원본 조회와 JSON 저장

```python
import json
from dataclasses import asdict
from pathlib import Path

result = await session.result()
snapshot = result.tracer
assert snapshot.complete

for event in snapshot.events:
    if event.type == "tool_finished":
        index = event.payload["result_ref"]["message_index"]
        original_outcome = json.loads(result.messages[index]["content"])
        print(event.payload["name"], original_outcome)

# 이벤트만 내보내기: Session이나 Provider 객체는 들어가지 않는다.
Path(f"{result.task_id}.trace.json").write_text(snapshot.to_json(indent=2), encoding="utf-8")

# 원본까지 필요한 Host는 같은 Task의 자료를 함께 보관한다.
bundle = {
    "task_id": result.task_id,
    "tracer": snapshot.to_dict(),
    "messages": result.messages,
    "memos": [asdict(memo) for memo in result.memos],
    "context_summary": result.context_summary,
}
Path(f"{result.task_id}.json").write_text(
    json.dumps(bundle, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8",
)
```

`TracerSnapshot`에는 `schema_version=1`, task_id, events, complete가 있다.
complete는 이 Task의 종료 이벤트까지 기록했다는 뜻이며 Task 성공 여부를 뜻하지 않는다.
반환 모델은 frozen dataclass/tuple이지만 중첩 payload는 소비자가 편집할 수 있는 독립 사본이다.
그 사본을 수정해도 원본 로그, 다른 구독자, 다음 `session.result()` 반환값은 바뀌지 않는다.
Session 참조가 제거된 후에도 스냅샷을 읽거나 내보낼 수 있다.

## 6. 검증 결과와 확인된 제약

환경: Windows, 프로젝트 `.venv`의 Python 3.12.9, Fake Provider/Tool, 표준 라이브러리 unittest.

| 검증 | 결과 |
|---|---|
| 구현 전 `python -m unittest discover -s tests -v` | 기존 53개 통과 |
| 신규 `python -m unittest tests.test_tracer -v` | 17개 통과 |
| 최종 전체 테스트 | 기존 53개 + 신규 17개 = 70개 통과 |
| `uv build` | sdist 생성 및 sdist에서 wheel 빌드 성공 |
| `python tests/verify_distribution.py` | 소스/테스트 바이트 일치, old·폐기 모듈 제외 확인 |
| wheel `-I -S` 격리 테스트 | 70개 통과 |
| sdist 소스·테스트 `-I -S` 격리 테스트 | 70개 통과 |
| 경량 import | site-packages 없이 성공, torch/ollama/openai/sqlalchemy 및 old 미로드 |
| 문서·차이 검사 | `git diff --check` 통과, 목표 문서의 18절 실행 정책은 Tracer 명칭 외 동일함 확인 |

최초 샌드박스 테스트는 첫 asyncio 테스트에서 진행되지 않아 중단하고,
승인된 샌드박스 밖 실행으로 기준선과 최종 결과를 검증했다. 빌드도 승인된 실행으로 완료했다.
중간 전체 테스트에서는 Memo payload 전체 비교 1건이 추가 메타데이터로 실패했다.
해당 payload를 기존 형태로 유지하도록 수정했고 기존 테스트를 변경하지 않은 채 최종 전체 검증을 통과했다.

신규 테스트에서 확인한 내용:

- 연속 sequence, UTC timestamp, 시계 역행에도 sequence 순서 유지, 단조 시계의 실제 경과 시간 단위.
- LLM/Tool 시작·종료 연결, 모델 선택, 제공된 숫자 사용량, 비공개 thinking 비수집.
- 성공·Provider 오류·검증 실패·빈 응답·미완료·도구 오류·큰 결과·직렬화 오류·한도 종료.
- 실행 전 취소, LLM/도구/질문/압축 중 취소, Runtime 자체 취소, 미실행 도구의 구분.
- 질문·답변·거부·추가 지시·중복 명령·응답 폐기·Memo·압축 성공/실패의 연결.
- 다중 실시간 구독, 중간 cursor 재조회, 느린 구독자, 실시간과 최종 스냅샷의 전체 이벤트 동일성.
- 구독자 없는 완료, JSON 내보내기, 소비자 간 중첩 데이터 독립성, Session 해제 후 스냅샷 유지.
- 동시에 진행하는 Task와 Provider가 재사용한 tool_call_id 사이의 기록 격리.

제약:

1. 단일 Task의 인메모리 기록이다. 기록 수에 비례해 메모리가 증가하며 자동 삭제·파일 저장·DB 저장은 없다.
   무제한 크기의 실행에 대한 RAM 보장은 하지 않는다. 프로세스 종료 후 조회하려면 Host가 저장해야 한다.
2. 스냅샷 JSON에는 이벤트와 참조가 들어간다. 참조된 원본을 복원하려면 같은 결과의 messages/memos도 보관해야 한다.
   실시간 Session에는 새 원본 조회 API를 추가하지 않았다. 기존 Conversation 도구와 최종 결과의 조회 계약을 유지한다.
3. Provider가 제공하지 않은 토큰·서버 시간·비용을 생성하지 않는다. LLM 입력 전체나 중간 Summary 본문,
   폐기한 응답 본문을 추가로 수집하지 않는다. 압축 범위와 적용 여부는 재구성하지만 과거 Summary 본문 전체는 복원하지 않는다.
4. Tracer는 실행 상태를 재개하는 checkpoint가 아니다. 강제 프로세스 종료에서는 최종 결과를 받을 수 없으며,
   실행 중인 동기 Python 스레드를 강제로 종료하는 보장도 추가하지 않았다.
5. 실제 OpenAI/Ollama 서버·GPU·DB 종단간 검증은 수행하지 않았다. 외부 의존성과 GPU 라이브러리를 추가하지 않았다.

## 7. 후속 Planning/Sub-agent 연동 시 확장할 부분

- Planning/Replanning은 실제 계획 생성·revision 변경 시점에 같은 emit 경로로 사건을 기록하고
  이전/이후 revision 및 원인이 된 event_id를 연결할 수 있다.
- Sub-agent는 task_id별 sequence를 유지하면서 부모/Root 관계와 별도의 Root 집계 순서를 연결해야 한다.
  서로 다른 Task의 sequence를 한 타임라인의 인과 순서로 해석해서는 안 된다.
- Agent 통신은 지금의 command/question/tool 참조처럼 요청·응답 ID와 발신/수신 Task를 연결해야 한다.
- Root Snapshot에는 기록 범위와 자식 종료 여부, 원본 참조 수명을 명시해야 한다.
  부모 완료·자손 취소·집계 완료 정책은 목표 아키텍처의 미확정 사항으로 남긴다.
- 저장소 연동은 현재 스냅샷/참조 계약을 출발점으로 보존·내보내기 정책을 별도 결정해야 한다.

위 기능을 위한 빈 모듈이나 임시 실행 정책은 추가하지 않았다. 이번 작업은 단일 Task Tracer의 2단계에서 종료한다.
