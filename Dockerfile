FROM python:3.13-alpine

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && rm -rf /build \
    && mkdir /data \
    && chown 65534:65534 /data

ENV PYTHONUNBUFFERED=1 \
    TASTARR_DATA_DIR=/data
WORKDIR /data
VOLUME ["/data"]

# Override with the UID/GID Transmission runs as (compose `user:`), so partial
# downloads are readable.
USER 65534:65534

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["tastarr", "--healthcheck"]

ENTRYPOINT ["tastarr"]
