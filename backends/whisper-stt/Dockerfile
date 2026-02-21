FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends --allow-change-held-packages \
        python3 python3-pip ffmpeg libcublas-12-8 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

RUN groupadd -g 10000 stt && \
    useradd -u 10000 -g stt -s /bin/bash -m stt && \
    mkdir -p /cache/huggingface && \
    chown -R stt:stt /cache/huggingface /app

USER stt

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
