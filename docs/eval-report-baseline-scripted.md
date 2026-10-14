# OnboardIQ eval — scripted mode — 2026-09-16T19:24:20Z

**12/12 cases passed · outcome accuracy 100% · avg latency 33 ms · tokens 0 · model n/a · prompt kyc-orchestrator/v1**

| Case | Expected | Actual | Gate | Risk | Result | Failed checks |
|---|---|---|---|---|---|---|
| G01_individual_clean_auto_approve | auto_approve | auto_approve | GATE_STP | low (0) | PASS | — |
| G02_individual_address_mismatch_request_more | request_more | request_more | GATE_MISSING_INFO | low (5) | PASS | — |
| G03_individual_low_confidence_request_more | request_more | request_more | GATE_MISSING_INFO | low (5) | PASS | — |
| G04_individual_sanctions_match_escalate | escalate | escalate | GATE_SANCTIONS | high (100) | PASS | — |
| G05_individual_pep_escalate | escalate | escalate | GATE_PEP_EDD | high (60) | PASS | — |
| G06_individual_possible_match_needs_review | needs_review | needs_review | GATE_POSSIBLE_MATCH | medium (30) | PASS | — |
| G07_individual_dob_conflict_escalate | escalate | escalate | GATE_CRITICAL_DATA | low (0) | PASS | — |
| G08_business_ubo_gap_request_more | request_more | request_more | GATE_MISSING_INFO | low (0) | PASS | — |
| G09_business_clean_low_risk_auto_approve | auto_approve | auto_approve | GATE_STP | low (0) | PASS | — |
| G10_business_high_risk_layered_escalate | escalate | escalate | GATE_HIGH_RISK | high (70) | PASS | — |
| G11_business_over_100_pct_escalate | escalate | escalate | GATE_CRITICAL_DATA_WITH_SCREENING | low (25) | PASS | — |
| G12_individual_cash_intensive_medium_needs_review | needs_review | needs_review | GATE_MEDIUM_RISK | medium (35) | PASS | — |

## Outcome confusion

| expected | actual | count |
|---|---|---|
| auto_approve | auto_approve | 2 |
| escalate | escalate | 5 |
| needs_review | needs_review | 2 |
| request_more | request_more | 3 |
