# Container image for the Ventura COA self-service portal.
#
# One image, one role: compile frontend/ into frontend/dist and serve that bundle
# with `vite preview` on container port 5173. `vite.config.ts` proxies every
# non-asset request under /api to http://api:8000, so the published bundle keeps
# its single-origin contract from inside the image ? no rebuild-time API URL is
# baked in and no CORS exception is needed.
#
# Build:  docker compose build frontend
# Run:    the compose `frontend` service, which publishes
#         ${P3_PREVIEW_FRONTEND_PORT:-5173}:5173 and health-checks this port.
FROM node:20-alpine

WORKDIR /app

# Dependencies first: the manifest changes far less often than src/, so this
# layer stays cached across source-only edits. devDependencies are required ?
# `npm run build` is `tsc && vite build`.
COPY package*.json ./
RUN npm ci --no-audit --no-fund

COPY . .
# Cognito build gate inputs. The gate in vite.config.ts refuses a production
# `vite build` without a pool unless VITE_AUTH_STUB_ENABLED=true. This image
# defaults to the explicit bootstrap build for this deployment; supply the
# Cognito vars via compose build args to compile the FR1 Authenticator bundle.
ARG VITE_AWS_USER_POOL_ID=""
ARG VITE_AWS_USER_POOL_CLIENT_ID=""
ARG VITE_AUTH_STUB_ENABLED="true"
ENV VITE_AWS_USER_POOL_ID=$VITE_AWS_USER_POOL_ID \
    VITE_AWS_USER_POOL_CLIENT_ID=$VITE_AWS_USER_POOL_CLIENT_ID \
    VITE_AUTH_STUB_ENABLED=$VITE_AUTH_STUB_ENABLED
RUN npm run build

EXPOSE 5173

USER node
CMD ["npx","vite","preview","--host","0.0.0.0","--port","5173","--strictPort"]