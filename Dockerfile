FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY catcam.py config.example.ini ./

# config.ini is mounted at /app/config.ini; recordings and clips go to /app/data
VOLUME /app/data
EXPOSE 8080
ENV PYTHONUNBUFFERED=1
CMD ["python", "catcam.py"]
