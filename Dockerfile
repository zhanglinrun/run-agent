FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 1000 runagent \
    && mkdir -p /workspace /home/runagent/.run

COPY . .

RUN python -m pip install --no-cache-dir ".[feishu]" \
    && chown -R runagent:runagent /workspace /home/runagent

USER runagent

VOLUME ["/home/runagent/.run"]

ENTRYPOINT ["run-agent-gateway"]
CMD ["--extension", "/app/examples/gateway_extensions/feishu.py", "--cwd", "/workspace"]
