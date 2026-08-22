# Portable container image for the CTN API.
#
# Railway is pinned to its Nixpacks builder via railway.json, so this file does
# not affect that deploy. It is here for Docker-based targets (Render, Fly.io,
# Cloud Run, or `docker run` locally) and to make the build reproducible.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install -r backend/requirements.txt

# The API serves the frontend from ../frontend, so both directories are copied.
COPY backend ./backend
COPY frontend ./frontend

WORKDIR /app/backend

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 ctn && chown -R ctn /app
USER ctn

EXPOSE 8000

# Hosts inject the port; default to 8000 when run directly.
ENV PORT=8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
