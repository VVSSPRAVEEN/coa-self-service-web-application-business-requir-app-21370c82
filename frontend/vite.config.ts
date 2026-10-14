import { defineConfig, loadEnv } from 'vite';
import type { Plugin, ResolvedConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * Vite configuration for the Ventura COA self-service portal.
 *
 * THIS IS THE ONE AND ONLY Vite project in the repository. Its root is the
 * `frontend/` directory, which also holds `package.json`, `tsconfig.json`,
 * `index.html` and `src/`, so:
 *
 *   npm run dev      -> Vite dev server serving frontend/index.html
 *   npm run build    -> `tsc && vite build`, bundle emitted to frontend/dist
 *   npm run preview  -> serves frontend/dist (docker-compose's frontend service)
 *
 * Every non-asset request under `/api` is proxied to the FastAPI application so
 * the browser only ever talks to this one origin and no CORS exception is
 * required. The FastAPI contract is mounted at the application ROOT
 * (`/coas`, `/auth/login`, `/missing-coas`, `/admin/users`), so the `/api`
 * prefix is stripped by `rewrite` before the request leaves this process.
 *
 * `api` is the Compose service name published by docker-compose.yml on the
 * compose network; for a purely local run, point it at http://localhost:8000.
 *
 * These values are plain module constants: this file is executed by Node when
 * the dev/preview/build command boots, never in the browser bundle, so no
 * browser-only `import.meta.env` access is performed here. The one thing it
 * does read from the environment is the Cognito build gate below.
 */
const PORTAL_HOST = '0.0.0.0';
const PORTAL_PORT = 5173;
const API_BASE_PATH = '/api';
const API_ORIGIN = 'http://api:8000';

const apiProxy = {
  [API_BASE_PATH]: {
    target: API_ORIGIN,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ''),
  },
};

/**
 * Production Cognito gate — a build-time failure, not a runtime surprise.
 *
 * The bundle contains both gates: FR1's Cognito `Authenticator` and a local
 * bootstrap login for when no pool was compiled in. Nothing in the source can
 * tell which one a given artefact will run, so a production build that compiled
 * without `VITE_AWS_USER_POOL_ID` / `VITE_AWS_USER_POOL_CLIENT_ID` silently
 * shipped the bootstrap path and the deployment looked healthy. This plugin is
 * that missing failure: it throws before a single module is emitted.
 *
 * Expressed as a plugin because since Vite 3 `configResolved` is a plugin hook,
 * not a top-level `UserConfig` key. The hook runs for `build`, `dev` and
 * `preview` alike, which is why the scope below is stated explicitly:
 *   - `command === 'build'` only. `vite dev` and `vite preview` also run with
 *     NODE_ENV=production, so `command` — not NODE_ENV alone — is what keeps
 *     local and e2e runs working without a .env.
 *   - VITE_AUTH_STUB_ENABLED=true is the explicit escape hatch for building the
 *     bootstrap bundle on purpose (e2e, local demos). Any other value, unset
 *     included, leaves the requirement in force.
 *
 * Values come from `loadEnv`, which merges the matching `process.env` entries
 * over the `.env`/`.env.<mode>` files, so a build arg, a shell export and a dotenv
 * file are all honoured with the same precedence the bundle itself gets. Every
 * value is trimmed, so an empty or whitespace-only variable counts as unset.
 */
const COGNITO_BUILD_VARS = [
  'VITE_AWS_USER_POOL_ID',
  'VITE_AWS_USER_POOL_CLIENT_ID',
] as const;
const AUTH_STUB_FLAG = 'VITE_AUTH_STUB_ENABLED';

function assertProductionCognitoConfig(config: ResolvedConfig): void {
  if (config.command !== 'build') {
    return;
  }

  const env = loadEnv(config.mode, config.root, 'VITE_');
  if ((env[AUTH_STUB_FLAG] ?? '').trim().toLowerCase() === 'true') {
    return;
  }

  const missing = COGNITO_BUILD_VARS.filter((name) => (env[name] ?? '').trim().length === 0);
  if (missing.length === 0) {
    return;
  }

  throw new Error(
    [
      `Refusing to build the production portal bundle: ${missing.join(' and ')} ${
        missing.length === 1 ? 'is' : 'are'
      } unset.`,
      'The bundle contains FR1\'s Cognito Authenticator and a non-OIDC bootstrap login;',
      'without these values it would silently ship the bootstrap gate instead.',
      'Supply them at build time (build arg, shell export, or frontend/.env — see',
      'frontend/.env.example), or set VITE_AUTH_STUB_ENABLED=true to compile the',
      'bootstrap gate deliberately. `vite dev` and `vite preview` are unaffected.',
    ].join(' '),
  );
}

const productionCognitoGate: Plugin = {
  name: 'ventura:cognito-build-gate',
  configResolved: assertProductionCognitoConfig,
};

export default defineConfig({
  root: '.',
  plugins: [react(), productionCognitoGate],
  server: {
    host: PORTAL_HOST,
    port: PORTAL_PORT,
    strictPort: true,
    proxy: apiProxy,
  },
  preview: {
    host: PORTAL_HOST,
    port: PORTAL_PORT,
    strictPort: true,
    proxy: apiProxy,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
});
