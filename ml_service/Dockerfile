FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MGTS_PROVIDER=mock MGTS_DATABASE=/data/ml.sqlite3
COPY requirements-dev.lock ./
RUN pip install --no-cache-dir -r requirements-dev.lock
COPY app ./app
COPY knowledge_base ./knowledge_base
COPY mappings ./mappings
COPY prompts ./prompts
COPY tests/fixtures ./tests/fixtures
RUN mkdir -p /data
EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health',timeout=2)"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8001"]
