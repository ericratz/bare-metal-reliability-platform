FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY run.py .

#baked in at build time so /health and brp_app_info can report which image is
#running — this is what confirms a rolling update actually landed on a node.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

RUN useradd -m -u 10001 appuser
USER appuser

EXPOSE 8000

#python, not curl: the slim base has no curl and adding one just for a
#healthcheck is a bigger image and one more package to patch on two distros.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"

CMD ["uvicorn", "app.core.api:app", "--host", "0.0.0.0", "--port", "8000"]
