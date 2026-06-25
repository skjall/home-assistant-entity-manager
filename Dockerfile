ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.14-alpine3.20
FROM $BUILD_FROM

# Install runtime dependencies
RUN apk add --no-cache \
    gcc \
    musl-dev \
    python3-dev

# Install Node.js temporarily for building frontend
RUN apk add --no-cache nodejs npm

# Build frontend assets
WORKDIR /build
COPY package.json package-lock.json postcss.config.js ./
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY templates/ ./templates/
# Create static directories
RUN mkdir -p static/js static/fonts
RUN npm ci && \
    npm run build && \
    mkdir -p /app/static && \
    cp -r static/* /app/static/ && \
    cd / && \
    rm -rf /build && \
    apk del nodejs npm

# Install Python dependencies
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Copy application files
# Copy ALL root-level Python modules so a newly added module can never be
# missing from the image (previously each .py was listed individually, which
# silently dropped modules added during refactoring -> ModuleNotFoundError).
COPY *.py /app/
COPY templates/ /app/templates/
COPY translations/ /app/translations/
WORKDIR /app

# Copy run script
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# For modern add-ons with init: false, use CMD to run directly
CMD [ "/run.sh" ]