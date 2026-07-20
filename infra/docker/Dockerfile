# Multi-stage Dockerfile for the Automata platform.
# Build stage: install dependencies, build wheels.
# Runtime stage: minimal, distroless-ish, non-root.

ARG PYTHON_VERSION=3.12
ARG POETRY_VERSION=1.8.3

# ---- build ----
FROM python:${PYTHON_VERSION}-slim AS build
ARG POETRY_VERSION
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /src
RUN pip install --upgrade pip
COPY requirements.txt /src/
RUN pip wheel --wheel-dir=/wheels -r requirements.txt

# ---- runtime ----
FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AUTOMATA_SERVICE=automata-runtime \
    PATH=/app/bin:$PATH

# Non-root user
RUN groupadd --system automata && useradd --system --gid automata --uid 10001 automata

WORKDIR /app
COPY --from=build /wheels /wheels
COPY requirements.txt /app/
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels
COPY . /app

USER automata:automata
EXPOSE 8080 50051

# Default to the control plane; workers override the command.
CMD ["python", "-m", "services.control_plane.api"]
