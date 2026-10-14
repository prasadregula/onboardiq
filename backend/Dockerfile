# Pin the Debian release to match the Microsoft repo below (python:3.12-slim moved to Debian 13 "trixie").
FROM python:3.12-slim-bookworm
# msodbcsql18 for Azure SQL (pyodbc). libgssapi-krb5-2 is installed explicitly: the driver dlopen()s it but the package
# does not declare it, so "apt-get autoremove" used to delete it -> "Can't open lib libmsodbcsql-18...: file not found".
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg2 ca-certificates unixodbc libgssapi-krb5-2 \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
 && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get purge -y curl gnupg2 && rm -rf /var/lib/apt/lists/*
# Fail the BUILD (not the running container) if the ODBC driver is missing any shared library.
RUN ! ldd /opt/microsoft/msodbcsql18/lib64/libmsodbcsql-18.*.so.* | grep "not found" \
 && odbcinst -q -d -n "ODBC Driver 18 for SQL Server"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -r -u 10001 onboardiq && mkdir -p /app/data && chown -R onboardiq /app/data
USER onboardiq
ENV ONBOARDIQ_LOG_JSON=true ONBOARDIQ_ENV=prod
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://localhost:8080/health').status_code==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
