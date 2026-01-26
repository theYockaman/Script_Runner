FROM python:3.11-slim
WORKDIR /app

# system deps for building common wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ./app /app/app
COPY ./scripts /app/scripts
RUN mkdir -p /app/data

EXPOSE 8080
ENV SCRIPTS_DIR=/app/scripts
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
