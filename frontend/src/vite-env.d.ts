/// <reference types="vite/client" />

/**
 * Typed build-time environment for the portal bundle.
 *
 * These are the ONLY `VITE_` variables the bundle reads (see `readEnv` in
 * src/main.tsx). They are declared here, next to the code that consumes them,
 * so a typo is a type error rather than a `undefined` that quietly disables a
 * feature at runtime.
 *
 * All three are optional in the type system because `vite dev` and
 * `vite preview` must keep working with no `.env` present — the production
 * requirement is enforced at build time instead, by the `configResolved` hook
 * in ../vite.config.ts, which throws when a production `vite build` finds
 * neither Cognito value unless `VITE_AUTH_STUB_ENABLED` is `true`. That hook
 * reads `process.env`, not this file, because a type cannot fail a build.
 */
interface ImportMetaEnv {
  /** Cognito user pool id, e.g. `us-west-2_Ab12Cd34E`. Required in production. */
  readonly VITE_AWS_USER_POOL_ID?: string;
  /** Cognito app client id that carries the `openid` scope. Required in production. */
  readonly VITE_AWS_USER_POOL_CLIENT_ID?: string;
  /**
   * Explicit opt-in (`'true'`) to compile the non-OIDC bootstrap gate into a
   * production bundle. Any other value, including unset, leaves the production
   * Cognito requirement in force.
   */
  readonly VITE_AUTH_STUB_ENABLED?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}