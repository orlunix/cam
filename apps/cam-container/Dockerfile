# CAM WebUI image. Build context MUST be the repo root:
#   docker build -f apps/cam-container/Dockerfile -t cam-webui .
FROM node:20-bookworm-slim
WORKDIR /app

# ssh2 for the reused desktop backend modules (electron/*.cjs resolve it
# from apps/cam-desktop/node_modules); ws for the server itself.
# --omit=optional skips cpu-features (ssh2's optional native cipher
# accelerator — pure-JS ciphers are fine, and it saves a toolchain).
COPY apps/cam-desktop/package.json apps/cam-desktop/package-lock.json apps/cam-desktop/
COPY apps/cam-container/package.json apps/cam-container/package-lock.json apps/cam-container/
RUN cd apps/cam-desktop && npm ci --omit=dev --omit=optional \
 && cd ../cam-container && npm ci --omit=dev

# Repo-relative layout matters: embedded-hub.cjs / assistant-host.cjs
# resolve extensions/, dist/camc, dist/skillm via __dirname fallbacks.
COPY apps/cam-desktop/electron apps/cam-desktop/electron
COPY apps/cam-container/lib apps/cam-container/lib
COPY apps/cam-container/server.mjs apps/cam-container/
COPY extensions/host extensions/host
COPY extensions/packages extensions/packages
COPY extensions/vendor/cam-assist/dist extensions/vendor/cam-assist/dist
COPY web web
COPY dist/camc dist/camc
COPY dist/skillm dist/skillm

ENV CAM_DATA_DIR=/data \
    CAM_BIND=0.0.0.0 \
    CAM_PORT=8420 \
    HOME=/data/cam-home
VOLUME /data
EXPOSE 8420
CMD ["node", "apps/cam-container/server.mjs"]
