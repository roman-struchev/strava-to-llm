FROM python:3.12-slim

# Unbuffered stdout so `docker logs` shows request/rate-limit lines as they happen.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY strava_export.py coros_mcp_export.py server.py ./

EXPOSE 8000
CMD ["python", "server.py"]
