FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt constraints.txt ./
ENV PIP_CONSTRAINT=/app/constraints.txt
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -U pip setuptools wheel \
 && pip install --no-cache-dir --no-build-isolation protobuf3-to-dict \
 && pip install --no-cache-dir -r requirements.txt
COPY tracker.py .
RUN useradd -r -u 10001 app && mkdir /recordings && chown app /recordings
USER app
CMD ["python", "tracker.py"]
