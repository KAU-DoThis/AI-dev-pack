# v1.1 구현과 실제 Workspace 검증

2026-10-20. 기준: [목표 아키텍처](../architecture/SHS_AGENT_TARGET_ARCHITECTURE.md).
기존 Runtime·Context·Communication·SubagentManager·Tracer를 확장했다. 별도 Root/Child 실행 엔진,
새 스케줄러, Host를 흉내 내는 Task는 추가하지 않았다.

## 테스트 정지 원인

샌드박스의 Windows Python 3.12에서 `asyncio.run()`만 호출해도 내부 event loop 생성이 멈췄다.
faulthandler로 확인한 대기 위치는 `socket.accept` → `_fallback_socketpair` →
Proactor event loop의 `_make_self_pipe`였다. Task coroutine 실행 이전의 문제다.
같은 Python으로 샌드박스 밖에서 기존 전체 240개 테스트가 47.638초에 통과했다.
생산 코드에 event loop 정책 교체, 임의 timeout, 강제 취소 같은 우회는 넣지 않았다.
이후 비동기 테스트는 같은 샌드박스 제한을 피해 실행했다.

## 구현

| 경계 | 구현 |
|---|---|
| Agent 엔진 | Root와 모든 자손이 `ProblemSolver._assemble_task` → 같은 TaskRuntime을 사용 |
| 부모 연결 | 단일 `ParentConnection`; 기존 RootCommunication/ChildCommunication 이름은 같은 클래스의 호환 alias |
| Host 관계 | Root parent_task_id=None 유지. 질문 ID별 비동기 전송/대기, 다중 질문, Host→Agent 질문과 답변 지원 |
| 프롬프트 | `agent/prompts.py`에 고정 문구. Builder가 역할/관계/실제 tools/선택적 Workspace 블록 조립. 원본 불변 |
| Workspace | 명시적 기존 디렉터리, 논리 mount/cwd, 읽기 전용·재위임 권한·revision. 프로세스 cwd 변경과 자동 폴더 생성 없음 |
| 기본 파일 도구 | list/read/search/write/edit/delete가 같은 resolver 사용. UTF-8, 부분 조회, 정확한 문자열 수정, 비재귀 삭제 |
| 자식 환경 | 작은 instruction/context/role/result_requirements + workspace_paths/tool_ids. profile로 별도 Workspace도 지정 |
| 자원 요청 | 부모가 자신의 명시적 위임 권한으로 제공/거절하거나 상위로 전달. Host가 Workspace 또는 새 Tool 객체 공급 |
| 적용 경계 | approved와 applied 구분. 현재 도구 batch 이후 환경/도구 교체, 다음 요청에서 실제 적용된 schema 제공 |
| 부모 계획 | `plan_read(scope="parent")`는 직계 부모의 읽기 전용 사본. 없으면 plan=null |
| Context 복구 | 확인된 ContextOverflow만 분할 요약→재조립→같은 논리 단계 재호출. 완료 도구 재실행 없음 |
| 계측 | 한도 출처/미제공, 입력 추정/Provider 실측/누적 usage 구분. prompt 블록 해시·변수, 복구 lineage 기록 |
| 트리 관찰 | task sequence와 tree_sequence 분리. 원본 참조 index, 중간 cursor 구독, 같은 순서로 저장/재생 |
| 패키징 | SQLAlchemy를 storage extra로 분리. 최소 설치 필수 의존성 없음. docs/dev 경로와 새 예제 포함 |

구체 API는 [Host 사용 계약](../HOST_USAGE.md)의 v1.1 절에 있다.

`host_ask`는 기존 전송+대기 호환 API로 유지한다. 새 Root/Child 코드는 모두
`parent_ask` → 필요 시 `agent_wait_reply` → `agent_reply`를 사용한다.
Root의 전체 Manager는 Host의 관리 핸들이며 Root Agent의 다른 실행 엔진을 의미하지 않는다.

기존 catalog+allowlist의 `grant_tool`은 유지하고, 명시적인 Host 공급 API `supply_tool`을 추가했다.
일반 질문의 “허용” 답변은 실행 권한으로 해석하지 않는다. 부모의 tool grant는 실제 적용된 Tool과
`delegable_tool_ids`를 확인한다. 파일 handler는 부모 cwd를 복사하지 않고 자식 Workspace에 다시 연결한다.
커스텀 executor의 동작을 잃을 수 있는 handler 복사는 거부한다.

## 실제 실행에서 발견하고 고친 문제

1. 마지막 assistant 자료 블록을 일부 Ollama 템플릿이 이어쓰기 입력으로 해석했다.
   Builder가 필요한 경우 다음 행동을 요청하는 user 메시지를 덧붙인다. 원본 Conversation은 그대로다.
2. 모델이 Workspace의 기본 파일 도구를 추가 tool_ids에 넣으려 했다.
   spawn 설명과 거절 결과에 Workspace가 자동으로 파일 도구를 제공한다는 점을 명시했다.
3. Host 명령 예제가 `/workspace/file.py`를 검사만 하고 Python에는 논리 경로 그대로 넘겼다.
   실제 실행 인자도 물리 경로로 변환하도록 수정하고 subprocess 재현 테스트를 추가했다.
4. Agent가 만든 테스트는 통과했지만 출력이 JSON이 아닌 Python dict 문자열인 경우가 있었다.
   독립적인 Host 검사 `verify_requirements`와 종료 후 재검사를 추가했다.
5. 요약 요청에서 초과가 나도 큰 최초 budget 때문에 같은 chunk를 다시 고를 수 있었다.
   다음 budget을 직전 실제 요약 요청보다 작게 제한하여 동일 요청 반복을 막았다.
6. Provider 오류에서 얻은 한도는 다른 모델의 근거가 아니므로 모델 변경 시 폐기한다.

실패한 실행도 `artifacts/workspace-coding-v11*` 아래 별도로 보존했다.
시스템 Python의 구형 Ollama SDK는 think 옵션을 거부했으므로 프로젝트의 기존 0.6.2 SDK를 사용했다.
선택적 Ollama 의존성에도 이 예제를 검증한 최소 버전을 명시했다.

## 검증 근거와 재현

오프라인 계약 검증은 `tests/test_parent_connection.py`, `tests/test_workspace.py`,
`tests/test_context_overflow.py`와 기존 회귀 테스트에 있다. 제어 가능한 gate로 안전한 적용 경계를
확인하고 Workspace는 실제 임시 파일과 subprocess를 사용한다.
Windows가 심볼릭 링크 생성을 허용하지 않는 환경에서는 해당 실제 링크 테스트 한 개를 skip한다.
이는 실제 링크 생성 경로를 검증했다고 주장하는 결과가 아니다.

```powershell
$env:PYTHONPATH = 'src'
.venv\Scripts\python.exe -B -m unittest discover -s tests -q
uv build --offline
.venv\Scripts\python.exe -B tests/verify_distribution.py
.venv\Scripts\python.exe -B examples/workspace_coding_host.py --workdir build/coding --model qwen3.5:9b
.venv\Scripts\python.exe -B examples/context_recovery_host.py --workdir build/recovery --model qwen3.5:9b
```

`verify_distribution.py`는 wheel/sdist의 실제 소스 일치, optional dependency metadata,
패키지에 담긴 예제·문서, `-I -S`의 최소 환경에서 전체 테스트와 Fake Host/replay를 검사한다.
SDK·SQLAlchemy·torch를 가져오지 않아도 오프라인 엔진과 예제가 동작해야 한다.
실행 로그는 `artifacts/v11-*.log`, 실제 코딩 결과는 해당 Workspace의 `host-report.json`과 `tree.json`에 보존한다.

최종 배포 검증은 wheel과 sdist 각각 **272개 중 271개 통과, 1개 skip, 실패 0개**였다.
소요 시간은 각각 60.378초와 55.156초이며, skip은 위의 Windows 실제 심볼릭 링크 생성 항목이다.
두 배포 형식 모두 격리된 최소 환경의 Fake Host 실행과 저장된 트리 replay도 통과했다.
`artifacts/v11-distribution.log`에 전체 결과를 보존했다.

최종 팀 실행 `artifacts/workspace-coding-v11-passed/host-report.json`:
qwen3.5:9b, Host가 제공한 기존 Workspace, 루트 + 구현 자식 + 테스트 자식 총 3개 Task가
모두 `completed/task_complete`로 종료했다. 원래 CLI의 실패 3건을 실제 파일 수정으로 해결했고,
생성한 unittest 4개와 Host의 독립 CLI 검사 4개가 모두 통과하여 `accepted=true`였다.
전체 트리는 1,093개 사건이다. 초기/최종 stdout·stderr·exit_code, 생성 코드와 테스트,
모든 실제 도구 인자와 결과를 보존했다. 이전 `verified` 실행은 테스트가 통과해도 단계 한도에
도달했으므로 실패로 보존했고, 성공 실행과 혼동하지 않는다.

Context 실모델 검증 `artifacts/context-recovery-v11/host-report.json`:
qwen3.5:9b가 실제 요약을 만들고 같은 Task에서 재호출하여 result.txt를 작성·재조회했다.
130개 관찰이 담긴 원본 notes.txt는 보존됐고, compactions=1, completed/accepted=true였다.
초과 신호는 **Host가 한 번 주입**했다. Ollama 서버의 자연적인 초과 오류를 관측했다는 주장이 아니다.
OpenAI Adapter의 구조화 초과 코드 변환은 SDK 경계의 제어 가능한 테스트로 확인했다.
출력 제한을 context 초과로 처리하지 않는 기준은
[공식 Chat Completions finish_reason 정의](https://developers.openai.com/api/reference/resources/chat)와 대조했다.

## 유지한 정책과 범위

기존 부모 완료/자식 종료, 취소 전파, Host timeout 정책을 유지했다.
새 scheduler·자동 재시작·미답변 우선순위·TTL·기록 삭제·Pause/Resume 정책을 추가하지 않았다.
문서에서 확정한 기본 context 초과 복구만 기본 동작으로 바꿨으며,
문자 수 기반 선제 요약은 명시적 호환 옵션이다.

Workspace는 파일 도구의 논리 접근 범위다. OS sandbox·컨테이너·명령 실행 격리·hard link 보안은 Host 책임이다.
중첩 mount의 상위 전체 위임은 거부하며 leaf 범위를 명시해야 한다.
기본 도구는 심볼릭 링크/junction 자체 삭제를 Host 작업으로 남긴다.
Storage 자동 저장, Checkpoint/Resume, 실행 중 Provider 교체, 장기 Memory는 이번 범위에 포함하지 않는다.
한도를 알 수 없는 Provider는 unknown으로 기록하며, 토큰 추정치는 tokenizer 실측이 아니다.
필수 입력이나 하나의 완결된 묶음도 요약에 담을 수 없으면 `context_unrecoverable`로 명시적으로 종료한다.
