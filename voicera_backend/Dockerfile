FROM python:3.11-slim

WORKDIR /app

# System deps: build tools + OpenMP for onnxruntime (pulled in by chromadb)
# + ffmpeg for converting inbound voice-note audio (WhatsApp/OGG/whatever format
# arrives) to the raw 16kHz mono s16le PCM ai4bharat_stt_server's /transcribe
# expects — see sajag_pipeline.convert_to_pcm16_mono_16k(). Without this, real
# voice notes silently produce empty transcriptions rather than an error.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libgomp1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

