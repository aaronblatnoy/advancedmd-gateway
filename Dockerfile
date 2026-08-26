# advancedmd-gateway, SPEC 21.
#
# One service, ONE replica. Horizontal scaling is forbidden (SPEC 4.7):
# two instances would be two rate clocks and AdvancedMD bills per excess
# call. Do not add replicas, do not add a load balancer.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app:/app/domains

WORKDIR /app

# lxml wheels cover slim; build tools are only a fallback for odd arches.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md* ./
COPY gateway/ ./gateway/
COPY domains/ ./domains/
COPY knowledge/ ./knowledge/
COPY schemas/ ./schemas/
COPY policy.schema.json action-catalog.data.json domain-mapping.data.json audit-v2.schema.json ./

RUN pip install --no-cache-dir .

# /data holds clock.json (SPEC 7.5) and the token table (SPEC 10.1).
VOLUME ["/data"]

EXPOSE 8820

# SPEC 21: healthy when status is ok OR degraded; unhealthy only when the
# process is down. degraded still serves.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8820/health \
      | grep -Eq '"status"[[:space:]]*:[[:space:]]*"(ok|degraded|starting)"'

CMD ["uvicorn", "--factory", "gateway.app:build_app", "--host", "0.0.0.0", "--port", "8820"]
