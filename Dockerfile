FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent_reconciler ./agent_reconciler
# Metrics on :8080, liveness on :8081
ENTRYPOINT ["kopf", "run", "-m", "agent_reconciler.operator", \
            "--all-namespaces", "--liveness=http://0.0.0.0:8081/healthz"]
