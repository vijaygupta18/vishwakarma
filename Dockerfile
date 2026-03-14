FROM --platform=linux/amd64 python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM --platform=linux/amd64 python:3.11-slim

ENV PYTHONUNBUFFERED=1

# System dependencies: WeasyPrint (PDF) + tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libgdk-pixbuf-2.0-0 \
    libffi8 \
    libcairo2 \
    shared-mime-info \
    fonts-liberation \
    curl \
    unzip \
    jq \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETPLATFORM

# kubectl (official k8s binary)
RUN ARCH=$([ "$TARGETPLATFORM" = "linux/arm64" ] && echo "arm64" || echo "amd64") && \
    curl -fsSLo /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/${ARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# stern (multi-pod log tailing) — pinned version
ARG STERN_VERSION=1.32.0
RUN ARCH=$([ "$TARGETPLATFORM" = "linux/arm64" ] && echo "arm64" || echo "amd64") && \
    curl -fsSL "https://github.com/stern/stern/releases/download/v${STERN_VERSION}/stern_${STERN_VERSION}_linux_${ARCH}.tar.gz" \
    -o /tmp/stern.tar.gz \
    && tar -xzf /tmp/stern.tar.gz -C /usr/local/bin stern \
    && rm /tmp/stern.tar.gz \
    && chmod +x /usr/local/bin/stern

# AWS CLI v2 (arch-aware)
RUN ARCH=$([ "$TARGETPLATFORM" = "linux/arm64" ] && echo "aarch64" || echo "x86_64") && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Copy application
COPY vishwakarma/ ./vishwakarma/
COPY pyproject.toml .

# Install the package itself (no deps — already installed above)
RUN pip install --no-cache-dir --no-deps .

# Data directory for SQLite PVC
RUN mkdir -p /data

EXPOSE 5050

CMD ["vk", "serve"]
