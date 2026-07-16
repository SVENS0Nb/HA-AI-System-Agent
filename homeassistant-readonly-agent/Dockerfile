FROM golang:1.26-trixie@sha256:117e07f49461abb984fc8aef661432461ff43d06faa22c3b73af6a49ce325cb9 AS signal-api-build

ARG SIGNAL_API_COMMIT=a4f5855b65d47bfe427735b5660053d1cc00c580

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && git init /signal-api \
    && cd /signal-api \
    && git remote add origin https://github.com/bbernhard/signal-cli-rest-api.git \
    && git sparse-checkout init --cone \
    && git sparse-checkout set src \
    && git -c protocol.version=2 fetch --depth=1 --filter=blob:none origin "${SIGNAL_API_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${SIGNAL_API_COMMIT}" \
    && sed -i 's/router.Run()/router.Run("127.0.0.1:" + port)/' src/main.go \
    && grep -F 'router.Run("127.0.0.1:" + port)' src/main.go \
    && cd src \
    && go test ./client ./utils \
    && go build -trimpath -o /signal-cli-rest-api main.go

FROM bbernhard/signal-cli-rest-api:0.100@sha256:2399d449123cdad56c4d859277e3b9127e1a00c4d2ab4601c239882609286cf8

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONHASHSEED=random \
    SIGNAL_BRIDGE_ENTRYPOINT=/usr/local/bin/signal-bridge-entrypoint \
    SIGNAL_CLI_CONFIG_DIR=/data/signal-cli \
    MODE=json-rpc \
    PORT=8080

USER root

COPY signal-bridge-entrypoint.sh /usr/local/bin/signal-bridge-entrypoint
COPY signal-api.conf /etc/supervisor/conf.d/signal-api.conf
COPY signal-supervisord.conf /etc/supervisor/signal-bridge.conf

RUN chmod 0755 /usr/local/bin/signal-bridge-entrypoint \
    && /usr/bin/supervisord -t -c /etc/supervisor/signal-bridge.conf \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/ha-agent-venv

COPY --from=signal-api-build /signal-cli-rest-api /usr/bin/signal-cli-rest-api

WORKDIR /opt/ha-agent

COPY requirements.txt ./requirements.txt
RUN /opt/ha-agent-venv/bin/python -m pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app ./app
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/ha-ai-system-agent/THIRD_PARTY_NOTICES.md
COPY run.sh /run.sh
RUN chmod 0755 /run.sh

ENTRYPOINT ["/run.sh"]
