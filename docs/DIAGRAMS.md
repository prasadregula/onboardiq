# OnboardIQ — architecture and design diagrams

GitHub renders these Mermaid diagrams directly. Each one answers one question.

1. [System architecture](#1-system-architecture) — what runs where
2. [Processing one case](#2-processing-one-case) — the request flow, step by step
3. [Three checkpoints on every tool call](#3-three-checkpoints-on-every-tool-call) — how an agent action is authorized
4. [Agent identities and Azure access](#4-agent-identities-and-azure-access) — least privilege per agent
5. [Decision gates](#5-decision-gates) — how the outcome is decided
6. [Trust boundaries](#6-trust-boundaries) — where untrusted input enters
7. [Deployment pipeline](#7-deployment-pipeline) — how code reaches Azure

---

## 1. System architecture

```mermaid
flowchart TB
    analyst(["Analyst"])

    subgraph front["Front door"]
        direction LR
        entra["Microsoft Entra ID<br/>sign-in · app roles"]
        swa["Review queue<br/>Static Web Apps"]
    end

    subgraph ca["Container App: OnboardIQ API"]
        direction LR
        api["FastAPI<br/>auth · cases · review"] --> orch["Orchestrator<br/>agent"] --> pep["Policy<br/>enforcement point"] --> tools["7 typed tools<br/>own identity each"] --> gates["Decision<br/>gates"]
    end

    subgraph svc["Azure services (managed identity, no secrets)"]
        direction LR
        aoai["Azure OpenAI<br/>gpt-4.1-mini · embeddings"]
        di["Document<br/>Intelligence"]
        sql[("Azure SQL<br/>cases · audit chain")]
        blob[("Blob Storage<br/>documents")]
        files[("Azure Files<br/>OFAC lists · policy index")]
    end

    subgraph ops["Operations"]
        direction LR
        job["OFAC refresh job<br/>daily, from US Treasury"]
        appi["Application Insights"]
        kv["Key Vault<br/>break-glass only"]
    end

    analyst --> front
    front -->|"bearer token"| ca
    ca --> svc
    ops --> svc
```

All service-to-service calls use managed identities. Azure SQL is reached with an Entra access token. No secrets in configuration.

---

## 2. Processing one case

```mermaid
sequenceDiagram
    autonumber
    actor A as Analyst
    participant W as Review app
    participant API as OnboardIQ API
    participant O as Orchestrator agent
    participant P as Policy enforcement point
    participant T as Tool (own identity)
    participant AZ as Azure service
    participant G as Decision gates
    participant L as Audit chain

    A->>W: Sign in (Entra ID) and click Run
    W->>API: POST /cases/{id}/run (bearer token)
    API->>API: Validate token, check app role
    API->>L: run_started (initiated_by = analyst)
    loop until record_decision
        API->>O: case brief
        O->>P: propose tool + arguments
        alt allowed
            P->>T: run under the tool's own identity
            T->>AZ: call with agent identity token
            AZ-->>T: result (or 403 if the identity lacks the role)
            T-->>O: findings
            T->>L: tool_call (agent identity + on behalf of analyst)
        else denied
            P-->>O: reason
            P->>L: tool_denied (who, which tool, why)
        end
    end
    O->>G: recommendation + findings + risk
    G->>G: apply fixed gates (model can only tighten)
    G->>L: policy_gate + decision
    G-->>API: outcome
    API-->>W: escalate / request_more / needs_review / auto_approve
    Note over API,W: escalate and needs_review open a task in the analyst queue
```

---

## 3. Three checkpoints on every tool call

```mermaid
flowchart TD
    start(["Orchestrator proposes a tool call"]) --> c1{"1 · Policy enforcement point<br/>caller allowed?<br/>no unknown arguments?<br/>ids belong to this case?<br/>decision text safe?"}
    c1 -- "no" --> deny["Denied before any code runs<br/>audit: tool_denied<br/>model is told why"]
    c1 -- "yes" --> c2["2 · Identity switch<br/>tool → agent role → managed identity<br/>(fixed table in code)"]
    c2 --> c3{"3 · Azure RBAC<br/>does this identity hold the role<br/>on this resource?"}
    c3 -- "no (403)" --> fail["Only this tool fails<br/>other agents continue<br/>case routed to an analyst<br/>GATE_CHECKS_INCOMPLETE"]
    c3 -- "yes" --> ok["Tool runs<br/>audit: tool_call with agent identity<br/>and the analyst it acted for"]
```

The first two checkpoints are code; the third is enforced by Azure, so even a bug in the code cannot exceed an identity's roles.

---

## 4. Agent identities and Azure access

```mermaid
flowchart LR
    subgraph ids["Managed identities"]
        o["orchestrator"]
        x["extraction"]
        s["screening"]
        r["risk"]
        d["decision"]
        a["API<br/>system-assigned"]
    end
    subgraph res["Azure resources"]
        aoai["Azure OpenAI"]
        di["Document Intelligence"]
        blob[("Blob Storage")]
        files[("OFAC lists<br/>read-only mount")]
        sql[("Azure SQL")]
    end
    o -->|"OpenAI User"| aoai
    r -->|"OpenAI User"| aoai
    x -->|"Cognitive Services User"| di
    x -->|"Blob Data Reader"| blob
    s -.->|"file mount, no RBAC role"| files
    a -->|"Blob Data Contributor"| blob
    a -->|"Entra token, db roles"| sql
    d ~~~ res
```

| Agent | Tools | Azure access | May call other tools |
|---|---|---|---|
| orchestrator | planning loop, consistency, address, ownership | Azure OpenAI | yes (only role allowed) |
| extraction | extract_document | Document Intelligence, Blob read | no |
| screening | screen_parties | none (read-only file mount) | no |
| risk | rate_risk, policy search | Azure OpenAI | no |
| decision | record_decision | none | no |
| API | HTTP, uploads | SQL, Blob read/write | no |

---

## 5. Decision gates

```mermaid
flowchart TD
    in(["Findings + risk rating + model recommendation"]) --> g0{"Required checks<br/>all completed?"}
    g0 -- "no" --> nr0["needs_review<br/>GATE_CHECKS_INCOMPLETE"]
    g0 -- "yes" --> g1{"Confirmed sanctions match?"}
    g1 -- "yes" --> e1["escalate<br/>GATE_SANCTIONS"]
    g1 -- "no" --> g2{"Confirmed PEP?"}
    g2 -- "yes" --> e2["escalate<br/>GATE_PEP_EDD"]
    g2 -- "no" --> g3{"Critical data conflict?<br/>(e.g. DOB differs on ID)"}
    g3 -- "yes" --> e3["escalate<br/>GATE_CRITICAL_DATA"]
    g3 -- "no" --> g4{"Risk rating High?"}
    g4 -- "yes" --> e4["escalate<br/>GATE_HIGH_RISK"]
    g4 -- "no" --> g5{"Gap the customer can fix?<br/>(missing or unreadable document,<br/>address mismatch, ownership gap)"}
    g5 -- "yes" --> rm["request_more<br/>GATE_MISSING_INFO"]
    g5 -- "no" --> g6{"Possible screening match?"}
    g6 -- "yes" --> nr1["needs_review<br/>GATE_POSSIBLE_MATCH"]
    g6 -- "no" --> g7{"Risk rating Medium?"}
    g7 -- "yes" --> nr2["needs_review<br/>GATE_MEDIUM_RISK"]
    g7 -- "no" --> stp["auto_approve<br/>GATE_STP"]
    stp -.->|"if the model recommended something stricter,<br/>the stricter outcome wins"| tighten["outcome tightened"]
```

Gates are evaluated in this fixed order. The model's recommendation can only move an outcome toward a stricter one, never looser.

---

## 6. Trust boundaries

```mermaid
flowchart LR
    subgraph untrusted["Untrusted input"]
        docs["Uploaded documents<br/>and OCR text"]
        app["Application form fields"]
        lists["External list data<br/>(OFAC)"]
    end
    subgraph semi["Model boundary"]
        llm["LLM reasoning<br/>(can be influenced)"]
    end
    subgraph trusted["Deterministic controls"]
        pep["Policy enforcement point"]
        gates["Decision gates"]
        audit["Audit chain"]
    end
    subgraph human["Human"]
        analyst["Analyst review"]
    end
    docs --> llm
    app --> llm
    lists -->|"parsed by screening code"| pep
    llm -->|"proposals only"| pep
    pep --> gates --> audit
    gates --> analyst
    llm x--x|"no outbound channel"| out(["Internet / email"])
```

Untrusted text can influence what the model proposes, but every proposal crosses deterministic code before anything happens, and the agent has no outbound channel (the "lethal trifecta" is broken by design). Full analysis: [THREATMODEL.md](THREATMODEL.md).

---

## 7. Deployment pipeline

```mermaid
flowchart LR
    dev["Code change"] --> ci["GitHub Actions CI<br/>lint · 87 tests · golden-case eval gate<br/>alembic check · frontend build · Bicep build"]
    ci --> build["az acr build<br/>pinned base image<br/>build fails if SQL driver libraries are missing"]
    build --> bicep["Bicep deployment<br/>identities · RBAC · Container App · SQL · Storage"]
    bicep --> grants["deploy.sh<br/>cross-resource-group RBAC grants<br/>revoke broad roles<br/>federated credential for OBO"]
    grants --> rev["New Container App revision<br/>startup probe → migrations under lock → ready"]
```
