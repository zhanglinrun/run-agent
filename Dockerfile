FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 1000 runagent \
    && mkdir -p /workspace /home/runagent/.run \
    && python -c "import uuid; open('/etc/machine-id','w',encoding='ascii').write(uuid.uuid4().hex)"

COPY . .

RUN python -m pip install --no-cache-dir ".[feishu]" \
    && chown -R runagent:runagent /workspace /home/runagent

USER runagent

VOLUME ["/home/runagent/.run"]

ENTRYPOINT ["run", "gateway"]
CMD ["--state-dir", "/home/runagent/.run", "--cwd", "/workspace"]
