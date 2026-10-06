FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Development images also get pytest/ruff (docker-compose.yml sets INSTALL_DEV=true); production images don't.
ARG INSTALL_DEV=false
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DEV" = "true" ]; then pip install --no-cache-dir -r requirements-dev.txt; fi
COPY . .
RUN useradd -m appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
# Migrations run on start by default (Docker Compose: one instance). Platforms with a separate migration step
# (Render's pre-deploy command: `alembic upgrade head`) set RUN_MIGRATIONS_ON_START=false. The port comes from
# $PORT when the platform sets it (Render), else 8000. Behind the production reverse proxy FORWARDED_ALLOW_IPS='*'
# makes the real client IP visible (rate limits); the backend is not reachable except through that proxy.
CMD ["sh", "-c", "if [ \"${RUN_MIGRATIONS_ON_START:-true}\" = true ]; then alembic upgrade head || exit 1; fi; exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
