FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.11-slim

# System dependencies: WeasyPrint + tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    libcairo2 \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# kubectl
RUN curl -Lo /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# stern (multi-pod log tailing)
RUN curl -Lo /usr/local/bin/stern \
    "https://github.com/stern/stern/releases/latest/download/stern_linux_amd64" \
    && chmod +x /usr/local/bin/stern

# AWS CLI v2
RUN curl -Ls "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
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

# Install the package itself (editable-like, no deps)
RUN pip install --no-cache-dir --no-deps -e .

# Data directory for SQLite PVC
RUN mkdir -p /data

EXPOSE 5050

CMD ["vk", "serve"]
