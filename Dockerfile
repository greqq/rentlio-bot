FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
# Diagnostics and one-off analyses are run inside the running container
# (docker compose exec rentlio-bot python scripts/...), so they have to ship
# with the image - this deployment pulls an image, it does not check out a repo.
COPY scripts/ ./scripts/

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "src.bot"]

