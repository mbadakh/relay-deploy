#!/bin/sh
set -eu
set -a
. "/run/relay/$1.env"
set +a
shift
exec "$@"
