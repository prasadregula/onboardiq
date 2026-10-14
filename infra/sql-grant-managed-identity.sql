-- Run ONCE against the onboardiq database as the Entra SQL admin (Azure Data Studio / sqlcmd -G).
-- Lets the Container App managed identity connect with Authentication=ActiveDirectoryMsi.
-- Replace ca-onboardiq-dev-api with the API's Container App name (deploy.sh prints it).
CREATE USER [ca-onboardiq-dev-api] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [ca-onboardiq-dev-api];
ALTER ROLE db_datawriter ADD MEMBER [ca-onboardiq-dev-api];
ALTER ROLE db_ddladmin  ADD MEMBER [ca-onboardiq-dev-api];   -- Alembic migrations at startup; drop after first deploy if preferred
