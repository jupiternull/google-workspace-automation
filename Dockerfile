FROM python:3.13-slim

RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /bin/false -u 10001 appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watcher.py parse_tickets.py ./

RUN mkdir /logs && chown appuser:appuser /logs

USER appuser

CMD ["python", "-u", "watcher.py"]
