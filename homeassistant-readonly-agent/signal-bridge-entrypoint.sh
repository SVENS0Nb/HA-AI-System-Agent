#!/bin/sh
set -eu

umask 077

if [ "${SIGNAL_CLI_CONFIG_DIR:-}" != "/data/signal-cli" ]; then
    echo "Signal bridge refused an unexpected configuration directory." >&2
    exit 1
fi
if [ "${MODE:-}" != "json-rpc-native" ]; then
    echo "Signal bridge requires the lightweight native JSON-RPC mode." >&2
    exit 1
fi

install -d -m 0700 -o signal-api -g signal-api "$SIGNAL_CLI_CONFIG_DIR"

# libsignal extracts its architecture-specific JNI library before loading it.
# Home Assistant deliberately mounts /tmp with noexec, so use a private,
# container-ephemeral directory without weakening that security boundary.
runtime_tmp=/run/ha-ai-signal
install -d -m 0700 -o signal-api -g signal-api "$runtime_tmp"
find "$runtime_tmp" -xdev -depth -mindepth 1 -delete

# Generate the multi-account JSON-RPC mapping and the signal-cli program.
# The upstream image normally starts a detached system supervisor here. Running
# one foreground supervisor instead keeps every bridge process in the process
# group owned by the Python runtime, so stop/restart cannot leave an orphan that
# breaks the next launch.
/usr/bin/jsonrpc2-helper

jsonrpc_program=/etc/supervisor/conf.d/signal-cli-json-rpc-1.conf
if [ ! -s "$jsonrpc_program" ]; then
    echo "Signal bridge could not generate its JSON-RPC configuration." >&2
    exit 1
fi

sed -i \
    -e "s|^command=signal-cli-native |command=signal-cli-native -Djava.io.tmpdir=$runtime_tmp |" \
    -e 's|^stdout_logfile=.*|stdout_logfile=/dev/fd/1|' \
    -e 's|^stderr_logfile=.*|stderr_logfile=/dev/fd/2|' \
    -e 's|^stdout_logfile_maxbytes=.*|stdout_logfile_maxbytes=0|' \
    -e 's|^stdout_logfile_backups=.*|stdout_logfile_backups=0|' \
    "$jsonrpc_program"
if ! grep -F "command=signal-cli-native -Djava.io.tmpdir=$runtime_tmp " "$jsonrpc_program" >/dev/null; then
    echo "Signal bridge could not secure the native runtime directory." >&2
    exit 1
fi
cat >>"$jsonrpc_program" <<'EOF'
environment=HOME="/data/signal-cli",TMPDIR="/run/ha-ai-signal"
stopasgroup=true
killasgroup=true
stopwaitsecs=20
EOF

chown -hR signal-api:signal-api "$SIGNAL_CLI_CONFIG_DIR"
rm -f /run/signal-bridge-supervisor.sock /run/signal-bridge-supervisord.pid

exec /usr/bin/supervisord -n -c /etc/supervisor/signal-bridge.conf
