ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.14-alpine3.20

# Build the frontend on the native build platform. Running npm/node under QEMU
# emulation (e.g. for aarch64 on an amd64 runner) crashes with SIGILL (exit 132),
# so we build it once natively and copy the arch-independent assets into the image.
FROM --platform=$BUILDPLATFORM node:20-alpine AS frontend
WORKDIR /build
COPY package.json package-lock.json postcss.config.js ./
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY templates/ ./templates/
RUN mkdir -p static/js static/fonts && npm ci && npm run build

FROM $BUILD_FROM

# Runtime build dependencies for Python packages
RUN apk add --no-cache \
    gcc \
    musl-dev \
    python3-dev

# Frontend assets (built natively in the previous stage)
COPY --from=frontend /build/static/ /app/static/

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
