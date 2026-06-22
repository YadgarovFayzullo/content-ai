FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
COPY admin-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy admin-api code
COPY admin-api/main.py .
COPY admin-api/config.py .

# Reuse the shared DB models/access layer from the bot project
COPY database.py .

# Shared subscription-tier limits (restrict mode) — single source of truth used
# by both admin-api and the bot.
COPY tiers.py .

# Generation pipeline (text-only preview for the admin panel). Image generation
# (google-genai) is imported lazily and not used here, so it isn't installed.
COPY context_builder.py .
COPY rag.py .
COPY generator.py .
COPY orchestrator.py .
# Stock-photo fetch (Pexels). orchestrator imports it at module load, so it must
# be present even for the text-only preview path (deps: httpx + PEXELS_API_KEY).
COPY image_search.py .

# Expose port
EXPOSE 8001

# Run admin API
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
