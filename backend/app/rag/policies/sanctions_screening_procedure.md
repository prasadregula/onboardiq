# KYC-POL-004 Sanctions, PEP and Adverse Media Screening Procedure

## 1. Who is screened
Every applicant, every beneficial owner, every control person, every authorised signer, and the legal entity itself are screened at onboarding against the OFAC Specially Designated Nationals list and consolidated non-SDN lists, the EU consolidated list, the UN Security Council consolidated list, a politically exposed persons database, and adverse media.

## 2. Matching method
Names are normalised (diacritics removed, honorifics and corporate suffixes stripped) and compared using a token-based similarity score between 0 and 1. Date of birth agreement raises the score; a conflicting date of birth lowers it. Nationality or country of residence agreement raises the score slightly.

## 3. Match thresholds
A score of 0.90 or higher is a confirmed match and must be escalated to the BSA/AML team; the account may not be opened while the alert is open. A score between 0.75 and 0.89 is a possible match that must be reviewed and dispositioned by a KYC analyst before the account is opened. A score below 0.75 is not a match and is recorded for audit purposes only.

## 4. Confirmed sanctions matches
No automated approval, request for information, or communication with the customer is permitted while a confirmed sanctions match is open. The case is escalated immediately to the BSA Officer, who determines whether the property must be blocked and whether OFAC must be notified within 10 business days.

## 5. Politically exposed persons
A match against the PEP database for the customer, a beneficial owner or a control person requires Enhanced Due Diligence under KYC-POL-002 Section 4 and senior management approval before account opening. PEP status alone is not grounds for refusal; it is grounds for escalation.

## 6. Adverse media
Adverse media alerts related to fraud, money laundering, terrorist financing, corruption or sanctions evasion add 25 points to the risk score and require analyst review. Adverse media unrelated to financial crime is recorded but does not affect the rating.

## 7. Alert disposition and recordkeeping
Every alert disposition (true positive, false positive, insufficient information) records the analyst, the date, the evidence relied upon and the reasoning. Dispositions are retained for five years and are used to tune matching thresholds and to build regression cases for the screening engine.
