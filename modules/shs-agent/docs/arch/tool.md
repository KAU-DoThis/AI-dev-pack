
flowchart TD
    A["TaskRuntime"]
    B["Tool Calls 처리"]
    C["ToolExecutor"]
    D["Tool 조회 및 인자 검증"]
    E["등록된 Tool Handler 실행"]
    F["ToolResult 반환"]
    G["Conversation 기록 및 결과 반영"]

    R["ToolRegistry"]
    S[("ToolSpec / Schema / Handler 참조")]
    CB["ContextBuilder"]

    A --> B
    B --> C
    C --> D
    D --> E
    E --> F
    F --> G

    D -.->|"도구명 조회"| R
    R --> S
    R -.->|"사용 가능한 Tool Schema"| CB

    classDef object fill:#DBEAFE,stroke:#2563EB,color:#1E40AF,stroke-width:2px
    classDef action fill:#FEF3C7,stroke:#D97706,color:#92400E,stroke-width:2px
    classDef data fill:#F3E8FF,stroke:#9333EA,color:#6B21A8,stroke-width:2px

    class A,C,R,CB object
    class B,D,E,G action
    class F,S data
