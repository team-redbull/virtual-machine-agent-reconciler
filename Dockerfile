FROM python:3.12-slim
WORKDIR /app
# Installed console scripts (kopf lives in /usr/local/bin) do NOT put the working
# directory on sys.path, so `import agent_reconciler` fails without this.
ENV PYTHONPATH=/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent_reconciler ./agent_reconciler
# Metrics on :8080, liveness on :8081
ENTRYPOINT ["kopf", "run", "-m", "agent_reconciler.operator", \
            "--all-namespaces", "--liveness=http://0.0.0.0:8081/healthz"]
