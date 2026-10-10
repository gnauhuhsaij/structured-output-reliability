FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/sor
COPY . .
RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 sor \
    && mkdir -p /workspace \
    && chown sor:sor /workspace

USER sor
WORKDIR /workspace

ENTRYPOINT ["sor"]
CMD ["--help"]
