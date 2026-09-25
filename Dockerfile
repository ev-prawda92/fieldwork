FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV FIELDWORK_DB=/data/fieldwork.db
RUN mkdir -p /data
EXPOSE 8000
CMD ["sh", "-c", "[ -f $FIELDWORK_DB ] || python -m fieldwork seed; python -m fieldwork serve --host 0.0.0.0 --port 8000"]
