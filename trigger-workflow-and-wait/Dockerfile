FROM alpine:3.15.0

RUN <<EOF
apk update
apk --no-cache add \
    coreutils \
    curl \
    jq
EOF

COPY entrypoint.sh /entrypoint.sh

ENTRYPOINT ["sh", "/entrypoint.sh"]
