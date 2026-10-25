FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_MODE=production PORT=8080
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser
COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY --from=frontend /build/dist ./frontend/dist/
USER appuser
EXPOSE 8080
CMD ["sh", "-c", "exec python -m uvicorn backend.app.main:create_app --factory --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log"]
