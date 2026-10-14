# ContextMD map

The parts of ContextMD and how they connect. Chromeria's Map tab colours each part
from GitHub: planned, working, broken or done.

Each part is declared once as `ID["Name"]`. The comment line `%% ID: refs` under it
names the part's Issues and pull requests; the first ref is the primary ref, which
decides planned, working or done. `%% ID: context` marks a part this project draws
but does not track. GitHub ignores these comment lines.

Change this map in the pull request that adds, splits, merges or removes a part.

```mermaid
flowchart TD
    S["Session in Chromeria"]
    %% S: context
    R["1 Record: Agent Observer sync"]
    %% R: context
    C["2 Capture: extract and judge"]
    %% C: #7 PR #22
    CJ["Judge calibration, blind rounds"]
    %% CJ: #8
    MC["Capture on every machine, scheduled"]
    %% MC: #42
    D["3 Cause: route and historical miss join"]
    %% D: #16
    W["4a Shape and add-only apply"]
    %% W: PR #44 #39
    ST["Great Library store"]
    %% ST: context
    KV["4b Key variant proposal"]
    %% KV: #17
    SI["4c Serving health check"]
    %% SI: #40 #64 PR #65
    CX["4d Cue miss example to retraining"]
    %% CX: #16 #4
    ES["4e Escalation proposal"]
    %% ES: #17
    RF["4f Reflection update or remove"]
    %% RF: #10
    DI["4g Defect Issues, tracker protocol"]
    %% DI: #9
    AS["AgentsMD procedure store"]
    %% AS: context
    EK["5 Exact-key serving"]
    %% EK: #3 #11
    MK["Must-know rule before its action"]
    %% MK: toolboxmd/chromeria PR #233 toolboxmd/chromeria#231
    PK["Path keys on shell commands"]
    %% PK: toolboxmd/chromeria#234
    FR["Pointer lifts only on a full read"]
    %% FR: toolboxmd/chromeria PR #226 toolboxmd/chromeria#225
    FL["Serving on Codex, Grok, OpenCode"]
    %% FL: #12
    GD["Grok deny defects"]
    %% GD: toolboxmd/chromeria#229 toolboxmd/chromeria#230 toolboxmd/chromeria#228
    CU["5 Cue lane: scoring service and Chromeria lane"]
    %% CU: PR #36 #13 toolboxmd/chromeria PR #227
    GL["Great Library pointer and registration on every machine"]
    %% GL: #34
    SR["6 Serve evidence report"]
    %% SR: PR #38 #15
    SC["6 Serve card and label prompt"]
    %% SC: PR #45 #15
    LJ["6 Labeler judge runs"]
    %% LJ: #15
    RP["7 Twice-weekly report"]
    %% RP: #17
    CI["Waiting items into Cue entries"]
    %% CI: #19
    WF["Cue input window format"]
    %% WF: #20
    SP["Per-row proof that a serve was shown"]
    %% SP: toolboxmd/chromeria#245
    CK["Command keys fire on runs, not mentions"]
    %% CK: toolboxmd/chromeria#232
    RD["Readers called by path; OpenCode reservations"]
    %% RD: toolboxmd/chromeria#221
    LF["Library format, flat"]
    %% LF: #41
    PT["Pointer-trial retirement"]
    %% PT: #43
    NC["Non-code capture"]
    %% NC: #35
    GH["GPU serving host recovers on its own"]
    %% GH: #6
    RL["Retrieval lane, only if needed"]
    %% RL: #14

    S --> R --> C --> D
    CJ -. gates writes .-> C
    MC -.-> C
    D -->|needs a home| W --> ST --> EK
    AS --> EK
    D -->|key_mismatch| KV --> W
    D -->|serving or matcher defect| SI
    D -->|cue_missed| CX --> CU
    D -->|ignored| ES --> W
    D -->|wrong| RF --> ST
    D -->|defect| DI
    EK --- MK
    EK --- PK
    EK --- FR
    EK --> FL
    FL --- GD
    GL --> S
    EK --> SR
    CU --> SR
    SR --> SC --> LJ
    LJ --> D
    EK -->|next moment| S
    CU -->|next moment| S
    LJ --> RP
    D --> RP
    RP --> KV
    RP --> ES
    W --> CI --> CU
    WF -.-> CU
    GH -.-> CU
    SP --> SR
    EK --- CK
    EK --- RD
    LF -.-> ST
    PT --> EK
    NC -.-> C
    EK -.-> RL
```

## Work that is not a part

Every piece of work on the path to the Objective is a part above, except these:

| Work | Why it is not a part |
| --- | --- |
| First bake (#39) | A run of one lesson through the whole loop, not a piece of it. It is bound on W. |
| Great Library flat store | Drawn as ST, a context part: its tracking lives outside this public repository. |
| "Read in full" wording in AgentsMD (toolboxmd/agentsmd#224, merged) | AgentsMD's own change, inside the AS context part. |
| Agent Observer event search and lock failures (toolboxmd/agent-observer#70, #71, done) | Agent Observer's own work, inside the R context part. |
