FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir ".[postgres]" && mkdir -p /data
EXPOSE 8000
CMD ["sh", "-c", "fieldwork migrate && if [ \"$FIELDWORK_DEMO\" = 1 ]; then fieldwork seed --if-empty; fi; fieldwork serve --host 0.0.0.0 --port ${PORT:-8000}"]
