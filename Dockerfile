FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

COPY . .

RUN uv sync

CMD ["tail", "-f", "/dev/null"]