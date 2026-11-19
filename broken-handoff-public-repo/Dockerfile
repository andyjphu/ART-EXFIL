FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir openpyxl==3.1.5

ENV PYTHONDONTWRITEBYTECODE=1
ENV HOME=/tmp
WORKDIR /workspace
CMD ["sleep", "infinity"]
