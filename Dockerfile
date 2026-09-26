FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data
EXPOSE 8000
CMD ["sh", "-c", "python -m fieldwork migrate && if [ \"$FIELDWORK_DEMO\" = 1 ] && [ ! -f /data/.seeded ]; then python -m fieldwork seed && touch /data/.seeded; fi; python -m fieldwork serve --host 0.0.0.0 --port 8000"]
