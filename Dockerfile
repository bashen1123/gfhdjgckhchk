FROM python:3.12-slim

WORKDIR /app
COPY bot.py /app/bot.py

ENV BOT_DB=/app/data/bot.db
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
