# Chimera PredMark — API plus self-hosted pmxt sidecar in one image.
#
# pmxt-core is the MIT-licensed server from github.com/pmxt-dev/pmxt, installed
# from npm at a pinned version. It runs on localhost inside this container and
# talks straight to the venue APIs. The hosted pmxt.dev service is not in the
# request path — see venues/pmxt_client.py, which refuses the one pmxt
# exchange ("router") that would call it.

FROM node:20-slim AS pmxt

# Pinned deliberately. pmxt-core's sidecar contract (POST /api/{exchange}/
# {method}, x-pmxt-access-token) was verified against this version; letting it
# float would mean a venue integration changing under a running trading system.
ARG PMXT_VERSION=2.54.0

WORKDIR /opt/pmxt
RUN npm init -y >/dev/null 2>&1 \
    && npm install --omit=dev --no-audit --no-fund "pmxt-core@${PMXT_VERSION}" \
    && npm cache clean --force


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

# Node is needed at runtime for the sidecar; nothing else is.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pmxt /opt/pmxt /opt/pmxt

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
RUN chmod +x /app/entrypoint.sh

# Run unprivileged. Nothing in the container needs to write outside /tmp.
RUN useradd --create-home --uid 10001 predmark \
    && chown -R predmark:predmark /app /opt/pmxt
USER predmark

EXPOSE 8080

# Cloud Run health-checks the service itself; this keeps `docker run` honest.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health',timeout=4).status==200 else 1)"

CMD ["/app/entrypoint.sh"]
