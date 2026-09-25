# Kalshi Trading System Dockerfile
# Multi-stage build for optimized production image

# ============================================
# Stage 1: Builder
# ============================================
FROM python:3.10-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install poetry
RUN pip install --no-cache-dir poetry==1.7.1

# Copy dependency files
COPY pyproject.toml poetry.lock ./

# Configure poetry to not create virtual env (we're in container)
RUN poetry config virtualenvs.create false

# Install dependencies (no dev dependencies for production)
RUN poetry install --no-dev --no-interaction --no-ansi

# ============================================
# Stage 2: Production
# ============================================
FROM python:3.10-slim as production

WORKDIR /app

# Create non-root user for security
RUN groupadd -r kalshi && useradd -r -g kalshi kalshi

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=kalshi:kalshi . .

# Create directories for data
RUN mkdir -p /app/data /app/logs && chown -R kalshi:kalshi /app

# Switch to non-root user
USER kalshi

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KALSHI_DATA_DIR=/app/data \
    KALSHI_LOG_DIR=/app/logs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Expose port for API (if needed)
EXPOSE 8080

# Default command: run trading system in paper mode
CMD ["python", "-m", "src.main", "--paper"]

# ============================================
# Stage 3: Development
# ============================================
FROM python:3.10-slim as development

WORKDIR /app

# Install build dependencies and dev tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install poetry
RUN pip install --no-cache-dir poetry==1.7.1

# Copy dependency files
COPY pyproject.toml poetry.lock ./

# Configure poetry
RUN poetry config virtualenvs.create false

# Install all dependencies including dev
RUN poetry install --no-interaction --no-ansi

# Copy application code
COPY . .

# Create data directories
RUN mkdir -p /app/data /app/logs

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KALSHI_DATA_DIR=/app/data \
    KALSHI_LOG_DIR=/app/logs \
    KALSHI_ENV=development

# Default command for development
CMD ["python", "-m", "pytest", "tests/", "-v"]
