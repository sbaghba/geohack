# SiteSense backend image (build from the repo root: needs both backend/ and contract/)
#   docker build -t sitesense-api .
#   docker run -p 8000:8000 -e GEMINI_API_KEY=... sitesense-api
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /srv

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY contract contract
COPY backend backend

WORKDIR /srv/backend
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
