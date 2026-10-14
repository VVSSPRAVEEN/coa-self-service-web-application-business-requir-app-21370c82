#!/bin/sh
# vite preview bundles vite.config.ts to a temporary file NEXT TO the config
# path; with a read-only root filesystem that write fails (EROFS) and the
# server never starts. Run from a tmpfs copy of the few files preview needs,
# with node_modules reached through a symlink into the read-only image.
set -eu
mkdir -p /tmp/app
cp /app/vite.config.ts /tmp/app/vite.config.ts
cp /app/package.json /tmp/app/package.json
rm -rf /tmp/app/dist
cp -r /app/dist /tmp/app/dist
ln -sfn /app/node_modules /tmp/app/node_modules
cd /tmp/app
exec npx vite preview --host 0.0.0.0 --port 5173
