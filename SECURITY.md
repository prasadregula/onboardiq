# Security policy

OnboardIQ is a portfolio project that demonstrates security controls for agentic AI in a regulated setting. It is not a production service and holds no real customer data.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's **private vulnerability reporting** on this repository (Security tab → Report a vulnerability). Include what you found, how to reproduce it, and the impact you expect. I aim to acknowledge reports within 7 days.

## Data

- All applicants, documents and watchlist entries in this repository are **fictional**.
- The deployed environment processes fictional golden cases only. Real personal data is out of scope until PII minimisation before model calls (threat T-07) is complete.
- Real OFAC sanctions lists are public data and are used only to screen fictional names.

## Secrets and configuration

- No secrets are stored in this repository or in application configuration. Azure services are reached with managed identities; Azure SQL uses Entra access tokens; on-behalf-of uses a managed-identity federated credential instead of a client secret.
- Environment-specific values (tenant, resource names, endpoints) live in gitignored files: `infra/.deploy.env`, `infra/params.dev.json`, `backend/.env`, `frontend/.env.production`.
- The development API-key mode is refused at startup outside dev and CI environments.

## Controls in the code

| Area | Control |
|---|---|
| Authentication | Microsoft Entra ID; JWT validated against tenant keys, audience and issuer; app roles for authorization |
| Agent identity | One managed identity per agent role with least-privilege Azure RBAC |
| Tool calls | Policy enforcement point: deny-by-default allow-list, argument validation, case-scoped ids |
| Decisions | Deterministic policy gates; the model can only tighten an outcome |
| Audit | Hash-chained event log with per-case verification; every event records the agent identity and the human principal |
| Supply chain | Base image pinned to an OS release; native-library check fails the build; CI runs lint, tests, the eval gate and a Bicep build |

The threat model, including accepted residual risks, is in [docs/THREATMODEL.md](docs/THREATMODEL.md).
