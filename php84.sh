#!/bin/sh
# Run a command with PHP 8.4 (image sf-php84, see php84.Dockerfile) in the current worktree.
WD=${WD:-$PWD}
exec docker run --rm -i ${DOCKER_OPTS} --network host \
  -e HTTPS_PROXY="$HTTPS_PROXY" -e HTTP_PROXY="$HTTP_PROXY" -e NO_PROXY="$NO_PROXY" \
  -e https_proxy="$HTTPS_PROXY" -e http_proxy="$HTTP_PROXY" \
  -e SSL_CERT_FILE=/ca.crt -e CURL_CA_BUNDLE=/ca.crt -e COMPOSER_CAFILE=/ca.crt \
  -e COMPOSER_ROOT_VERSION="${COMPOSER_ROOT_VERSION:-8.2.x-dev}" -e COMPOSER_HOME=/tmp/composer -e COMPOSER_ALLOW_SUPERUSER=1 \
  -v /root/.ccr/ca-bundle.crt:/ca.crt:ro -v /usr/local/bin/composer:/usr/local/bin/composer:ro \
  -v /home/user/symfony/.git:/home/user/symfony/.git -v "$WD":"$WD" -w "$WD" sf-php84 "$@"
