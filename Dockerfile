# Portable container image for the CTN API.
#
# Build context is the repository root, because the API serves the frontend from
# ../frontend and both directories must be present.
#
#     docker build -t ctn-api .
#
# This file deliberately lives at the root rather than in backend/: Railway's
# service root is backend/, and a Dockerfile there would take precedence over
# its Nixpacks builder and then fail, since Railway would build with backend/ as
# the context and these COPY paths would not resolve.

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
