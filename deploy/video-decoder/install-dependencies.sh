#!/bin/sh
# Build-time recipe only. The canonical image owner chooses when to consume it.
set -eu
apt-get update
apt-get install -y --no-install-recommends ffmpeg bubblewrap util-linux
mkdir -p /usr/local/share/leaf
dpkg-query -W -f='${binary:Package}\t${Version}\n' ffmpeg bubblewrap util-linux \
  > /usr/local/share/leaf/video-decoder-packages.tsv
test -x /usr/bin/ffmpeg
test -x /usr/bin/ffprobe
test -x /usr/bin/bwrap
test -x /usr/bin/prlimit
rm -rf /var/lib/apt/lists/*
