#!/bin/sh
# Make /data writable for the unprivileged `smooth` user, then drop root.
# Started as root (the default): fix ownership of a bind-mounted /data that
# Docker created as root, then run as uid 1000. Started with --user: just run.
set -e
if [ "$(id -u)" = "0" ]; then
  if [ "$(stat -c %u /data)" != "$(id -u smooth)" ]; then
    chown -R smooth:smooth /data
  fi
  exec setpriv --reuid=smooth --regid=smooth --init-groups --inh-caps=-all \
    env HOME=/home/smooth tini -- smooth-agent "$@"
fi
exec tini -- smooth-agent "$@"
