# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# The optional secret supports HTTPS package installation in a managed build
# environment. The CA is not copied into a layer or the resulting image.
COPY requirements.lock ./requirements.lock
RUN --mount=type=secret,id=proxy_ca,required=false \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir -r requirements.lock

RUN groupadd --gid 10001 omnicrm \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin omnicrm
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 web ./web
COPY --chown=10001:10001 scripts ./scripts
USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=3)"
CMD ["python", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
