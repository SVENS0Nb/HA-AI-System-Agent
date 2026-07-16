FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONHASHSEED=random

WORKDIR /opt/ha-agent

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app ./app
COPY run.sh /run.sh
RUN chmod 0755 /run.sh

CMD ["/run.sh"]
