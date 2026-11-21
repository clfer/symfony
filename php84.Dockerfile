FROM php:8.4-cli
ARG HTTPS_PROXY
COPY ccr.crt /usr/local/share/ca-certificates/ccr.crt
RUN update-ca-certificates \
 && printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$HTTPS_PROXY" "$HTTPS_PROXY" > /etc/apt/apt.conf.d/99proxy \
 && sed -i 's|http://|https://|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update -q && apt-get install -y -q git unzip \
 && rm -f /etc/apt/apt.conf.d/99proxy && git config --system --add safe.directory '*'
