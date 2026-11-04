# shs-agent 현재 구현 상태 조사 보고서

조사일: 2026-10-17 (Asia/Seoul)  
대상: `H:/shs/projects/shs-agent`  
기준 HEAD: `12f9f890a1ecdab390c2a68a45d937374d51e196`

## 조사 범위와 판정 기준

프로젝트 소스인 `src/shs_agent`의 Python 파일 32개, 총 2,833줄과 루트의 `test.py`, `examples/test_db.py`, 패키징 설정, README, 로컬 설치 메타데이터를 읽었다. 가상환경의 외부 라이브러리 전체나 Git 객체는 프로젝트 소스에 포함하지 않았다. `docs/`는 비어 있다. 작업 시작 시 Git에 표시되는 미커밋 변경과 기존 `report.md`는 없었다.

목표 아키텍처는 이번 요청에 제시된 설명을 기준으로 삼았다. README나 주석의 의도만으로 기능이 구현되었다고 판정하지 않았다.

이번 조사는 **정적 코드 조사**다. 프로젝트 모듈 import, 예제 실행, pytest, 실제 LLM 요청, DB 연결, 설치·빌드·성능 측정은 수행하지 않았다. 아래의 “구현 완료”는 해당 범위의 구체적인 구현과 호출 경로가 코드에서 확인되었다는 의미이며, 실행 테스트 통과 또는 운영 안정성 보장을 뜻하지 않는다. 런타임 오류 가능성은 재현 결과와 구분하여 설명한다. 보고서의 Python 예시도 실행하지 않았다.

| 상태 | 이 보고서에서의 의미 |
|---|---|
| 구현 완료 | 실제 처리 코드와 해당 기능으로 이어지는 호출 경로가 확인됨. 실행 검증 여부는 별도 설명 |
| 부분 구현 | 일부 경로만 있거나, 필요한 연결·제어 기능이 빠져 있음 |
| 인터페이스만 존재 | 해당 선언 자체에는 실행 코드가 없음. 별도의 구체 구현 존재 여부를 함께 표시 |
| 미구현 | 조사한 전체 프로젝트 소스에 해당 기능의 선언·처리 경로가 없음 |
| 확인 불가 | 외부 서버, 설치 환경, 실제 실행·성능 등 코드 읽기만으로 판단할 수 없음 |

## 1. 현재 전체 구현 상태 요약

현재 shs-agent는 **외부 Provider와 Host 도구를 주입받아 한 Task의 LLM↔도구 루프를 수행하는 인메모리 비동기 라이브러리**로 구현되어 있다. `ProblemSolver.create_task()` → `TaskSession.start()` → `TaskRuntime.run()` → `AgentCore.step()` 연결이 있으며, Host가 이벤트를 구독하고 질문에 답하거나 추가 지시·취소를 전달하는 경로도 연결되어 있다. [생성 진입점][solver], [세션 API][session], [실행 루프][rt-loop]

코드상 가능한 범위는 다음과 같다.

- Host가 Provider, 모델, Registry, Executor를 제공하면 LLM 요청, 복수 도구의 순차 실행, 도구 결과의 다음 문맥 반영을 반복한다.
- `host_ask`로 답변을 기다리고, `memory_write`로 Task 내부 메모리를 남기며, 오래된 문맥을 요약하고 기존 transcript를 검색·부분 조회한다.
- 단독 `task_complete` 호출이 유효하게 처리되면 완료된다. 일반 assistant 답변은 중간 메시지다. 완료 여부를 독립적으로 검사하는 평가기는 없으므로 `COMPLETED`는 목표 달성의 외부 검증 결과가 아니다.
- 여러 Session은 각자의 메모리·명령함·이벤트 목록을 가진다. 다만 같은 Solver의 Provider 및 Host가 재사용한 Registry/Executor는 공유될 수 있다.
- Storage 패키지는 SQL 저장 기능을 별도로 제공한다. **Solver/Runtime과는 연결되어 있지 않으므로 Task 실행이 자동 저장되거나 복구되지 않는다.** [Storage 구현][sql], [Runtime 초기화][rt-init]

현재 라이브러리만으로 완성된 Windows 코딩 에이전트가 제공되지는 않는다. 파일 읽기·쓰기·패치, 셸/PowerShell 실행, Workspace·Mount, 권한 승인, UI, 장기 기억, DB 기록 연결은 Host가 제공해야 한다. 이는 Host가 환경과 도구를 소유한다는 목표와 상당 부분 부합하지만, 공통 실행 복구·Pause/Resume·명시적인 실행 중 모델 변경·Planning은 아직 제공되지 않는다. [도구 실행기][executor], [세션 공개 메서드][session], [작업 입력][types]

**실행 가능성의 현재 한계:** 실제 Ollama/OpenAI/vLLM 서버와의 성공 여부, 동시 실행 안정성, Windows에서의 종단간 작업 성공 및 저사양 성능은 이번 조사로 확인하지 않았다. 로컬 `.venv`에는 Ollama/OpenAI SDK 디렉터리가 있지만 SQLAlchemy·pytest·DB 드라이버 디렉터리는 발견되지 않았고, 설치 메타데이터도 현재 설정과 다르다. 따라서 “현재 가상환경에서 모든 예제가 바로 동작한다”고 판단할 근거는 없다. 자세한 근거는 4.4 및 6절에 정리했다.

## 2. 실제 디렉터리 구조와 모듈별 책임

### 2.1 파일별 역할

아래 트리는 조사 시점의 실제 구조다. 루트는 `H:/shs/projects/shs-agent`이며 캐시 내부 파일은 묶어서 표시했다.

```text
H:/shs/projects/shs-agent/
├─ pyproject.toml                  패키지·Python 버전·기본/선택 의존성·src 탐색
├─ README.md                       '# shsagent' 한 줄; 사용법 없음
├─ .gitignore                      가상환경·캐시·egg-info·환경 파일 제외
├─ test.py                         실제 Ollama + 계산 도구 + Host 질문 수동 데모
├─ examples/
│  └─ test_db.py                    SQLite 생성·기록·조회 수동 데모
├─ docs/                           빈 디렉터리
├─ src/
│  ├─ shs_agent/
│  │  ├─ __init__.py               루트 API 3개 export, __version__
│  │  ├─ agent/
│  │  │  ├─ __init__.py            Agent API export
│  │  │  ├─ solver.py              Provider 보유, Session/Runtime 생성
│  │  │  ├─ session.py             Host 명령 접수, 이벤트 replay, 실행/결과 핸들
│  │  │  ├─ runtime.py             Task 루프·종료·도구·Host·메모리·문맥 압축 조율
│  │  │  ├─ core.py                Provider/Tool Protocol, sync/async 호출, 응답 검증
│  │  │  ├─ types.py               입력·제한·상태·이벤트·결과·WorkingMemory 모델
│  │  │  ├─ context.py             LLM 문맥 view, 문자량 추정, 압축 경계·요약 프롬프트
│  │  │  ├─ tools_depricated.py    이전 SessionTools/스키마 사본; 현재 루프에서 미사용
│  │  │  └─ tools/
│  │  │     ├─ __init__.py         현재 SessionTools export
│  │  │     ├─ session_tools.py    Host Registry와 Task 전용 5개 도구 결합·분기
│  │  │     └─ schemas.py          host_ask/memory_write/conversation_search/
│  │  │                           conversation_read/task_complete 스키마
│  │  ├─ llm/
│  │  │  ├─ __init__.py            BaseLLM·옵션·응답·Registry export
│  │  │  ├─ base.py                BaseLLM ABC, 공통 function tool 스키마 변환
│  │  │  ├─ types.py               ChatOptions, LLMResponse, ToolCall
│  │  │  ├─ registry.py            ProviderSpec 등록·설치 검사·동적 생성
│  │  │  └─ provider/
│  │  │     ├─ __init__.py         빈 파일
│  │  │     ├─ ollama.py           Ollama SDK 요청·메시지/응답 변환
│  │  │     └─ openai.py           OpenAI 호환 Chat Completions 요청·변환
│  │  ├─ tool/
│  │  │  ├─ __init__.py            Tool·ToolSpec·Registry·Executor export
│  │  │  ├─ model.py               ToolSpec 데이터와 Python handler 결합
│  │  │  ├─ registry.py            이름별 등록·교체·삭제·스키마·version
│  │  │  ├─ executor.py            이름 조회 후 handler(**arguments) 호출
│  │  │  └─ builtin.py             echo/add 및 ECHO_TOOL/ADD_TOOL; 자동 등록 안 됨
│  │  └─ storage/
│  │     ├─ __init__.py            Storage·Record·3개 DB Backend export
│  │     ├─ base.py                비동기 Storage Protocol
│  │     ├─ model.py               TaskRecord/StorageRecord, 입력 검증·검색용 텍스트
│  │     └─ provider/
│  │        ├─ __init__.py         SQLite/PostgreSQL/MySQL 구현 export
│  │        ├─ _sqlalchemy.py      테이블 정의와 공통 SQL 처리
│  │        ├─ sqlite.py           파일 경로·SQLite 연결·PRAGMA
│  │        ├─ postgresql.py       psycopg DSN 변환·Engine 생성
│  │        └─ mysql.py            PyMySQL DSN 변환·Engine 생성
│  └─ shs_agent.egg-info/          오래된 로컬 빌드 메타데이터; Git 제외
│     ├─ PKG-INFO                 이전 배포 메타데이터
│     ├─ requires.txt             이전 선택 의존성 목록
│     ├─ SOURCES.txt              이전 소스 목록; 현재 없는 agent/tools.py 포함
│     ├─ top_level.txt            shs_agent 패키지 이름
│     └─ dependency_links.txt     실질 내용 없음
├─ .venv/                          로컬 Python/설치 패키지; 프로젝트 구현 아님
├─ .git/                           Git 관리 데이터
└─ **/__pycache__/                생성된 바이트코드; 구현 판단은 .py 원문 기준
```

별도의 Workspace, Mount, 파일 도구, CLI/Server 진입점, 테스트 전용 디렉터리, MindMap, Life Agent 모듈은 없다. `report.md`는 이 조사로 새로 추가한 파일이다.

### 2.2 공개 API와 내부 구현

| 경로/경계 | 실제 API | 성격 및 사용상 주의 |
|---|---|---|
| [루트 패키지][root-api] | `ProblemSolver`, `TaskSession`, `TaskResult` | `TaskRequest`와 `AgentLimits`는 루트 export에 없음 |
| [agent 패키지][agent-api] | 위 3개 + `AgentCore`, `AgentEvent`, `AgentLimits`, `TaskRequest`, `TaskStatus`, `TaskMemory` | 공개적으로 export된 기능. 고수준 진입점은 `ProblemSolver` |
| [TaskSession][session] | `task_id`, `status`, `done`, `start()`, `result()`, `events()`, `send_instruction()`, `answer()`, `cancel()` | Host용 핸들. 직접 생성하면 Runtime이 없어 `start()` 실패; Solver로 생성해야 함 |
| [llm 패키지][llm-api] | `BaseLLM`, `ChatOptions`, `LLMResponse`, `ToolCall`, `ProviderSpec`, `LLMProviderRegistry` | Provider 구현 클래스는 각 `llm.provider.ollama/openai`에서 직접 import |
| [tool 패키지][tool-api] | `Tool`, `ToolSpec`, `ToolRegistry`, `ToolExecutor` | Host가 handler와 스키마를 수동 등록 |
| [storage 패키지][storage-api] | `Storage`, `TaskRecord`, `StorageRecord`, `SQLiteStorage`, `PostgreSQLStorage`, `MySQLStorage` | Host가 독립적으로 호출하는 API. Runtime 자동 저장과는 다름 |
| [Runtime][rt-init], [상태 모델][working-memory], [SessionTools][session-tools] | `TaskRuntime`, `WorkingMemory`, `HostCommand`, `SessionTools`, 문맥 helper | import 자체는 가능하나 고수준 export에 포함되지 않는 내부 구성. `_runtime`, `_commands` 등을 조작하는 방식은 공개 제어 API가 아님 |

`BaseLLM`을 반드시 상속해야 엔진에 연결되는 것은 아니다. `AgentCore`는 최소 `chat()` Protocol과 반환 객체의 필드를 검사한다. 반면 `BaseLLM`은 `models()`까지 요구하는 ABC다. Host는 계약에 맞는 자체 Provider/Registry/Executor도 주입할 수 있다. [Protocol과 invoke][core], [BaseLLM][llm-base]

### 2.3 의존 관계

```text
Host
 ├─ ProblemSolver ── AgentCore ── 주입한 provider.chat()
 │       └─ create_task() ── TaskSession ↔ TaskRuntime
 │                              │            ├─ WorkingMemory / context helper
 │                              │            └─ SessionTools
 │                              │                 ├─ Task 전용 Runtime callback
 │                              │                 └─ Host Registry / Executor
 │                              │                                      └─ handler
 │                              └─ 이벤트·명령·결과
 └─ Storage API ── SQLite/PostgreSQL/MySQL ── SQLStorage ── SQLAlchemy/DB
      ↑ Host가 직접 연결해야 함; TaskRuntime에서 Storage로 이어지는 호출 없음
```

Agent 핵심은 표준 라이브러리와 내부 모듈에 의존한다. 실제 Ollama/OpenAI SDK는 Provider 생성자 안에서 import한다. 반면 `shs_agent.storage` import는 Provider들을 거쳐 SQLAlchemy를 바로 import한다. Session의 Runtime 역참조 import는 `TYPE_CHECKING`에 한정된다. [core.py][core], [session.py][session], [storage export][storage-api]

### 2.4 중복과 책임 경계

- [tools_depricated.py][legacy-tools]에 현재 [tools 패키지][session-tools]와 거의 같은 `SessionTools` 및 4개 스키마가 남아 있다. 이전 사본에는 `task_complete`가 없고, 실제 Runtime은 `from .tools import SessionTools`를 사용한다. 이는 미완성 현재 경로가 아니라 사용되지 않는 별도 사본이다.
- `LLMResponse.to_message()`는 `thinking` 보존까지 구현하지만 Runtime은 assistant 메시지를 직접 만든다. 이 두 직렬화 경로의 차이로 실제 Task transcript에는 `thinking`이 기록되지 않는다. `usage`도 집계·이벤트·TaskResult에 연결되지 않는다. [응답 모델][llm-types], [Runtime 응답 반영][rt-loop]
- `BaseLLM` ABC와 `agent.core.LLMProvider` Protocol은 요구 계약이 다르다. Registry와 Executor도 Protocol/구체 클래스가 나뉜다. 이는 주입 경계로 사용할 수 있으나 문서가 없어 어떤 계약을 구현해야 하는지 Host가 코드를 읽어야 한다.
- `TaskRuntime` 한 클래스가 루프·명령·질문·도구·압축·검색·결과 생성까지 맡는다. 개별 기능이 없는 것은 아니며, 향후 제어 기능이 늘 때 상태 전이의 상호작용을 검토할 부분이다. `TaskSession`과는 private 필드/메서드로 긴밀히 연결되어 있다.
- Runtime transcript 검색과 SQL Storage 검색은 둘 다 존재하지만 대상·검색 규칙이 다르다. 전자는 인메모리 JSON 문자열의 구문/모든 단어 검색, 후자는 저장된 `search_text`의 부분 문자열 검색이다. 둘 사이 동기화는 없다. [대화 검색][rt-search], [SQL 검색][sql-search]

## 3. 현재 코드 기준 Task 실행 흐름

### 3.1 주요 클래스의 실제 메서드와 책임

| 클래스 | 메서드 | 실제 책임 |
|---|---|---|
| `ProblemSolver` | `__init__(provider)`, `create_task(...)` | 공유 `AgentCore` 생성; 입력 검사; 새 Session과 Runtime 연결. Task 목록·스케줄러·Storage는 보유하지 않음 |
| `TaskSession` | `start`, `result`, `events`, `send_instruction`, `answer`, `cancel`; `status`, `done` property | 실행 핸들, 명령 큐/멱등 키, 이벤트 기록 및 독립 구독 cursor. `_check_loop`, `_send`, `_emit`, `_set_status`, `_nonempty`가 내부 처리 |
| `AgentCore` | `__init__`, `step` | Provider 한 번 호출, 응답 content/tool_calls 구조·호출 ID·인자 JSON 검증. 도구 실행/Task 종료 판단은 하지 않음 |
| `TaskRuntime` | `run`, `_loop`, `_execute_tool`, `_wait_operation` | 전체 루프, 제한, 도구 실행, 외부 작업 중 명령 처리 |
| `TaskRuntime` | `_drain_commands`, `_apply_instructions`, `_ack`, `_ask_host` | 취소·추가 지시·질문 답변 처리와 이벤트 |
| `TaskRuntime` | `_compact_context_if_needed`, `_write_memory`, `_search_conversation`, `_read_conversation` | 문맥 압축, Task 메모리, transcript 조회 |
| `TaskRuntime` | `_current_tool_invocation_index`, `_transcript_text`, `_complete_task`, `_error`, `_serialize_result`, `_finish` | 검색 자기참조 제외, 직렬화, 완료 출력 검증, 결과/종료 이벤트 생성 |

근거: [solver.py:11][solver-class], [session.py:16][session], [core.py:37][core-step], [runtime.py:24][rt-init]. `TaskRuntime`의 실제 구현은 612줄이며 선언만 있는 클래스가 아니다.

### 3.2 생성 → LLM → 도구 → 재호출 → 종료

1. **Host 구성:** Provider와 `ToolRegistry`, 그 Registry를 사용하는 `ToolExecutor`를 준비한다. `create_task()`는 `request`, `model`, `registry`, `executor`를 필수로 받고 `limits`, `options`, `model_selector`를 선택적으로 받는다. Registry/Executor가 같은 도구 집합을 가리키는지는 검증하지 않는다. [생성 API][solver]
2. **Session 생성:** UUID `task_id`, `CREATED` 상태, 빈 명령함·이벤트 로그를 만들고 `created` 이벤트를 기록한다. Runtime은 새 `WorkingMemory`를 만들고 system prompt, 최초 instruction, 추가 context를 transcript에 넣는다. Provider/Core는 Solver에서 재사용한다. [세션 초기화][session], [Runtime 초기화][rt-init]
3. **실행 시작:** `await session.start()`는 Runtime을 `asyncio.create_task()`로 시작하고 반환한다. `await session.result()`가 완료를 기다린다. 결과 대기는 `asyncio.shield()`로 보호되어 대기자 timeout/cancel이 곧 Runtime 취소가 되지 않는다. [start/result][session-start]
4. **step 준비:** 명령을 처리하고 추가 지시를 user 메시지로 반영한다. 취소·`max_steps`를 확인하고 `model_selector(step_number)` 또는 고정 모델을 선택한다. 필요하면 같은 Provider/모델/옵션으로 별도 요약 요청을 수행한다. [루프][rt-loop], [압축][rt-compact]
5. **LLM 호출:** 현재 스키마, Registry version, 지시 revision, 문맥 view를 확보한다. `step_started`를 발생시키고 `AgentCore.step()` → `invoke(provider.chat)`를 실행한다. 동기 호출은 worker thread, 비동기 호출은 이벤트 루프에서 처리한다. [core.py:24][core], [runtime.py:92][rt-call]
6. **호출 중 Host 명령:** `_wait_operation()`이 LLM 작업과 inbox 대기를 동시에 관찰한다. 새 지시가 오면 revision을 바꾼다. 반환된 LLM 응답이 이전 revision이면 `response_discarded`를 기록하고 재시도한다. 이 응답은 transcript에 넣지 않으며 이미 소비한 step은 복구하지 않는다. [작업 대기][rt-wait], [응답 처리][rt-call]
7. **응답 처리:** 빈 응답은 실패한다. `length`, `max_tokens`, `content_filter` 종료는 `INCOMPLETE`다. 도구 없는 일반 텍스트는 transcript와 `message` 이벤트에 기록하고 다음 step으로 간다. 도구 호출이 있으면 flat `id/name/arguments` 형식의 assistant 메시지를 기록한다. [runtime.py:119][rt-response]
8. **도구 순차 실행:** 응답의 도구들을 차례로 처리한다. 현재 지시 revision, 취소, 실행 상한, 제시했던 도구 이름·스키마·Registry version을 확인한다. Task 전용 도구는 Runtime callback으로, 외부 도구는 `SessionTools.execute()` → `invoke(executor.execute)` → `handler(**arguments)`로 간다. [도구 검사][rt-tool], [SessionTools][session-tools], [Executor][executor]
9. **결과 반영:** 성공은 `{ok: true, output: ...}`, 예외는 `{ok: false, error: ...}`로 감싼다. 결과를 JSON 문자열로 만들어 대응하는 tool 메시지에 붙이고 `tool_finished`를 발생시킨다. 이후 새 지시를 반영하고 다음 LLM 호출로 진행한다. 같은 응답의 도구를 병렬 실행하는 코드는 없다. [runtime.py:164][rt-batch], [직렬화][rt-serialize]
10. **완료:** `task_complete`가 다른 도구 없이 단독 호출되고 출력이 비어 있지 않으며 취소/새 지시로 무효화되지 않았다면 `COMPLETED`가 된다. `_finish()`가 메모리·transcript·통계·요약을 `TaskResult`로 반환하고 상태 변경 및 `finished` 이벤트를 기록한다. 이벤트 iterator는 남은 로그를 전달한 뒤 종료한다. [완료 분기][rt-complete-branch], [결과 생성][rt-finish]

### 3.3 종료 상태의 정확한 의미

| 상태 / stop_reason | 코드상 조건 | 주의점 |
|---|---|---|
| `COMPLETED / task_complete` | 단독 완료 도구 성공; 완료 직전 취소·revision 변경 없음 | 일반 텍스트나 Provider의 `stop`만으로는 완료 안 됨; 작업 결과의 사실성 검증 없음 |
| `FAILED / empty_response` | 도구 호출도 없고 content도 비어 있음 | thinking만 있는 응답도 이 분기에 들어갈 수 있음 |
| `FAILED / runtime_error` | Provider·응답 검증·요약·설정 등에서 처리되지 않은 예외 | 예외 이름/문자열이 `warnings`에 기록됨; 엔진 차원의 재시도 없음 |
| `CANCELLED / host_cancelled` | Host 취소 요청을 처리한 후 안전 지점 도달 | 실행 중 LLM/일반 도구 종료를 기다림 |
| `CANCELLED / runtime_cancelled` | Runtime 자체가 외부 `asyncio` 취소를 받음 | `_finish()` 후 `CancelledError`를 다시 던짐. `result()`가 정상 TaskResult를 반환하는 경우와 구분 필요 |
| `LIMIT_REACHED / max_steps` | 다음 step 시작 전 누적 step이 상한 도달 | 일반 텍스트 반복·폐기 응답도 step을 소비 |
| `LIMIT_REACHED / max_tool_calls` | 도구 실행을 시도할 때 누적 호출 수가 상한 도달 | 단순히 상한 값에 도달한 순간 자동 종료하지는 않음 |
| `INCOMPLETE / length`, `max_tokens`, `content_filter` | Provider가 해당 finish_reason 반환 | content를 결과 output으로 반환하나 이 응답은 정상 transcript append 분기 전에 종료 |
| `INCOMPLETE / host_response_timeout` | `host_ask`가 설정 시간 안에 답변을 못 받음 | 기본 300초, `None`이면 무기한 대기 |

근거: [run 예외 처리][rt-run], [루프·종료 분기][rt-loop], [Host 질문][rt-ask], [한도 설정][limits]. 일반 도구 예외·알 수 없는 도구·변경된 도구는 보통 오류 관찰로 LLM에 반환하고 루프를 계속한다. `tool_errors`는 누적되지만 이 값 자체로 중단하는 제한은 없다.

`max_tool_calls`에는 `host_ask`, `memory_write`, `task_complete`도 포함된다. 반면 실행 전 거부된 unknown/changed 도구 등은 카운트 증가 지점 전에 반환한다. `task_complete`와 다른 도구를 함께 부르면 **배치 전체를 실행하지 않고** 각 호출에 `invalid_tool_batch`를 붙인다. 이런 배치는 step은 소비하지만 실제 도구 호출 수는 증가하지 않는다. 완료 도구를 실행할 호출 예산도 필요하다. [배치 처리][rt-batch], [호출 수 증가][rt-tool]

## 4. 기능별 구현 상태와 제약

### 4.1 엔진과 Host 상호작용

| 기능 | 상태 | 실제 구현·범위와 근거 |
|---|---|---|
| Task 생성·실행·결과 | 구현 완료 | `ProblemSolver.create_task`, `TaskSession.start/result`, `TaskRuntime.run` 연결. [solver][solver], [session][session-start] |
| LLM↔도구 반복 | 구현 완료 | `AgentCore.step`, `_execute_tool`, tool 메시지 append 후 반복. [루프][rt-loop] |
| 완료·실패·제한 상태 | 구현 완료 | 명시적 완료 도구, 빈 응답, 예외, step/tool 제한, 미완료 분기. [종료 분기][rt-response] |
| Session별 실행 상태 분리 | 구현 완료 | Session별 UUID·큐·로그·WorkingMemory 생성. [초기화][rt-init] |
| 동시 Task 실행 | 부분 구현 | 여러 Session을 asyncio Task로 시작할 수 있음. 공유 Provider/Registry/Executor의 thread safety, 작업 자원 격리, 동시성 상한은 관리하지 않음. [Solver][solver-class], [start][session-start] |
| 이벤트 관찰·재구독 | 구현 완료 | `events(after_sequence=...)`, 1부터 증가하는 sequence, 구독자별 cursor, deepcopy. 세션 객체 수명 내 replay. [events][session-events] |
| 사용자 질문·답변 | 구현 완료 | `host_ask` → question Future → `answer(request_id, content)` → 도구 결과. 질문 ID 매칭 및 timeout. [질문][rt-ask], [명령 처리][rt-commands] |
| 추가 지시 | 구현 완료 | `send_instruction`, revision 증가, user 메시지 append, 이전 응답 폐기·미실행 도구 차단. [명령][rt-commands], [루프][rt-loop] |
| 명령 중복 처리 | 구현 완료 | `message_id`와 내용이 같으면 재전송 멱등; 같은 ID로 다른 내용이면 오류. 접수·적용·거부 이벤트. [Session._send][session-send] |
| 협력적 작업 취소 | 구현 완료 | inbox cancel → CANCELLING → 안전 지점에서 CANCELLED. host_ask 대기는 취소 가능. [대기][rt-wait], [명령][rt-commands] |
| 실행 중 일반 호출 즉시 중단 | 미구현 | LLM·동기/일반 비동기 도구의 진행 중 작업을 public cancel이 강제 종료하지 않음. [대기 정책][rt-wait] |
| Task별 시작 모델/옵션 선택 | 구현 완료 | create_task의 `model`, `options`; 옵션 deepcopy. [생성][solver], [Runtime 초기화][rt-init] |
| 실행 중 모델 선택 | 부분 구현 | `model_selector(step: int) -> str`를 step 준비 시 호출. Host 명령·승인/적용 이벤트·Provider 교체 API는 없음. [선택 지점][rt-model] |
| Pause/Resume | 미구현 | Session 메서드와 TaskStatus에 해당 항목 없음. WAITING_FOR_HOST는 질문 대기 상태일 뿐. [상태 정의][types], [Session][session] |
| Planning/계획 상태·서브태스크 | 미구현 | 별도 Planner, 계획 모델, 실행 의존 그래프 없음. 모델의 텍스트 계획 또는 메모리 저장과 구분. [Runtime][rt-loop], [모델 정의][working-memory] |
| 네트워크 Host 통신·GUI·CLI 서버 | 미구현 | 인프로세스 asyncio API만 제공. HTTP/WebSocket/IPC 서버·UI 어댑터 없음. [Session][session] |
| LLMProvider/Tool Protocol 선언 자체 | 인터페이스만 존재 | `LLMProvider.chat`, `ToolRegistryLike.schemas`, `ToolExecutorLike.execute`의 `...` 선언. 구체 구현은 다른 모듈에 존재. [core.py:11][core-protocol] |

**실행 중 Host 통신의 구조적 제약**

1. Session의 비동기 API는 하나의 이벤트 루프에서 사용해야 한다. `_check_loop()`가 다른 loop 사용을 거부한다. 다른 UI 스레드·프로세스에서 명령을 직접 전달하는 통신 계층은 Host가 마련해야 한다. 동기 Provider/Executor는 worker thread에서 호출되므로 UI 스레드에 고정된 함수는 별도 어댑터가 필요하다. [Session loop 검사][session], [invoke][core]
2. `send_instruction()`/`answer()`/`cancel()` 반환값은 접수된 `message_id`다. 실제 반영은 `command_applied`, 거부는 `command_rejected`를 확인해야 한다. 잘못된 질문 ID는 접수된 뒤 `no_matching_pending_question`으로 거부될 수 있다. [명령 처리][rt-commands]
3. 질문 대기 중 새 지시를 보내도 기존 질문은 자동으로 닫히지 않는다. 지시는 pending에 쌓이고 답변·timeout·cancel을 기다린다. `host_response_timeout=None`에서 답변 없이 추가 지시만 보내면 계속 대기할 수 있다. [질문 Future][rt-ask], [대기 루프][rt-wait]
4. 새 지시는 시작 전 도구나 아직 반영되지 않은 LLM 응답을 무효화할 수 있지만, 이미 실행한 도구의 파일 변경·외부 부작용을 되돌리지 않는다. 한 응답의 도구들은 순차 실행되어, 앞선 질문이나 긴 도구가 뒤 도구들을 지연시킨다. [배치 처리][rt-batch]
5. 이벤트는 토큰 스트림이 아니다. `message`는 응답 후 전달된다. 이벤트·멱등 키·transcript는 메모리에 계속 보관되며 디스크 replay, 보존 기간, 구독 backpressure, 크기 상한은 없다. `step_started`에는 모델과 문맥량이 있으나 `tool_started`에는 호출 인자가 없다. 완전한 API 원문 감사 로그도 아니다. [Session 로그][session], [도구 이벤트][rt-tool]
6. `model_selector`는 동기 callback이며 step 번호만 받는다. Host가 closure의 모델 값을 변경하면 다음 선택 지점에서 반영할 수 있으나, 그 변경 자체는 지시 revision을 올리지 않고 진행 중 호출을 무효화하지 않는다. 선택은 압축 전이므로 압축 중 바뀐 값도 현재 step에 재선택되지 않는다. [모델 선택 순서][rt-model]
7. 느린 동기 `schemas()`/`model_selector()`나 이벤트 루프를 막는 async handler는 명령 처리도 지연시킬 수 있다. 모든 Host 함수가 자동으로 안전한 비동기 실행으로 바뀌는 구조는 아니다. 종료 후 새 명령은 거부되고 Session은 재시작되지 않는다. [loop][rt-loop], [Session.start/_send][session-start]

실제 이벤트 종류는 `created`, `status_changed`, `command_received`, `command_applied`, `command_rejected`, `step_started`, `response_discarded`, `message`, `tool_started`, `tool_finished`, `question`, `question_closed`, `task_memory_written`, `context_compaction_started`, `context_compacted`, `finished`다. `AgentEvent`에는 `task_id`, `sequence`, `type`, `payload`만 있으며 timestamp는 없다. [이벤트 모델][event-type], [Session emit][session-events], [Runtime][rt-loop]

### 4.2 LLM Provider와 도구

| 기능 | 상태 | 실제 구현·범위와 근거 |
|---|---|---|
| `BaseLLM` 선언 자체 | 인터페이스만 존재 | abstract `models/chat`. 엔진은 이 ABC 대신 최소 Protocol에도 대응. [base.py][llm-base] |
| Ollama adapter | 구현 완료 | `Client.list/chat`, tool schema·메시지·응답·usage 변환. 실제 서버 호출은 미검증. [ollama.py][ollama] |
| OpenAI 호환 adapter | 구현 완료 | `OpenAI.models.list`, `chat.completions.create`, tool 인자 JSON·ID 변환. 실제 서버 호출은 미검증. [openai.py][openai] |
| vLLM 접속 | 부분 구현 | 전용 Provider는 없음. OpenAI 호환 base_url 및 extra_body 전달 경로는 있음. 서버·모델별 호환은 미검증. [OpenAI 요청][openai-chat] |
| Provider 등록/동적 생성 | 구현 완료 | `register/unregister/get/all/is_installed/available/create`. 생성 시 import. 기본 등록 목록은 비어 있음. [registry.py][provider-registry] |
| Provider URL·모델·옵션 | 구현 완료 | URL은 Provider 생성자, 모델·옵션은 Task 생성/step 호출. 아래 설정 표 참조 |
| 토큰 스트리밍 | 미구현 | Provider는 완성 응답 1개로 변환, Runtime도 단일 응답을 기다림. [Ollama][ollama-chat], [OpenAI][openai-chat] |
| usage/thinking의 Task 관측 연결 | 부분 구현 | LLMResponse에 필드가 있으나 Runtime transcript/TaskResult/이벤트에 연결되지 않음. [응답 타입][llm-types], [응답 처리][rt-response] |
| 공통 요청 timeout·retry·비용 제한 | 미구현 | AgentLimits에는 Host 답변 timeout만 있음. SDK 자체 기본 정책과 엔진 보장은 구분해야 함. [limits][limits], [Provider 생성자][openai] |
| Tool Registry | 구현 완료 | 등록·동일 이름 교체·해제·조회·스키마·version. [registry.py][tool-registry] |
| Executor, sync/async handler | 구현 완료 | 직접 Python 호출 + `invoke`가 반환 awaitable도 await. [executor.py][executor], [invoke][core] |
| Task 전용 내장 도구 5개 | 구현 완료 | SessionTools가 자동 추가; 같은 이름의 외부 도구는 충돌 오류. [schemas.py][schemas], [SessionTools][session-tools] |
| 일반 builtin | 구현 완료 | `ECHO_TOOL`, `ADD_TOOL`만 제공; Host가 등록해야 노출. [builtin.py][builtin] |
| JSON Schema 기반 실행 인자 검증 | 부분 구현 | dict·문자열 key·JSON 가능 여부는 검증. required/type/range/additionalProperties 전체 검증기는 없음. [AgentCore.step][core-step], [Executor][executor] |
| 파일/패치/셸/Windows 제어 도구 | 미구현 | 일반 builtin은 echo/add뿐. Python handler를 통한 Host 도구 주입은 가능. [builtin][builtin], [Executor][executor] |
| 도구 권한·승인·sandbox | 미구현 | Executor가 handler를 직접 호출. 허용 목록은 등록 도구 수준이며 파일/프로세스 권한 통제는 없음. [executor.py][executor] |
| 도구 변경 감지 | 부분 구현 | snapshot 스키마 및 Registry version 비교. 이미 실행 중인 handler 교체를 되돌리지 않음; 직접 객체 변경은 version을 우회할 수 있음. [도구 검사][rt-tool], [Registry][tool-registry] |

**실제 Provider 설정**

| 항목 | OllamaLLM | OpenAILLM |
|---|---|---|
| 생성자 | `OllamaLLM(host="http://localhost:11434")` | `OpenAILLM(base_url="http://localhost:8000/v1", api_key="EMPTY")` |
| 모델 목록 | `models()` → `get_available_models()` → `client.list()` | `models()` → `get_available_models()` → `client.models.list()` |
| 모델 선택 | `chat(model=...)`; Task에서는 `create_task(model=...)` | 동일. 기본 URL은 로컬 호환 서버이며 실제 사용할 endpoint/key를 Host가 지정해야 함 |
| 공통 옵션 | temperature, top_p, seed, stop; max_tokens → num_predict | temperature, top_p, max_tokens, seed, stop을 요청에 전달 |
| 확장 옵션 | `extra["options"]`를 generation 옵션에 병합; think/format/keep_alive/logprobs/top_logprobs만 top-level 전달 | `extra["extra_body"]`와 `extra["request"]`를 전달 |
| 응답 처리 | ToolCall ID는 자체 UUID 생성, tool 결과에 tool_name 사용 | 서버 ToolCall ID 보존, tool 결과에 tool_call_id 사용 |
| 오류 인자 처리 | arguments를 dict로 변환 후 Core 검증 | JSON 파싱 실패/객체 이외이면 `{"_raw": 원문}`; Core를 통과해 handler 인자 오류가 될 수 있음 |

근거: [Ollama 요청·변환][ollama-chat], [OpenAI 요청·변환][openai-chat], [ChatOptions][llm-options]. `extra["request"]`는 `kwargs.update()`로 병합되므로 model/messages/tools 등도 덮어쓸 수 있다. 이 경우 `step_started`의 모델과 실제 요청이 달라질 수 있고, `stream=True`처럼 완성 응답 가정을 깨는 값은 현재 변환 코드와 맞지 않는다. 이는 정적 코드에서 확인한 제약이며 서버 오류를 재현한 것은 아니다.

Host의 외부 도구 등록은 `registry.register(Tool(spec=ToolSpec(...), handler=함수))` 후 그 Registry와 `ToolExecutor(registry)`를 `create_task()`에 넘기는 방식이다. 자동 함수 스키마 생성, 도구 발견 서비스, MCP 클라이언트는 없다. `ToolExecutor`는 Task ID나 Workspace를 handler에 자동 주입하지 않으므로, Task마다 다른 환경은 Host의 closure·별도 Registry/Executor로 연결해야 한다. [tool 모델][tool-model], [실제 데모 등록][demo-tools]

### 4.3 Workspace / Storage / Memory

| 기능 | 상태 | 실제 구현·범위와 근거 |
|---|---|---|
| Task별 파일 Workspace | 미구현 | TaskRequest/Runtime에 workspace/root/cwd 필드와 디렉터리 생성 없음. [작업 입력][types], [Runtime 초기화][rt-init] |
| 추가 Mount·경로 매핑 | 미구현 | 관련 모델/API 없음. SQLite 경로는 DB 파일 위치이며 Workspace가 아님. [SQLite][sqlite] |
| 파일 접근 권한/읽기·쓰기 정책 | 미구현 | 파일 도구/경로 정책이 없고 Host handler가 권한을 책임짐. [Executor][executor] |
| Storage Protocol 자체 | 인터페이스만 존재 | initialize/create_task/get_task/update_task_status/append/get/read_messages/search/close 선언. [base.py][storage-base] |
| 공통 SQL 저장 구현 | 구현 완료 | SQLStorage에 위 메서드 모두 구현; 동기 SQLAlchemy I/O를 to_thread로 실행. [구현][sql] |
| SQLite Backend | 구현 완료 | 파일 경로 resolve·부모 생성·Engine·FK/busy timeout 설정. DB 실행 미검증. [sqlite.py][sqlite] |
| PostgreSQL/MySQL Backend | 구현 완료 | DSN 정규화 후 공통 SQLStorage 상속; 빈 스텁이 아님. 실제 DB/driver 조합은 미검증. [postgresql.py][postgres], [mysql.py][mysql] |
| Task 상태·실행 이력 영속화 전체 | 부분 구현 | 저장 메서드와 Record 모델은 있으나 Runtime 자동 기록/동기화 없음. Host가 직접 호출해야 함. [Storage 모델][storage-model], [Runtime][rt-init] |
| Namespace/Task별 SQL 조회 범위 | 구현 완료 | 조회에 namespace+task_id 필터. 인증·사용자별 접근 통제는 아님. [SQLStorage][sql] |
| Working Memory | 구현 완료 | transcript·TaskMemory·요약·revision·누적 통계를 Session별 보유. [WorkingMemory][working-memory] |
| Task 메모리 기록·재사용·반환 | 구현 완료 | `memory_write` → 메모리 목록 → 매 호출 system context에 삽입 → TaskResult.memory. [기록][rt-memory], [문맥][context-build] |
| Context compaction | 구현 완료 | 문자량 기준, 오래된 tool/result 그룹 경계 보존, 추가 LLM 요약, 원래 transcript 유지. [압축][rt-compact], [경계][context-boundary] |
| 모델 토큰 창의 엄격한 보장 | 미구현 | tokenizer/실제 모델 용량 조회 없음; 문자 threshold는 hard cap이 아님. [문자 추정][context-estimate] |
| transcript 검색·분할 읽기 | 구현 완료 | conversation_search/read, 안정적인 message_index와 char_offset. [검색/읽기][rt-search] |
| 전체 원본 Provider/도구 결과 보존 | 부분 구현 | 이미 기록된 transcript는 압축 시 보존하지만 큰 도구 결과는 기록 전에 잘리고 폐기 응답·thinking·usage는 저장 안 됨. [직렬화][rt-serialize], [응답 처리][rt-response] |
| 세션 재구성·중단 지점 복구 | 미구현 | TaskResult/DB를 받아 Runtime을 복원하는 API 없음. Future·pending 명령·실행 위치 저장 없음. [생성 API][solver], [결과 모델][result-type] |
| Host 장기 기억 연동 | 부분 구현 | TaskRequest.context로 텍스트 입력, TaskResult.memory로 출력 가능. 장기 기억 조회·선정·저장은 Host 책임. [입력][types], [결과][result-type] |
| MindMap / 자율형 Life Agent | 미구현 | 프로젝트에 모듈 없음. 사용자 목표상 별도 개발 영역이므로 현재 핵심 엔진의 필수 결함으로 취급하지 않음 |

**Storage가 실제로 저장하는 것**

- `agent_tasks`: `(namespace, task_id)` 복합 기본키, status, created_at. `TaskStatus` 전이 규칙과 연동되지 않는 문자열 상태다.
- `agent_records`: 전역 unique `record_id`, namespace/task_id, kind, message_index, JSON payload/source_refs, source_start/end, search_text, created_at 및 증가 row_id. Task에 대한 FK, `(namespace, task_id, kind, message_index)` unique 제약이 있다. [테이블 정의][sql-schema]
- `read_messages()`는 kind=message의 index 순으로 읽고, `search()`는 해당 Task의 search_text를 대소문자 무시 부분 문자열로 찾아 최근 row부터 반환한다. 기본 search_text는 payload JSON의 앞 8,000자다. 벡터 검색·의미 검색은 아니다. [StorageRecord][storage-model], [SQL 조회][sql-read]
- 각 메서드 단위 DB transaction은 있으나 Task 실행과 저장을 원자적으로 묶지 않는다. `create_task()`/`append()`는 insert이며 동일 ID 재시도의 upsert/멱등 처리가 없다. source_refs는 다른 Record를 가리킨다는 설명만 있고 해당 ID의 존재를 검증하는 FK는 아니다. [SQL 쓰기][sql-write]
- schema 초기화는 `metadata.create_all()`이다. 마이그레이션, 삭제·보존 정책, Task 목록 API, 복구 로더는 없다. SQLite 기본 위치는 `./data/shs-agent.sqlite3`; 생성자에서 부모 디렉터리를 만든다. `":memory:"`를 특별 처리하지 않아 현재 생성자는 메모리 DB 선택 API도 제공하지 않는다. [SQL 초기화][sql], [SQLite 생성자][sqlite]

**메모리와 문맥의 실제 제한**

기본값은 step 20, tool 호출 50, tool 결과 12,000자, 압축 시작 기준 48,000자, 최근 메시지 최소 12개, 요약 최대 12,000자, 메모리 항목 content 4,000자 및 총 content 24,000자다. 검색 결과 최대 20개, 기본 검색 반환 8개, snippet 600자, 메시지 읽기 최대 6,000자다. [AgentLimits][limits], [검색 기본값][rt-search]

`TaskMemory`는 추가만 가능하고 수정·삭제·중복 제거 API는 없다. 메모리 크기 제한은 content 기준이어서 kind/source_refs 등 모든 직렬화 길이를 제한하지 않는다. 검색은 현재 실행 중인 조회 도구의 assistant 메시지를 제외한다. 조회하는 “원본”은 Runtime이 기록한 JSON 메시지이며, 도구 결과가 12,000자 제한으로 잘린 경우 잘린 뒤의 내용만 남는다. [메모리 쓰기][rt-memory], [검색/읽기][rt-search], [직렬화][rt-serialize]

압축은 최초 system/사용자 instruction을 유지하고 요약·메모리를 system 메시지에 덧붙인 뒤 최근 메시지를 결합한다. 안전하게 접을 오래된 그룹이 없거나 원래 instruction·도구 스키마가 큰 경우 threshold를 넘은 채 요청할 수 있다. 요약 호출도 전체 오래된 구간을 한 번에 보내므로 입력 토큰 창 초과를 예방하는 보장은 없다. 요약 실패는 fallback 없이 Runtime 실패로 이어진다. 요약 호출은 problem-solving step 수에 포함되지 않는다. [문맥 구성][context-build], [압축][rt-compact]

### 4.4 설치·Windows·경량성·테스트

| 기능/조건 | 상태 | 확인된 범위와 근거 |
|---|---|---|
| GPU/무거운 AI 라이브러리 없는 엔진 구조 | 구현 완료 | 핵심은 stdlib, 추론은 외부 SDK 호출. torch/transformers/CUDA/vLLM 엔진 의존성 없음. [패키지 설정][packaging], [core][core] |
| 기본 설치 구성 | 구현 완료 | Python >=3.11, setuptools>=68 빌드, SQLAlchemy>=2.0,<3.0 기본 의존성. 무의존 패키지는 아님. [pyproject][packaging] |
| 선택 의존성 분리 | 구현 완료 | openai, ollama, postgresql(psycopg[binary]>=3.1,<4.0), mysql(PyMySQL>=1.1,<2.0), dev(pytest/pytest-asyncio), all. SDK는 버전 제한 없음. [pyproject][packaging] |
| Windows 전체 사용 성공 | 확인 불가 | 핵심에 POSIX 전용 셸/fork 의존은 안 보이고 asyncio/pathlib 사용. 실제 Host/SDK/DB 실행은 하지 않음 |
| 저사양 CPU/RAM 성능 | 확인 불가 | 벤치마크 없음. transcript·이벤트 누적 및 deepcopy 비용이 있어 경량 의도만으로 상한 보장 불가. [메모리][working-memory], [Session][session] |
| 실제 LLM/DB 서비스 연결 | 확인 불가 | adapter 코드 존재와 서비스 연결·모델의 tool calling 능력은 별개 |
| 자동 회귀 테스트 | 미구현 | 프로젝트에서 pytest/unittest 테스트 함수·assert 기반 검증을 찾지 못함. 테스트 의존성 선언만 있음 |
| 수동 엔진 예제 | 구현 완료 | test.py의 Ollama/계산/질문/완료 루프. 이번 조사에서는 미실행. [test.py][demo] |
| 수동 DB 예제 | 구현 완료 | examples/test_db.py의 초기화·Task/Record insert·조회·close. 이번 조사에서는 미실행. [DB 예제][db-demo] |
| 사용 가이드·CLI entry point | 미구현 | README 한 줄, docs 비어 있음, project.scripts 없음. [README][readme], [pyproject][packaging] |

로컬 설치 상태는 소스의 설치 요구사항과 일치하지 않는다. `.venv/pyvenv.cfg`는 Python 3.12.9, system site-packages 제외로 기록되어 있다. site-packages 디렉터리에서 Ollama 0.6.2, OpenAI 3.19.2 메타데이터는 확인했지만 SQLAlchemy·pytest·psycopg·PyMySQL에 해당하는 디렉터리는 확인하지 못했다. `.venv`의 shs-agent METADATA와 `src/shs_agent.egg-info/requires.txt`에도 새 기본 의존성 SQLAlchemy와 DB extras가 빠져 있다. 따라서 현재 `.venv`에서 Storage import가 의존성 부족으로 실패할 가능성이 높지만, import를 실행하지 않았으므로 실제 오류로 재현했다고 표현하지 않는다. [가상환경 설정][venv-config], [설치 메타데이터][installed-meta], [기존 requires][egg-requires]

또한 `pyproject.toml`의 버전은 `0.1.0`, 패키지 `__version__`은 `0.3.1`이다. 실제 배포/설치 버전과 코드 버전을 같은 값으로 간주하면 안 된다. [패키징][packaging], [루트 버전][root-api]

## 5. 외부 Host에서 사용하는 현재의 실제 API 예시

아래 예시는 **현재 존재하는 공개 API로 조합한 보고서 내 사용 예시**다. 프로젝트에 새 실행 파일을 만들거나 기능을 구현한 것이 아니며, 이번 조사에서 실행하지 않았다. 실제 서버·tool calling 지원 모델·선택 SDK·대상 프로젝트가 준비되어야 한다.

### 5.1 Windows 프로젝트를 읽는 Host의 최소 연결 예시

라이브러리에 파일 도구가 없으므로 예시의 `read_project_text`는 **Host 측 코드**다. Path 기반 경로 검사도 이 예시에서 Host가 수행하며, 라이브러리가 제공하는 보안 경계라고 해석하면 안 된다. OS 권한, junction/링크 변경 경쟁, 프로세스 격리까지 보장하는 production sandbox 예시는 아니다.

```python
import asyncio
from pathlib import Path

from shs_agent.agent import AgentLimits, ProblemSolver, TaskRequest
from shs_agent.llm import ChatOptions
from shs_agent.llm.provider.ollama import OllamaLLM
from shs_agent.tool import Tool, ToolExecutor, ToolRegistry, ToolSpec


async def run_host(project_root: Path, model: str):
    root = project_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project_root must be a directory")

    # Host가 소유하는 읽기 전용 도구. 엔진 builtin이 아니다.
    def read_project_text(relative_path: str) -> str:
        path = (root / relative_path).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("file must be inside the host project")
        if path.stat().st_size > 32_000:
            raise ValueError("file is too large for this example")
        return path.read_text(encoding="utf-8")

    registry = ToolRegistry()
    registry.register(Tool(
        spec=ToolSpec(
            name="read_project_text",
            description="Read a UTF-8 file inside the host project.",
            parameters={
                "type": "object",
                "properties": {"relative_path": {"type": "string"}},
                "required": ["relative_path"],
                "additionalProperties": False,
            },
        ),
        handler=read_project_text,
    ))

    provider = OllamaLLM(host="http://localhost:11434")
    solver = ProblemSolver(provider)
    session = solver.create_task(
        request=TaskRequest(
            instruction=(
                "read_project_text로 README.md를 읽고 프로젝트를 설명해줘. "
                "필수 정보가 부족하면 host_ask로 질문하고, "
                "완료하면 task_complete를 단독 호출해줘."
            ),
            context=("Host가 제공한 프로젝트 파일 읽기 도구만 사용한다.",),
        ),
        model=model,
        registry=registry,
        executor=ToolExecutor(registry),
        options=ChatOptions(temperature=0.2),
        limits=AgentLimits(max_steps=10, max_tool_calls=20),
    )

    async def observe():
        async for event in session.events():
            print(event.sequence, event.type, event.payload)
            if event.type == "question":
                try:
                    answer = ""
                    while not answer:
                        answer = (await asyncio.to_thread(input, "답변: ")).strip()
                except EOFError:
                    await session.cancel()
                    return
                if not session.done:
                    await session.answer(event.payload["request_id"], answer)

    observer = asyncio.create_task(observe())
    await session.start()
    result = await session.result()
    await observer
    print(result.status.value, result.stop_reason, result.output)
    return result


# 별도의 Host 프로그램에서 실행할 때만 사용한다.
# 첫 인자는 기존 Windows 프로젝트 경로, 둘째는 서버에 실제 설치된 모델 이름이다.
if __name__ == "__main__":
    import sys
    asyncio.run(run_host(Path(sys.argv[1]), sys.argv[2]))
```

`ToolRegistry`, `ToolSpec`, `ToolExecutor`는 패키지 공개 export를 사용했고, `TaskRequest`와 `AgentLimits`는 `shs_agent.agent`에서 가져왔다. Session 전용 5개 도구는 별도 등록하지 않아도 Runtime이 추가한다. 이 예시로 설명하는 범위는 파일 읽기 도구를 연결한 Task 루프이며, 파일 편집·테스트 실행·Git 작업은 제공하지 않는다. [공개 API][agent-api], [tool export][tool-api], [자동 세션 도구][session-tools]

위 단순 터미널 observer는 입력 대기 중 다른 이벤트 표시도 지연된다. Host 질문이 timeout되거나 다른 경로에서 Task가 종료되어도 이미 실행한 `input()` thread를 엔진이 중단하지 않으므로 observer 대기가 남을 수 있다. 실제 UI Host는 질문 표시/답변 입력과 이벤트 소비를 분리하고 `question_closed`도 처리해야 한다. 이것은 제공 라이브러리의 기능과 예시 Host의 입력 정책을 구분하기 위한 설명이다. 기존 `test.py`도 한 이벤트 소비 coroutine에서 input을 기다리는 형태다. [기존 Host 예제][demo-host], [질문 종료][rt-ask]

### 5.2 추가 지시·취소·모델 선택

동작 중인 동일 Session/이벤트 루프를 보유한 Host는 다음 공개 API를 각각 필요한 시점에 호출할 수 있다.

```python
# 추가 지시: 접수 ID 반환 후 command_applied/command_rejected를 관찰한다.
message_id = await session.send_instruction(
    "범위를 README.md와 pyproject.toml로 제한해줘.",
    message_id="host-instruction-001",
)

# 취소 버튼 등 별도의 Host 동작에서 호출한다.
cancel_id = await session.cancel(message_id="host-cancel-001")
```

모델 선택 callback은 `create_task(..., model_selector=...)`의 기존 인자다.

```python
selected = {"model": first_model}

session = solver.create_task(
    request=TaskRequest(instruction="주어진 문제를 해결해줘."),
    model=first_model,
    model_selector=lambda step: selected["model"],
    registry=registry,
    executor=ToolExecutor(registry),
)

# Host가 같은 loop에서 값을 바꾸면 이후 callback 호출부터 다른 이름을 반환한다.
selected["model"] = next_model
```

`first_model`/`next_model`은 같은 Provider에서 사용할 수 있는 실제 모델 이름이어야 한다. 위 변경에는 모델 변경 이벤트·명령 확인·현재 요청 취소가 없다. Provider URL/API key까지 Task마다 바꾸려면 현재 고수준 API에서는 별도의 Provider/Solver를 구성하는 방식이 자연스럽다. `session.change_model()`, `pause()`, `resume()` 같은 메서드는 현재 없다. [모델 선택][rt-model], [Solver][solver], [Session][session]

OpenAI 호환 서버를 사용하려면 위 Host 예시의 Provider 생성 부분을 다음처럼 바꿀 수 있다. 생성자와 `ChatOptions.extra` 전달 경로가 존재한다는 예시이며, 특정 외부 모델/서버의 호환성을 검증한 코드는 아니다.

```python
from shs_agent.llm.provider.openai import OpenAILLM

provider = OpenAILLM(base_url=host_configured_url, api_key=host_configured_key)
solver = ProblemSolver(provider)
```

### 5.3 완료 결과를 Host가 수동 저장하는 예시

다음은 현재 Storage API의 연결 가능성을 보여준다. **완료 후 저장**이며 실행 중 checkpoint나 자동 복구를 제공하지 않는다. SQLAlchemy가 준비된 별도 Host 환경을 전제로 한다. `db_path`의 생성/쓰기 권한도 Host 책임이다.

```python
from dataclasses import asdict
from shs_agent.storage import SQLiteStorage, StorageRecord, TaskRecord


async def save_result(result, db_path):
    storage = SQLiteStorage(db_path)
    try:
        await storage.initialize()
        await storage.create_task(TaskRecord(
            task_id=result.task_id,
            status=result.status.value,
        ))
        for index, message in enumerate(result.messages):
            await storage.append(StorageRecord(
                task_id=result.task_id,
                kind="message",
                message_index=index,
                payload=message,
            ))
        for item in result.memory:
            await storage.append(StorageRecord(
                task_id=result.task_id,
                kind="task_memory",
                payload=asdict(item),
            ))
        await storage.append(StorageRecord(
            task_id=result.task_id,
            kind="result",
            payload={
                "output": result.output,
                "stop_reason": result.stop_reason,
                "context_summary": result.context_summary,
                "warnings": list(result.warnings),
            },
        ))
    finally:
        await storage.close()
```

같은 Task를 다시 저장하는 upsert 경로는 없으므로 이 예시는 한 번만 저장하는 전제다. 각 append는 별도 transaction이라 중간 저장 실패 시 일부 기록만 남을 수 있다. 또한 Runtime의 TaskMemory.source_refs와 StorageRecord의 최상위 source_refs 사이에 자동 ID 매핑은 없다. 위 예시는 TaskMemory 값을 payload에 그대로 보관할 뿐이다. [SQL 저장][sql-write], [StorageRecord][storage-model]

## 6. 테스트·사용성과 목표 아키텍처의 차이

### 6.1 현재 예제와 실제 검증 범위

| 파일 | 코드에 있는 사용 시나리오 | 검증 공백 |
|---|---|---|
| [test.py][demo] | Ollama 모델 목록 조회, 계산 도구 등록, 41+1 → host_ask → 추가 계산 → task_complete 요청, 결과·메모리 출력 | assert/기대 결과 비교 없음. 모델의 실제 행동에 의존하는 수동 데모 |
| [test.py의 calculate][demo-calculate] | AST와 Fraction을 이용한 제한적 사칙연산·거듭제곱 handler | 엔진의 계산 builtin이 아님. 별도 단위 테스트 없음 |
| [examples/test_db.py][db-demo] | SQLite initialize, task-001 생성, message insert/get, payload 출력, close | 상태 갱신·검색·페이지 조회·제약·동시성·장애 복구 검증 없음. PostgreSQL 선택 부분은 주석, MySQL 예제 없음 |
| [README][readme], [pyproject][packaging] | 패키지 이름, 설치 extras와 pytest 의존성 선언 | 사용 문서·자동 검증·CI 설정을 대신하지 않음 |

`test.py`는 `MODEL`을 두 번 대입해 최종적으로 `gemma4:26b`를 선택한다. 해당 모델 설치 여부를 조회한 후 실행하며, `provider.chat`을 debug wrapper로 바꾸어 thinking/usage 등을 출력한다. 이는 Runtime 자체가 이 정보를 이벤트로 내보내는 구현이 아니다. [모델 설정·debug wrapper][demo], [데모 실행][demo-main]

`examples/test_db.py`는 `if __name__ == "__main__"` 가드 없이 파일 끝에서 `asyncio.run(main())`을 호출한다. 따라서 파일 import/테스트 수집 시에도 DB 접근이 발생하는 구조다. 기본 파일 DB와 고정 task_id `task-001`을 사용하므로, 기존 DB에 같은 Task가 있으면 insert unique 충돌이 날 수 있다. 이번 조사에서는 이 파일을 실행하거나 테스트 수집하지 않았다. [DB 예제][db-demo], [insert 구현][sql-write]

자동 검증이 확인되지 않은 주요 범위는 완료 도구 단독 호출, 한도/도구 오류, 질문 timeout, 늦은 답변·중복 message_id, 호출 중 새 지시·취소, 복수 Session, Registry 변경, 문맥 압축의 tool/result 묶음 보존, 결과 잘림, Provider 메시지 변환, 각 DB Backend 및 Windows Host 통합이다. 이 목록은 새 테스트를 추가했다는 의미가 아니라 현재 검증 공백이다.

### 6.2 목표와 현재 구현의 대응

| 목표 | 현재 코드 | 차이 또는 남은 조건 |
|---|---|---|
| 다양한 Host에서 재사용하는 독립 라이브러리 | UI/셸에 직접 의존하지 않는 Python API와 주입 경계 | Host SDK/가이드, CLI/Server/Windows UI adapter는 없음 |
| LLM 추론을 외부 Provider에 위임 | Ollama 및 OpenAI 호환 adapter, 커스텀 chat Protocol | vLLM 전용 구현 없음; 실제 서버별 호환과 모델의 완료 도구 사용 능력 검증 필요 |
| GPU 없이 가벼운 엔진 실행 | 학습/추론 프레임워크·GPU 의존 없음 | SQLAlchemy 기본 의존, 로그 누적/deepcopy 비용 존재; 저사양 성능은 미측정 |
| Host가 작업 환경·도구·권한·UI 소유 | Registry/Executor 주입, question/event 명령 API | Workspace·Mount·권한 정책 전달용 공통 객체는 없음. Host closure/adapter가 환경을 연결해야 함 |
| Task 단위 엔진·TaskSession·Working Memory | 실제 생성/실행/상태/메모리 흐름 구현 | 완전한 작업 격리/스케줄링, 프로세스 재시작 후 복구는 없음 |
| 실행 중 관찰·추가 지시·답변·취소 | 인프로세스 events/commands와 revision 기반 안전 지점 처리 | 실시간 토큰 스트림, 즉시 네트워크/도구 중단, 원격 전송은 없음 |
| 실행 중 모델 변경 | step별 model_selector | 명시적 Host 명령·options/Provider 변경·진행 중 응답 정책은 없음 |
| Pause/Resume·Planning | 해당 API·상태·실행 모델 없음 | 질문 대기나 LLM 텍스트 계획과 구분해야 함 |
| Host 장기 기억과 엔진 작업 기억 분리 | TaskRequest.context 입력, TaskResult.memory 출력 | 장기 기억 검색·저장·다음 Task로 이전은 Host가 처리 |
| 별도 MindMap/Life Agent | 현재 소스에 없음 | 별도 모듈 개발 목표와 모순되지 않음 |

### 6.3 Windows 코딩 에이전트 Host 구성 가능성

**공개 API를 엔진으로 사용하여 Host를 구성하는 것은 코드 구조상 가능하다.** Provider와 Python handler를 붙이면 파일 읽기/편집/테스트 실행 같은 Host 동작을 LLM이 호출하는 구조를 만들 수 있다. 다만 현재 프로젝트에는 그 Windows Host 자체가 없고, 이번 조사에서 그러한 종단간 실행을 확인하지 않았다.

| shs-agent가 현재 제공하는 기능 | Host에서 직접 담당해야 하는 기능 |
|---|---|
| Task 생성, 비동기 루프, 상태·결과 | 작업 접수 UI/CLI/서버, 세션 목록, 실행 정책 |
| Provider adapter와 옵션 전달 | 서버 준비·모델 선택·키 관리·Provider 수명/접속 정책 |
| 도구 호출·결과 포장·다음 LLM 반영 | 파일/패치/Git/PowerShell/테스트 도구 구현, 인자 검증, 실행 승인 |
| Task 전용 질문 도구, 이벤트 및 명령 | 질문 UI, 사용자 입력, 이벤트 전달·저장, UI thread/프로세스 bridge |
| 협력적 취소, step/tool 상한 | 외부 프로세스 종료·rollback 정책, 전체 timeout·비용·자원 제한 |
| Working Memory, 요약, transcript 검색 | Workspace 격리·Mount·경로 접근 정책·장기 기억 |
| 독립 SQL Storage API | Runtime 이벤트/결과를 Record로 변환하여 저장, 장애 대응·보존 정책 |

Host가 저장된 TaskResult의 필요한 내용을 문자열로 변환하여 `TaskRequest.context`에 넣고 새 Task를 시작할 수는 있으나, 이는 새로운 Session이다. context는 문자열의 시퀀스만 받으며 TaskResult 객체를 직접 받지 않는다. 원래 도구 호출 위치·질문 Future·명령 멱등성·Task ID를 복원하는 Resume와 동일하지 않다. [새 Session 생성][solver], [Session task_id][session], [입력/결과 모델][types]

## 7. 주요 미구현 기능과 구조적으로 검토할 부분

아래는 현재 코드에서 드러난 공백과 검토 지점이다. 새 설계를 확정하거나 구현하지 않았다.

| 항목 | 현재 사실과 영향 | 검토 방향 |
|---|---|---|
| Runtime와 Storage 연결·복구 | 저장 클래스는 있으나 자동 이력 기록, checkpoint, loader 없음. 프로세스 종료 시 실행 상태 소실. [Runtime][rt-init], [Storage][sql] | Host 저장 책임과 엔진 복구 책임의 경계를 명확히 할 필요 |
| 모델 변경·Pause/Resume | model_selector만 있고 제어 명령/상태 없음. [Session][session], [모델 선택][rt-model] | 안전 지점·진행 중 호출·변경 적용 시점의 요구사항 정리 |
| Planning | Planner/계획 상태/서브태스크 API 없음. [상태 모델][working-memory] | 모델 텍스트 계획과 엔진이 관리할 계획을 구분 |
| Workspace/권한 | 주입 도구가 process 권한으로 실행. Task별 filesystem 분리나 mount 정책 없음. [Executor][executor] | Host 소유 원칙을 유지하면서 전달해야 할 환경·권한 계약 검토 |
| 종료 의미와 예산 | 완료 도구 미호출 시 텍스트 답변이 반복될 수 있고 완료 도구도 tool 예산 소비. 오류 수 제한 없음. [루프][rt-loop] | 실제 대상 모델의 완료 동작과 상한 설정 검증 |
| 취소 응답성과 질문 대기 | 일반 호출 종료까지 취소 완료 지연; 새 지시만으로 질문 대기 해제 안 됨. [대기][rt-wait], [질문][rt-ask] | Host UI의 기다림·취소 표시와 외부 작업 제어 책임 정리 |
| 전체 기록과 메모리 상한 | tool 결과 기록 전 잘림, 폐기 응답 미기록, 이벤트/원본 transcript 누적. 압축은 LLM view만 줄임. [직렬화][rt-serialize], [WorkingMemory][working-memory] | 감사 기록 범위와 저사양 장시간 작업의 실제 메모리 비용 확인 |
| 압축의 토큰 한계·실패 처리 | 문자 기반 threshold, 전체 과거 구간 단일 요약 요청, 실패 시 Task 실패. [압축][rt-compact] | 실제 모델 context 창과 압축 경계/실패 시 동작 검증 |
| Registry 변경과 실행 계약 | version은 register/unregister 때 증가; Tool/ToolSpec은 가변. Executor와 Registry 일치 확인 없음. [Registry][tool-registry], [Tool 모델][tool-model] | 실행 중 도구 변경 및 공유 객체의 허용 범위 정리 |
| Provider 응답 정보 연결 | LLMResponse의 thinking/usage와 Runtime 기록 경로가 분리되어 관측 정보 소실. [응답 타입][llm-types], [Runtime][rt-response] | 공개 이벤트/결과에서 필요한 관측 범위를 정리 |
| 설치·문서·테스트 신뢰성 | 소스/설치 메타데이터 불일치, 버전 불일치, 수동 데모만 존재. [설정][packaging], [메타데이터][installed-meta], [예제][db-demo] | 의존성 충족 환경에서 핵심 경로 검증과 사용 계약 문서화 필요 |
| 미사용 중복 소스 | tools_depricated.py에 이전 SessionTools가 남아 현재 구현과 다름. [기존 사본][legacy-tools] | 실제 사용 경로와 호환성 의도를 명확히 할 필요 |

현재 동작 범위를 가장 정확히 표현하면, **“Host가 준비한 도구와 외부 LLM을 사용해 인메모리 Task를 수행하고, 실행 중 질문·지시·취소와 작업 문맥 관리를 제공하는 엔진”**이다. “파일 권한이 격리되고 실행이 자동 영속화되며 중단 후 재개되는 완성형 Windows 코딩 에이전트”까지 구현된 상태로 볼 수는 없다. 이 판단은 정적 호출 경로에 근거하며 실제 실행 성공을 인증하는 결과는 아니다.

---

## 조사 기록

본문의 근거 링크는 조사 시점 실제 소스의 절대 경로와 시작 줄을 가리킨다. 조사는 파일 목록 확인, 전체 프로젝트 소스 읽기, import/호출 관계 및 공개 export 대조, 패키징/설치 메타데이터 확인으로 진행했다. 프로젝트 코드를 실행하지 않았으며 보고서 외 코드·설정·의존성을 수정하지 않았다. commit·push도 수행하지 않았다.

[root-api]: H:/shs/projects/shs-agent/src/shs_agent/__init__.py:1
[agent-api]: H:/shs/projects/shs-agent/src/shs_agent/agent/__init__.py:1
[solver-class]: H:/shs/projects/shs-agent/src/shs_agent/agent/solver.py:11
[solver]: H:/shs/projects/shs-agent/src/shs_agent/agent/solver.py:15
[session]: H:/shs/projects/shs-agent/src/shs_agent/agent/session.py:16
[session-start]: H:/shs/projects/shs-agent/src/shs_agent/agent/session.py:46
[session-send]: H:/shs/projects/shs-agent/src/shs_agent/agent/session.py:76
[session-events]: H:/shs/projects/shs-agent/src/shs_agent/agent/session.py:95
[types]: H:/shs/projects/shs-agent/src/shs_agent/agent/types.py:13
[limits]: H:/shs/projects/shs-agent/src/shs_agent/agent/types.py:56
[event-type]: H:/shs/projects/shs-agent/src/shs_agent/agent/types.py:124
[result-type]: H:/shs/projects/shs-agent/src/shs_agent/agent/types.py:139
[working-memory]: H:/shs/projects/shs-agent/src/shs_agent/agent/types.py:155
[core-protocol]: H:/shs/projects/shs-agent/src/shs_agent/agent/core.py:11
[core]: H:/shs/projects/shs-agent/src/shs_agent/agent/core.py:24
[core-step]: H:/shs/projects/shs-agent/src/shs_agent/agent/core.py:37
[rt-init]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:24
[rt-run]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:52
[rt-loop]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:66
[rt-model]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:78
[rt-call]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:92
[rt-response]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:119
[rt-batch]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:164
[rt-complete-branch]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:203
[rt-compact]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:227
[rt-tool]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:286
[rt-wait]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:315
[rt-commands]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:352
[rt-ask]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:390
[rt-memory]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:412
[rt-search]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:458
[rt-serialize]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:566
[rt-finish]: H:/shs/projects/shs-agent/src/shs_agent/agent/runtime.py:583
[context-build]: H:/shs/projects/shs-agent/src/shs_agent/agent/context.py:44
[context-estimate]: H:/shs/projects/shs-agent/src/shs_agent/agent/context.py:72
[context-boundary]: H:/shs/projects/shs-agent/src/shs_agent/agent/context.py:105
[legacy-tools]: H:/shs/projects/shs-agent/src/shs_agent/agent/tools_depricated.py:75
[session-tools]: H:/shs/projects/shs-agent/src/shs_agent/agent/tools/session_tools.py:23
[schemas]: H:/shs/projects/shs-agent/src/shs_agent/agent/tools/schemas.py:4
[llm-api]: H:/shs/projects/shs-agent/src/shs_agent/llm/__init__.py:1
[llm-base]: H:/shs/projects/shs-agent/src/shs_agent/llm/base.py:7
[llm-types]: H:/shs/projects/shs-agent/src/shs_agent/llm/types.py:43
[llm-options]: H:/shs/projects/shs-agent/src/shs_agent/llm/types.py:14
[provider-registry]: H:/shs/projects/shs-agent/src/shs_agent/llm/registry.py:20
[ollama]: H:/shs/projects/shs-agent/src/shs_agent/llm/provider/ollama.py:7
[ollama-chat]: H:/shs/projects/shs-agent/src/shs_agent/llm/provider/ollama.py:33
[openai]: H:/shs/projects/shs-agent/src/shs_agent/llm/provider/openai.py:8
[openai-chat]: H:/shs/projects/shs-agent/src/shs_agent/llm/provider/openai.py:44
[tool-api]: H:/shs/projects/shs-agent/src/shs_agent/tool/__init__.py:1
[tool-model]: H:/shs/projects/shs-agent/src/shs_agent/tool/model.py:5
[tool-registry]: H:/shs/projects/shs-agent/src/shs_agent/tool/registry.py:4
[executor]: H:/shs/projects/shs-agent/src/shs_agent/tool/executor.py:6
[builtin]: H:/shs/projects/shs-agent/src/shs_agent/tool/builtin.py:1
[storage-api]: H:/shs/projects/shs-agent/src/shs_agent/storage/__init__.py:2
[storage-base]: H:/shs/projects/shs-agent/src/shs_agent/storage/base.py:9
[storage-model]: H:/shs/projects/shs-agent/src/shs_agent/storage/model.py:20
[sql-schema]: H:/shs/projects/shs-agent/src/shs_agent/storage/provider/_sqlalchemy.py:28
[sql]: H:/shs/projects/shs-agent/src/shs_agent/storage/provider/_sqlalchemy.py:93
[sql-write]: H:/shs/projects/shs-agent/src/shs_agent/storage/provider/_sqlalchemy.py:104
[sql-read]: H:/shs/projects/shs-agent/src/shs_agent/storage/provider/_sqlalchemy.py:188
[sql-search]: H:/shs/projects/shs-agent/src/shs_agent/storage/provider/_sqlalchemy.py:221
[sqlite]: H:/shs/projects/shs-agent/src/shs_agent/storage/provider/sqlite.py:12
[postgres]: H:/shs/projects/shs-agent/src/shs_agent/storage/provider/postgresql.py:9
[mysql]: H:/shs/projects/shs-agent/src/shs_agent/storage/provider/mysql.py:9
[packaging]: H:/shs/projects/shs-agent/pyproject.toml:6
[readme]: H:/shs/projects/shs-agent/README.md:1
[demo]: H:/shs/projects/shs-agent/test.py:18
[demo-calculate]: H:/shs/projects/shs-agent/test.py:37
[demo-tools]: H:/shs/projects/shs-agent/test.py:132
[demo-host]: H:/shs/projects/shs-agent/test.py:175
[demo-main]: H:/shs/projects/shs-agent/test.py:217
[db-demo]: H:/shs/projects/shs-agent/examples/test_db.py:12
[venv-config]: H:/shs/projects/shs-agent/.venv/pyvenv.cfg:1
[installed-meta]: H:/shs/projects/shs-agent/.venv/Lib/site-packages/shs_agent-0.1.0.dist-info/METADATA:1
[egg-requires]: H:/shs/projects/shs-agent/src/shs_agent.egg-info/requires.txt:1

