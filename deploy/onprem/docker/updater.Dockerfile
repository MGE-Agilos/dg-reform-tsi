# Monthly data update of the self-hosted TSI x CSR Explorer (deploy/onprem/README.md, step 11), for Docker or Podman.
# Build from the root of the repository:
#   podman build -f deploy/onprem/docker/updater.Dockerfile -t tsi-explorer-updater .
# No secret in the image: the key and the database URL come from updater.env (env_file of compose.updater.yaml).
FROM python:3.12-slim

# util-linux: flock (one update at a time); ca-certificates: HTTPS to europa.eu and GPT@EC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config/sources.yaml config/sources.yaml
# GPT@EC settings (llm_provider: openai); mount another file on /app/config/settings.yaml to change them.
COPY deploy/updater/settings.updater.yaml config/settings.yaml
COPY deploy/onprem/update.sh deploy/onprem/update.sh
COPY .github/scripts/ci_summary.py .github/scripts/ci_summary.py

# Unprivileged user; output/, .cache/ and logs/ are volumes (the state of the update survives the container).
RUN useradd --uid 1000 --create-home tsi \
    && mkdir -p output .cache logs \
    && chown -R tsi /app/output /app/.cache /app/logs
USER tsi
ENV TSI_PYTHON=python PYTHONUNBUFFERED=1
VOLUME ["/app/output", "/app/.cache", "/app/logs"]

ENTRYPOINT ["bash", "deploy/onprem/update.sh"]
