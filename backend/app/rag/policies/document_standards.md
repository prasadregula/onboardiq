# KYC-POL-005 Document Standards and Automated Extraction

## 1. Required documents by customer type
Natural person: one unexpired government-issued photo identification document and one proof of address. Legal entity: formation document (articles of incorporation or organization, partnership agreement or trust instrument), EIN confirmation letter, a beneficial ownership declaration, and one identification document for each beneficial owner and the control person.

## 2. Expiry
Identification documents must be unexpired on the date of application. An expired document is not acceptable and must be replaced; the applicant is asked for a current document. Proof of address must be dated within 90 days of the application.

## 3. Extraction confidence
Automated extraction (Azure Document Intelligence) reports a confidence score per document. Documents scoring below 0.80 are treated as unreadable for automated processing: the customer is asked for a clearer copy. Documents scoring 0.80 or above may be relied upon without manual transcription. A document from which no fields could be extracted at all is treated as unreadable.

## 4. Acceptable proof of address
Utility bill, bank or brokerage statement, lease agreement, property tax bill, or government correspondence showing the applicant's name and residential street address. Post office boxes, commercial mail-receiving agencies and registered-agent addresses are not acceptable as the residential or principal business address.

## 5. Cross-document consistency
The name, date of birth, identification number and address extracted from each document are compared with the declared application data and with each other. Minor name variations (middle initial, ordering, diacritics, common nicknames) are tolerated. A date-of-birth conflict between two government-issued documents, or an identification number that does not match the declared number, indicates possible identity fraud and requires analyst escalation rather than a customer request. An address difference between the identification document and the proof of address is common (people move) and is resolved with the customer.

## 6. Retention
Images of documents relied upon are retained with the case for five years after the relationship ends, together with the extraction result and confidence score at the time of decision.
