# Build context: repository root (see compose.yaml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY server/pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install . && rm -rf app build *.egg-info

COPY server/ ./
RUN pip install --no-deps .

# Test image only: the runner tests need the client library.
ARG WITH_TESTS=0
COPY clients/core /opt/clients-core
RUN if [ "$WITH_TESTS" = "1" ]; then pip install ".[test]" /opt/clients-core; fi

RUN useradd --create-home --uid 1000 multichat && mkdir -p /data && chown multichat /data
USER multichat
VOLUME ["/data"]
EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
