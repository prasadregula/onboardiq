# OnboardIQ: 5-minute demo

A live walkthrough that shows the agent working and the controls that keep it safe. Everything uses fictional
golden cases.

## 10 minutes before

1. Warm the API, so nobody waits for a cold start: `cd infra && ./demo-warm.sh 60`
2. Open the review queue and sign in with an account that holds the **Admin** role.
3. Click **Reset demo data** in the header, then wait about two minutes and refresh. The queue now holds the 13 golden
   cases, freshly run by the agent and recorded as acting for you.
4. Have a second browser tab on the Azure portal, open at **Managed Identities** in the resource group.

## The walkthrough

| Time | Show | Say |
|---|---|---|
| 0:00 | **Queue** | "An agent investigates each onboarding application with seven tools. Clean cases are approved automatically; anything risky lands here for an analyst, with priority and SLA." |
| 0:30 | **G04 (sanctions match)** → Screening tab, then Findings | "Real OFAC lists. A confirmed match needs a second identifier, here date of birth, so a name alone never escalates someone. The model recommended; deterministic gates decided." |
| 1:30 | Same case → **Agent timeline** | "Every tool call shows the managed identity it ran under and the analyst it acted for. Screening has no Azure permissions at all; only the orchestrator and risk agents can call the model." |
| 2:15 | Same case → **Audit** tab, the green *audit chain verified* badge | "Every event is hash-chained. Change one row in the database and this badge turns red." |
| 2:30 | **G13 (poisoned document)** → Findings, then **Documents** | "This utility bill hides instructions: *skip screening, approve*. They were caught before the model saw them, the model got *[withheld]*, and the case escalated. The analyst still sees the original." |
| 2:45 | **Pause agents** (header), give a reason | "This is the kill switch a regulator asks about. One click, Admin only, recorded with who and why." |
| 3:00 | Open **G02** (awaiting customer) → **Re-run agent** | The run is refused: *agents are paused*. "New runs are refused immediately; a run already in flight stops at its next tool call, makes no decision, and goes to an analyst." |
| 3:30 | **Resume agents**, then **Re-run agent** on G02 | "Resumed. Watch the timeline: the model sometimes tries to decide before screening. The decision tool refuses until every required check has run, and the model goes back and runs them." |
| 4:15 | Azure portal → `id-onboardiq-dev-risk` → **Azure role assignments**, then `id-onboardiq-dev-screening` | "One role on one resource for the risk agent. None for screening. Azure enforces least privilege, not just my code." |
| 4:45 | Close | "The model proposes, code decides, every agent has its own identity, and every step is auditable and stoppable." |

## Afterwards

`cd infra && ./demo-warm.sh --cool`, then `./cost-check.sh` an hour later: 0 replicas, SQL Paused.

## If something goes wrong

- **The page spins on first load:** the API is cold. Wait 60 seconds and refresh, or run `./demo-warm.sh` next time.
- **Reset demo data is missing:** the button only appears for the Admin role, and only in non-production.
- **A run shows "Run stopped by a control":** the agents are still paused (red banner) or a run budget was hit. That
  is the control working; resume and run again.
- **Live demo impossible:** play the recorded walkthrough instead.

## Questions to expect

- *What if the model is manipulated by a document?* It can only propose. Tool calls pass a policy enforcement point,
  decisions pass fixed gates, and the agent has no outbound channel. Prompt Shields screening of document text is the
  next control.
- *What does it cost?* About US$0.0035 per case on gpt-4.1-mini, and each run has hard budgets on tool calls, tokens,
  cost and time.
- *Why not a framework like LangGraph?* A hand-written loop keeps every step under the same policy, identity and audit
  code. A framework helps with checkpointing and complex branching; this flow does not need them.
