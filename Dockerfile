FROM python:3.11-slim AS runtime

ENV APP_HOST=127.0.0.1 \
    ALLOW_AZURE_USE=false \
    PYTHONUNBUFFERED=1
WORKDIR /build
COPY pyproject.toml LICENSE ./
COPY app ./app
COPY scripts ./scripts
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple \
        --upgrade 'setuptools>=84.1.0' \
    && python -m pip install --no-cache-dir --index-url https://pypi.org/simple . \
    && rm -rf /build
WORKDIR /app
RUN useradd --uid 10001 --create-home appuser \
    && chown appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["incident-memory"]
