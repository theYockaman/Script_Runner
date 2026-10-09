# Node runtime donor: same Debian base as the final image, so glibc/libstdc++ match.
FROM node:24-bookworm-slim AS nodejs

# Pin bookworm explicitly: plain python:3.11-slim now resolves to Debian 13 (trixie),
# which would change the Microsoft apt repo URL and Debian package versions below.
FROM python:3.11-slim-bookworm
WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    POWERSHELL_TELEMETRY_OPTOUT=1 \
    DOTNET_CLI_TELEMETRY_OPTOUT=1

# Build deps (gcc/libpq for Python wheels) + script runtimes from Debian:
#   ruby 3.1, php 8.2 CLI, perl 5.36 (full core modules; perl-base is already present).
# PowerShell 7 comes from Microsoft's Debian repo (amd64 + arm64 are published).
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libpq-dev curl ca-certificates gnupg \
      ruby php-cli perl \
  && . /etc/os-release \
  && curl -fsSL -o /tmp/pmc.deb "https://packages.microsoft.com/config/debian/${VERSION_ID}/packages-microsoft-prod.deb" \
  && dpkg -i /tmp/pmc.deb && rm /tmp/pmc.deb \
  && apt-get update && apt-get install -y --no-install-recommends powershell \
  && rm -rf /var/lib/apt/lists/*

# Node 24 + npm copied from the official image (no curl | bash), then tsx for TypeScript.
COPY --from=nodejs /usr/local/bin/node /usr/local/bin/node
COPY --from=nodejs /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g tsx \
 && npm cache clean --force \
 && node --version && npm --version && tsx --version \
 && pwsh -NoProfile -NonInteractive -Command '$PSVersionTable.PSVersion.ToString()' \
 && ruby --version && php --version && perl --version | head -2

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ./app /app/app
COPY ./scripts /app/scripts
RUN mkdir -p /app/data

EXPOSE 8080
ENV SCRIPTS_DIR=/app/scripts
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
