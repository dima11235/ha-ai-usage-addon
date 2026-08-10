ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.22
FROM ${BUILD_FROM}

COPY rootfs/ /

RUN chmod +x /etc/services.d/ai-usage-monitor/run \
    && chmod +x /usr/local/bin/ai_usage_monitor.py

ENV PYTHONUNBUFFERED=1
