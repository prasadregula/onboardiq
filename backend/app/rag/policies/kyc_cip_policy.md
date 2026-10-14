# KYC-POL-001 Customer Identification Program (CIP) Policy

## 1. Purpose and scope
This policy implements the Customer Identification Program requirements of 31 CFR 1020.220 for all new deposit, lending and treasury relationships. It applies to natural persons and legal entities, including each beneficial owner and control person identified under KYC-POL-003.

## 2. Required identifying information
Before an account is opened the bank must obtain, at minimum: (a) legal name; (b) date of birth for natural persons, or date of formation for entities; (c) residential or principal business street address — a post office box is not acceptable as the primary address; (d) a taxpayer identification number (SSN/ITIN for US persons, EIN for US entities) or, for non-US persons, a passport number and country of issuance or an equivalent government-issued identification number.

## 3. Acceptable identification documents
For natural persons, an unexpired government-issued photo identification bearing the name and date of birth is required: passport, state driver's license, state identification card, or a national identity card. For legal entities: certified articles of incorporation or organization, a partnership agreement or trust instrument, an EIN confirmation letter, and a government-issued business license where applicable. Copies must be legible; the extraction confidence threshold in KYC-POL-005 applies.

## 4. Verification methods
Identity is verified through documentary methods (Section 3) and, where documents are insufficient or inconsistent, non-documentary methods: comparison with information from a consumer reporting agency, public database, or address verification service; contacting the customer; or checking references with other financial institutions. Non-documentary verification is mandatory when a document is expired, when name or date-of-birth information differs across documents, or when the applicant is not physically present.

## 5. Inability to verify
When identity cannot be verified within 30 days, the account must not be opened, or if opened must be closed, and a Suspicious Activity Report must be considered. A request for additional information (request_more) is the standard first response to a verification gap; escalation to a KYC analyst is required when a discrepancy suggests possible identity fraud, for example a date-of-birth mismatch between two government documents or an identification number that does not match the document presented.

## 6. Straight-through processing
Applications may be approved without analyst review only when every required identifying element has been obtained and verified, no screening alert is open, the customer risk rating under KYC-POL-002 is Low, and no document scored below the confidence threshold. Any other outcome requires either a request for additional information or analyst review.

## 7. Recordkeeping
The bank retains all identifying information, a description of every document relied upon, the methods and results of verification, and the resolution of every discrepancy for five years after the account is closed. The OnboardIQ audit trail is the system of record for these elements.
