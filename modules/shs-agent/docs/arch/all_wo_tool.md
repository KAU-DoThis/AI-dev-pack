
flowchart TD
    %% =========================
    %% Task 생성
    %% =========================
    REQ["Task 요청자"]
    SOLVER["ProblemSolver"]
    CREATE["Task 실행 환경 구성"]
    RT["TaskRuntime"]

    REQ -->|"TaskRequest"| SOLVER
    SOLVER --> CREATE
    CREATE --> RT

    %% =========================
    %% TaskRuntime 내부 동작
    %% =========================
    subgraph LOOP["TaskRuntime - Agent Loop"]
        INPUT["대기 입력 및 명령 적용"]
        LIMIT{"실행 가능한 상태인가?"}

        BUILDER["ContextBuilder"]
        BUILD["Step Context 생성"]
        CORE["AgentCore"]
        LLM["LLM Provider"]
        RESPONSE{"LLM 호출 결과"}

        VALID{"응답을 적용할 수 있는가?"}
        SAVE["LLM 응답 Conversation 기록"]
        ACTION{"LLM의 다음 행동"}

        COMPACT["Compaction 수행"]

        DISPATCH["Tool Calls 처리"]
        TOOLS["Tool System"]
        TYPE{"Tool 종류"}

        NORMAL["일반 Tool 실행"]
        PLAN["Planning / Memo / Conversation 처리"]

        ASK["ask 도구"]
        ASK_SEND["Task 요청자에게 질문 전달"]
        ASK_WAIT["답변 대기"]

        DELEGATE["작은 TaskRequest 작성"]
        NEW_AGENT["동일 Solver로 새 Agent 생성"]
        CHILD_SESSION["새 TaskSession 반환"]

        CHILD_WAIT["위임한 Task 상태 조회 / 대기"]

        RECORD["Tool Result Conversation 기록"]

        COMPLETE["task_complete 검증"]
        FINISH["TaskResult 생성"]
        STOP["취소 / 한도 / 오류 종료"]

        STATE_UPDATE["현재 State 갱신 요청"]

        INPUT --> LIMIT
        LIMIT -->|"Yes"| BUILDER
        LIMIT -->|"No"| STOP

        BUILDER --> BUILD
        BUILD --> CORE
        CORE --> LLM
        LLM --> RESPONSE

        RESPONSE -->|"정상"| VALID
        RESPONSE -->|"Context 초과"| COMPACT
        COMPACT --> INPUT
        RESPONSE -->|"기타 오류"| STOP

        VALID -->|"Yes"| SAVE
        VALID -->|"지시 변경 등으로 무효"| INPUT

        SAVE --> ACTION

        ACTION -->|"Tool Calls"| DISPATCH
        ACTION -->|"중간 텍스트만 있음"| INPUT

        DISPATCH --> TOOLS
        TOOLS --> TYPE

        TYPE -->|"일반 도구"| NORMAL
        TYPE -->|"내부 관리 도구"| PLAN
        TYPE -->|"ask"| ASK
        TYPE -->|"작업 위임"| DELEGATE
        TYPE -->|"자식 상태 / 결과"| CHILD_WAIT
        TYPE -->|"task_complete"| COMPLETE

        ASK --> ASK_SEND
        ASK_SEND --> ASK_WAIT
        ASK_WAIT -->|"답변 도착"| RECORD
        ASK_WAIT -->|"처리할 새 요청"| INPUT

        DELEGATE --> NEW_AGENT
        NEW_AGENT --> CHILD_SESSION
        CHILD_SESSION --> RECORD

        CHILD_WAIT -->|"결과 또는 대기 해제"| RECORD

        NORMAL --> RECORD
        PLAN --> RECORD
        RECORD --> INPUT

        COMPLETE --> FINISH
        STOP --> FINISH

        %% State 변경
        INPUT -.-> STATE_UPDATE
        CORE -.-> STATE_UPDATE
        DISPATCH -.-> STATE_UPDATE
        ASK_WAIT -.-> STATE_UPDATE
        CHILD_WAIT -.-> STATE_UPDATE
        COMPACT -.-> STATE_UPDATE
        FINISH -.-> STATE_UPDATE
    end

    RT --> INPUT

    %% =========================
    %% Task별 독립 데이터 및 State
    %% =========================
    subgraph DATA["Task별 독립 데이터 및 상태"]
        SESSION["TaskSession"]
        CHANNEL["TaskChannel"]

        STATE_INIT["초기 State 생성"]
        TASKSTATE[("TaskState")]
        AGENTSTATE[("AgentState")]

        CONTEXT["TaskContext"]
        CONV[("Conversation")]
        MEMO[("Memo")]
        SUMMARY[("Summary")]

        PLANNING["Planning"]
        CURRENTPLAN[("Current Plan")]
    end

    CREATE -.-> STATE_INIT
    CREATE -.-> SESSION

    STATE_INIT --> TASKSTATE
    STATE_INIT --> AGENTSTATE

    CHANNEL -.->|"현재 상태 소유"| TASKSTATE
    CHANNEL -.->|"현재 활동 소유"| AGENTSTATE

    REQ -.->|"Task 제어"| SESSION
    SESSION -.->|"명령 / 상태 조회"| CHANNEL
    CHANNEL -.->|"수신 메시지"| INPUT

    STATE_UPDATE -.->|"상태 변경 요청"| CHANNEL

    CONTEXT --> CONV
    CONTEXT --> MEMO
    CONTEXT --> SUMMARY
    PLANNING --> CURRENTPLAN

    CONV -.-> BUILDER
    MEMO -.-> BUILDER
    SUMMARY -.-> BUILDER
    CURRENTPLAN -.-> BUILDER

    INPUT -.->|"수신 내용 기록"| CONV
    SAVE -.-> CONV
    RECORD -.-> CONV
    COMPACT -.-> SUMMARY
    PLAN -.->|"변경 적용"| PLANNING

    %% =========================
    %% 작업 위임 - 동일한 Agent 구조
    %% =========================
    CHILD_RT["새 Agent의 TaskRuntime"]

    NEW_AGENT -.->|"동일한 실행 구조 생성"| CHILD_RT

    %% =========================
    %% 공통 Tracer
    %% =========================
    subgraph OBS["실행 트리 전체의 공통 Tracer"]
        TRACER["Tracer"]
        TRACE_DATA[("전체 Events / Artifacts")]
        LIVE["실시간 이벤트 제공"]
        HISTORY["Snapshot / Replay"]

        TRACER --> TRACE_DATA
        TRACER --> LIVE
        TRACE_DATA --> HISTORY
    end

    %% 신규 트리면 생성, 위임된 Task면 공유
    CREATE -.->|"신규 생성 또는 기존 Tracer 연결"| TRACER
    NEW_AGENT -.->|"기존 Tracer 공유"| TRACER

    %% 각 Agent의 실행 이벤트 수집
    CHANNEL -.->|"State 변경 / 메시지 이벤트"| TRACER
    CORE -.->|"LLM 요청 / 응답"| TRACER
    DISPATCH -.->|"Tool 호출 / 결과"| TRACER
    PLANNING -.->|"Plan 변경"| TRACER
    ASK_SEND -.->|"질문 / 통신"| TRACER
    NEW_AGENT -.->|"Task 위임 / 생성 관계"| TRACER
    COMPACT -.->|"Context 압축"| TRACER
    FINISH -.->|"Task 종료"| TRACER

    %% 새 Agent도 동일한 Tracer에 기록
    CHILD_RT -.->|"자식 Agent 실행 이벤트"| TRACER

    %% =========================
    %% Host 관찰 인터페이스
    %% =========================
    UI["Host 시각화 UI"]

    LIVE -->|"이벤트 스트림"| SESSION
    HISTORY -->|"과거 기록"| SESSION
    SESSION -->|"실시간 관찰 / Replay"| UI

    %% =========================
    %% 스타일
    %% =========================
    classDef external fill:#F3F4F6,stroke:#6B7280,color:#374151,stroke-width:2px
    classDef object fill:#DBEAFE,stroke:#2563EB,color:#1E40AF,stroke-width:2px
    classDef action fill:#FEF3C7,stroke:#D97706,color:#92400E,stroke-width:2px
    classDef decision fill:#FCE7F3,stroke:#DB2777,color:#9D174D,stroke-width:2px
    classDef data fill:#F3E8FF,stroke:#9333EA,color:#6B21A8,stroke-width:2px

    class REQ,LLM,UI external

    class SOLVER,RT,BUILDER,CORE,TOOLS,ASK,SESSION,CHANNEL,CONTEXT,PLANNING,TRACER,CHILD_RT object

    class CREATE,INPUT,BUILD,SAVE,COMPACT,DISPATCH,NORMAL,PLAN,ASK_SEND,ASK_WAIT,DELEGATE,CHILD_SESSION,CHILD_WAIT,RECORD,COMPLETE,FINISH,STOP,STATE_INIT,STATE_UPDATE,NEW_AGENT,LIVE,HISTORY action

    class LIMIT,RESPONSE,VALID,ACTION,TYPE decision

    class CONV,MEMO,SUMMARY,CURRENTPLAN,TASKSTATE,AGENTSTATE,TRACE_DATA data
