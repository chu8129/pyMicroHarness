FROM python:3.14-rc-slim-bookworm

# Install nsjail build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    autoconf \
    bison \
    flex \
    gcc \
    g++ \
    git \
    libprotobuf-dev \
    libnl-route-3-dev \
    libtool \
    make \
    pkg-config \
    protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

# Build nsjail from source
RUN git clone --depth 1 https://github.com/google/nsjail.git /tmp/nsjail \
    && cd /tmp/nsjail \
    && make -j$(nproc) \
    && mv /tmp/nsjail/nsjail /usr/local/bin/nsjail \
    && rm -rf /tmp/nsjail

# Clean up build-only deps to keep image smaller
RUN apt-get update && apt-get install -y --no-install-recommends \
    libprotobuf32 \
    libnl-route-3-200 \
    && apt-get purge -y autoconf bison flex gcc g++ git libtool make pkg-config protobuf-compiler \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /app

ENTRYPOINT ["python", "."]
