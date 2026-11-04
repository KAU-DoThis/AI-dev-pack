
flowchart TD
    %% =========================
    %% 1. Task 생성
    %% =========================
    REQ["Task 요청자"]
    SOLVER["ProblemSolver"]
    CREATE["Task 실행 환경 구성"]
    RT["TaskRuntime"]

    REQ -->|"TaskRequest"| SOLVER
    SOLVER --> CREATE
    CREATE --> RT

    %% =========================
    %% 2. TaskRuntime - Agent Loop
    %% =========================
    subgraph LOOP["TaskRuntime - Agent Loop"]
        INPUT["대기 입력 및 명령 적용"]
        LIMIT{"실행 가능한 상태인가?"}

        BUILDER["ContextBuilder"]
        BUILD["Step Context 구성"]
        CORE["AgentCore"]
        LLM["LLM Provider"]
        RESPONSE{"LLM 호출 결과"}

        VALID{"응답 적용 가능한가?"}
        SAVE["LLM 응답 Conversation 기록"]
        ACTION{"다음 행동"}

        COMPACT["Compaction 수행"]

        DISPATCH["Tool Calls 실행 요청"]
        RECORD["Tool Result Conversation 기록"]
        OUTCOME{"Tool 실행 결과 처리"}

        WAIT["응답 또는 작업 결과 대기"]
        COMPLETE["task_complete 검증"]
        FINISH["TaskResult 생성"]
        STOP["취소 / 한도 / 복구 불가 오류 종료"]

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
        VALID -->|"무효"| INPUT

        SAVE --> ACTION

        ACTION -->|"Tool Calls"| DISPATCH
        ACTION -->|"중간 텍스트"| INPUT

        RECORD --> OUTCOME

        OUTCOME -->|"다음 판단"| INPUT
        OUTCOME -->|"대기 필요"| WAIT
        OUTCOME -->|"완료 요청"| COMPLETE

        WAIT -->|"답변 / 결과 / 새 요청"| INPUT

        COMPLETE -->|"유효"| FINISH
        COMPLETE -->|"무효"| INPUT

        STOP --> FINISH

        %% State 변경
        INPUT -.-> STATE_UPDATE
        CORE -.-> STATE_UPDATE
        DISPATCH -.-> STATE_UPDATE
        WAIT -.-> STATE_UPDATE
        COMPACT -.-> STATE_UPDATE
        FINISH -.-> STATE_UPDATE
    end

    RT --> INPUT

    %% =========================
    %% 3. Tool System
    %% =========================
    subgraph TOOL_SYSTEM["Tool System"]
        SESSION_TOOLS["SessionTools"]
        REGISTRY["ToolRegistry"]
        TOOL_SPEC[("ToolSpec / Schema / Handler 참조")]

        EXECUTOR["ToolExecutor"]
        LOOKUP["Tool 조회 및 인자 검증"]
        RUN_TOOL["등록된 Handler 호출"]
        HANDLER["Tool Handler"]
        TOOL_RESULT[("ToolResult / 제어 결과")]

        SESSION_TOOLS --> REGISTRY
        SESSION_TOOLS --> EXECUTOR

        REGISTRY --> TOOL_SPEC

        EXECUTOR --> LOOKUP
        LOOKUP -.->|"도구명 조회"| REGISTRY
        LOOKUP --> RUN_TOOL
        RUN_TOOL --> HANDLER
        HANDLER --> TOOL_RESULT
    end

    CREATE -.->|"도구 구성 및 연결"| SESSION_TOOLS

    %% Tool Schema는 LLM 입력에도 사용
    REGISTRY -.->|"사용 가능한 Tool Schema"| BUILDER

    %% Runtime은 도구 실행을 요청하고 결과를 수신
    DISPATCH --> EXECUTOR
    TOOL_RESULT --> RECORD

    %% =========================
    %% 4. Task별 데이터 및 State
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
    CREATE -.-> CHANNEL
    CREATE -.-> CONTEXT
    CREATE -.-> PLANNING

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

    %% Handler는 필요한 작업 데이터에 접근
    HANDLER -.->|"계획 관련 도구"| PLANNING
    HANDLER -.->|"메모 / 대화 관련 도구"| CONTEXT

    %% =========================
    %% 5. 실행 트리 공통 Tracer
    %% =========================
    subgraph OBS["실행 트리 전체의 공통 Tracer"]
        TRACER["Tracer"]
        TRACE_DATA[("Events / Artifacts")]
        LIVE["실시간 이벤트 제공"]
        HISTORY["Snapshot / Replay"]

        TRACER --> TRACE_DATA
        TRACER --> LIVE
        TRACE_DATA --> HISTORY
    end

    CREATE -.->|"신규 생성 또는 기존 Tracer 공유"| TRACER

    CHANNEL -.->|"State / 메시지 이벤트"| TRACER
    CORE -.->|"LLM 요청 / 응답"| TRACER
    EXECUTOR -.->|"Tool 시작 / 종료 / 실패"| TRACER
    PLANNING -.->|"Plan 변경"| TRACER
    COMPACT -.->|"Context 압축"| TRACER
    FINISH -.->|"Task 종료"| TRACER

    %% =========================
    %% 6. 외부 관찰
    %% =========================
    UI["Host 시각화 UI"]

    LIVE --> SESSION
    HISTORY --> SESSION
    SESSION -.->|"실시간 관찰 / Replay"| UI

    %% =========================
    %% 스타일
    %% =========================
    classDef external fill:#F3F4F6,stroke:#6B7280,color:#374151,stroke-width:2px
    classDef object fill:#DBEAFE,stroke:#2563EB,color:#1E40AF,stroke-width:2px
    classDef action fill:#FEF3C7,stroke:#D97706,color:#92400E,stroke-width:2px
    classDef decision fill:#FCE7F3,stroke:#DB2777,color:#9D174D,stroke-width:2px
    classDef data fill:#F3E8FF,stroke:#9333EA,color:#6B21A8,stroke-width:2px

    class REQ,LLM,UI external

    class SOLVER,RT,BUILDER,CORE,SESSION_TOOLS,REGISTRY,EXECUTOR,HANDLER,SESSION,CHANNEL,CONTEXT,PLANNING,TRACER object

    class CREATE,INPUT,BUILD,SAVE,COMPACT,DISPATCH,RECORD,WAIT,COMPLETE,FINISH,STOP,STATE_UPDATE,LOOKUP,RUN_TOOL,STATE_INIT,LIVE,HISTORY action

    class LIMIT,RESPONSE,VALID,ACTION,OUTCOME decision

    class TOOL_SPEC,TOOL_RESULT,TASKSTATE,AGENTSTATE,CONV,MEMO,SUMMARY,CURRENTPLAN,TRACE_DATA data
