
flowchart TD
    %% ==================================
    %% taskrequest 를 받은 후 agent 상태
    %% ==================================
    SOLVER["ProblemSolver"]
    CREATE["Task 실행 환경 구성"]
    USER --> HOST
    HOST -->|"Session API 호출"| SOLVER

    SOLVER --> CREATE

    CREATE --> SESSION["TaskSession"]
    CREATE --> CHANNEL["TaskChannel"]
    CREATE --> RUNTIME["TaskRuntime"]
    CREATE --> CORE["AgentCore"]
    CREATE --> BUILDER["ContextBuilder"]
    CREATE --> TOOLS["Tool System"]
    CREATE --> TRACER["Tracer"]

    CREATE --> CONTEXT["TaskContext"]
    CONTEXT --> CONV[("Conversation")]
    CONTEXT --> MEMO[("Memo")]
    CONTEXT --> SUMMARY[("Summary")]

    CREATE --> PLANNING["Planning"]
    PLANNING --> PLAN[("Plan - 선택적으로 생성")]

    SESSION -.->|"Host 또는 Agent 요청자에게 제공"| REQUESTER["HOST"]

    classDef external fill:#F3F4F6,stroke:#6B7280,color:#374151
    classDef object fill:#DBEAFE,stroke:#2563EB,color:#1E40AF,stroke-width:2px
    classDef action fill:#FEF3C7,stroke:#D97706,color:#92400E,stroke-width:2px
    classDef data fill:#F3E8FF,stroke:#9333EA,color:#6B21A8,stroke-width:2px

    class SOLVER,SESSION,CHANNEL,RUNTIME,CORE,BUILDER,TOOLS,TRACER,CONTEXT,PLANNING object
    class CREATE action
    class CONV,MEMO,SUMMARY,PLAN data
    class REQUESTER external
    class USER,HOST external
