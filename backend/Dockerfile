FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Development images also get pytest/ruff (docker-compose.yml sets INSTALL_DEV=true); production images don't.
ARG INSTALL_DEV=false
COPY requirements.txt requirements-dev.txt ./
# Packages come over verified HTTPS. On a build machine whose network inspects TLS (an antivirus web shield, a
# corporate proxy), pass the root CA that machine already trusts as the BuildKit secret `extra_ca` (README ›
# "Building behind TLS inspection"): it is added to pip's bundle for this step only, never written to a layer, and
# verification stays on. Without the secret the build is unchanged.
RUN --mount=type=secret,id=extra_ca \
    set -e; cert=""; \
    if [ -s /run/secrets/extra_ca ]; then \
        cat "$(python -c 'import pip._vendor.certifi as c; print(c.where())')" /run/secrets/extra_ca > /tmp/ca.pem; \
        cert="--cert /tmp/ca.pem"; \
    fi; \
    pip install --no-cache-dir $cert -r requirements.txt; \
    if [ "$INSTALL_DEV" = "true" ]; then pip install --no-cache-dir $cert -r requirements-dev.txt; fi; \
    rm -f /tmp/ca.pem
COPY . .
# Tests, evals and the demo seed (demo accounts with a known password) are for development and CI images only.
RUN if [ "$INSTALL_DEV" != "true" ]; then rm -rf tests evals seed; fi \
    && useradd -m appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
# Migrations run on start by default (Docker Compose: one instance). Platforms with a separate migration step
# (Render's pre-deploy command: `alembic upgrade head`) set RUN_MIGRATIONS_ON_START=false. The port comes from
# $PORT when the platform sets it (Render), else 8000. Behind the production reverse proxy FORWARDED_ALLOW_IPS='*'
# lets uvicorn take the scheme (https) from that proxy; the backend is not reachable except through it. The client
# address for rate limits comes from TRUSTED_PROXY_HOPS (app/api/deps.py), never from a client-supplied header.
CMD ["sh", "-c", "if [ \"${RUN_MIGRATIONS_ON_START:-true}\" = true ]; then alembic upgrade head || exit 1; fi; exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\" --no-server-header"]
