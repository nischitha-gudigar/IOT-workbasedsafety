FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer cache efficiency)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Expose demo API port
EXPOSE 8000

# No CMD — persona and scenario are always passed by the POC project's
# docker-compose command: block, keeping the image generic.
ENTRYPOINT ["python", "run.py"]
