# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Ventura COA self-service portal - production image.
#
# Build context: the repository ROOT, because this file lives below frontend/:
#   build: { context: ., dockerfile: frontend/Dockerfile }
#   docker build -f frontend/Dockerfile \
#     --build-arg VITE_AWS_USER_POOL_ID=us-east-1_xxxxxxxxx \
#     --build-arg VITE_AWS_USER_POOL_CLIENT_ID=1xxxxxxxxxxxxxxxxxxxxxxxxx .
#
# Stage 1 installs the frontend dependencies and compiles the bundle (tsc and
# `vite build`, output frontend/dist); stage 2 is the published image, serving
# that bundle with `vite preview` - the server docker-compose runs for this
# service.
#
# FR1 (Cognito + Azure AD): Vite inlines VITE_* variables at BUILD time, so the
# user pool has to reach this file as a build arg. There is deliberately NO
# default for VITE_AWS_USER_POOL_ID / VITE_AWS_USER_POOL_CLIENT_ID and NO
# default for VITE_AUTH_STUB_ENABLED. This image previously defaulted the stub
# flag to `true`, which silently published the non-OIDC bootstrap login in
# place of FR1's Authenticator; the build now fails loudly, naming the missing
# configuration, whenever the pool is not supplied (frontend/vite.config.ts,
# configResolved, enforces the same rule for `vite build`).
#
# VITE_AUTH_STUB_ENABLED may still be passed explicitly to compile the
# bootstrap bundle on purpose (local demos, air-gapped previews, the Playwright
# harness). It is never what this image defaults to.
# ---------------------------------------------------------------------------

FROM node:22-bookworm-slim AS build

WORKDIR /app
# The security scanner requires every stage to end on a non-root USER; the
# build stage compiles as the image's node user and owns its workspace.
RUN chown -R node:node /app
USER node

# Dependencies first, so source-only edits do not invalidate the npm layer.
COPY frontend/package*.json ./
RUN if [ -f package-lock.json ]; then \
      npm ci --no-audit --no-fund; \
    else \
      npm install --no-audit --no-fund; \
    fi

COPY frontend/ ./

# FR1: the Cognito pool is a build input of this image, not a default.
ARG VITE_AWS_USER_POOL_ID
ARG VITE_AWS_USER_POOL_CLIENT_ID
ARG VITE_AUTH_STUB_ENABLED
ENV VITE_AWS_USER_POOL_ID=${VITE_AWS_USER_POOL_ID} \
    VITE_AWS_USER_POOL_CLIENT_ID=${VITE_AWS_USER_POOL_CLIENT_ID} \
    VITE_AUTH_STUB_ENABLED=${VITE_AUTH_STUB_ENABLED}

RUN set -eu; \
    if [ -n "${VITE_AWS_USER_POOL_ID}" ] && [ -n "${VITE_AWS_USER_POOL_CLIENT_ID}" ]; then \
      echo "Building the Cognito bundle for user pool ${VITE_AWS_USER_POOL_ID}."; \
    elif [ "${VITE_AUTH_STUB_ENABLED}" = "true" ]; then \
      echo "::warning::Building the non-OIDC bootstrap bundle: VITE_AUTH_STUB_ENABLED=true was requested explicitly, so the image will not carry FR1 sign-in."; \
    else \
      echo "::error::VITE_AWS_USER_POOL_ID and VITE_AWS_USER_POOL_CLIENT_ID build args are required. This image serves the Cognito + Azure AD Authenticator (FR1). Pass --build-arg VITE_AWS_USER_POOL_ID=<pool> --build-arg VITE_AWS_USER_POOL_CLIENT_ID=<app client>, or pass VITE_AUTH_STUB_ENABLED=true only for a deliberate local/demo build." >&2; \
      exit 1; \
    fi; \
    npm run build

FROM node:22-bookworm-slim AS runtime

WORKDIR /app
ENV NODE_ENV=production
ENV HOST=0.0.0.0
ENV PORT=5173

# `vite preview` resolves outDir and the preview host/port from vite.config.ts,
# so the compiled bundle ships together with the config that serves it.
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/vite.config.ts ./vite.config.ts
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist

EXPOSE 5173

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:' + (process.env.PORT || '5173') + '/').then((r) => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"

COPY frontend/docker-entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh
USER node
ENTRYPOINT ["/entrypoint.sh"]
