FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Cloud Run injects PORT; server.py reads it. Never COPY credentials.json here —
# the cloud deployment gets credentials via the GOOGLE_CREDENTIALS_JSON env var.
ENV MCP_TRANSPORT=http

CMD ["python3", "server.py"]
