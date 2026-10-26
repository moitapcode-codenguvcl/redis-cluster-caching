FROM python:3.12-slim

WORKDIR /app

# procps provides pkill/kill for process management inside the container
RUN apt-get update && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Default command: keep container alive so we can exec into it
CMD ["sleep", "infinity"]
