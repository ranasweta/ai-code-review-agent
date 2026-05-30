# ---------------------------------------------------------------------------
# Dockerfile (Day 7) — for Railway / Render / Fly.io / any container host.
# Streamlit Cloud does NOT use this (it builds from requirements.txt +
# packages.txt); this is the portable alternative.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# Don't write .pyc files; flush stdout/stderr immediately so logs stream live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (separate layer) so code changes don't bust the
# pip cache layer on every rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the application source.
COPY . .

# Streamlit's default port.
EXPOSE 8501

# Healthcheck hits Streamlit's built-in endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8501/_stcore/health').status==200 else 1)"

# Bind to 0.0.0.0 so the container is reachable; headless for a server.
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true"]
