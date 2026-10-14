import { StrictMode, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactElement } from 'react';
import { createRoot } from 'react-dom/client';

import { Amplify } from 'aws-amplify';
import { fetchAuthSession } from 'aws-amplify/auth';
import { Authenticator } from '@aws-amplify/ui-react';
import '@aws-amplify/ui-react/styles.css';

import { hasAdminRole, hasReportsRole, login, onUnauthorized, toApiError } from './api';
import type { ApiError, LoginResult } from './api';
import { AdminScreen } from './screens/AdminScreen';
import { DetailScreen } from './screens/DetailScreen';
import { ReportScreen } from './screens/ReportScreen';
import { SearchScreen } from './screens/SearchScreen';
import {
  Alert,
  ApiErrorNotice,
  Field,
  IconChart,
  IconDocument,
  IconRefresh,
  IconSearch,
  IconUsers,
  Loading,
} from './ui';
import './styles.css';

function readEnv(name: keyof ImportMetaEnv): string {
  const value = import.meta.env[name];
  return typeof value === 'string' ? value.trim() : '';
}

const USER_POOL_ID = readEnv('VITE_AWS_USER_POOL_ID');
const USER_POOL_CLIENT_ID = readEnv('VITE_AWS_USER_POOL_CLIENT_ID');
/**
 * Explicit opt-in for the non-OIDC bootstrap gate. A production `vite build`
 * refuses to compile a bundle without Cognito unless this is set to `true`
 * (see vite.config.ts), so a value of `true` here means somebody opted in.
 */
const AUTH_STUB_ENABLED = readEnv('VITE_AUTH_STUB_ENABLED').toLowerCase() === 'true';
const COGNITO_CONFIGURED = USER_POOL_ID.length > 0 && USER_POOL_CLIENT_ID.length > 0;

if (COGNITO_CONFIGURED) {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: USER_POOL_ID,
        userPoolClientId: USER_POOL_CLIENT_ID,
      },
    },
  });
}

type TopLevelView = 'search' | 'report' | 'admin';

interface View {
  name: TopLevelView | 'detail';
  coaId?: string;
}

/**
 * FR1 authentication gate, and FR3 role gating on top of it.
 *
 * With Cognito configured, `Authenticator` is the gate: the id token it issues
 * is exchanged once for the portal session. Where the pool is not configured —
 * a `vite preview` bundle built without a .env, or the e2e harness — the Cognito
 * host is unreachable, so a local bootstrap gate stands in front of the same
 * `POST /api/auth/login` call. Either way no screen is reachable before the API
 * has issued a session, and the bootstrap token is labelled as such: only an API
 * willing to accept it hands one out.
 *
 * The roles on that session then decide the navigation, mirroring the API's
 * `ROLE_RESOURCES` table: Search is for everyone, Report is offered to the roles
 * that grant `reports` (internal/admin/ventura_admin and `3pl`, never `customer`),
 * and Admin only to an internal/admin role. The gate is enforced in three places
 * so no path reaches the screen — the tab is not rendered, a view left open falls
 * back to Search when the roles change, and each restricted screen itself renders
 * only under the same predicate.
 */
function PortalShell({
  session,
  onSignOut,
}: {
  session: LoginResult;
  onSignOut: () => void;
}) {
  const [view, setView] = useState<View>({ name: 'search' });
  const [sessionExpired, setSessionExpired] = useState(false);
  const canAdminister = hasAdminRole(session.roles);
  const canViewReports = hasReportsRole(session.roles);

  useEffect(
    () =>
      onUnauthorized(() => {
        setSessionExpired(true);
      }),
    [],
  );

  useEffect(() => {
    if (!canAdminister && view.name === 'admin') {
      setView({ name: 'search' });
    } else if (!canViewReports && view.name === 'report') {
      setView({ name: 'search' });
    }
  }, [canAdminister, canViewReports, view.name]);

  function openDetail(coaId: string) {
    setView({ name: 'detail', coaId });
  }

  const tabs: Array<{
    key: TopLevelView;
    label: string;
    icon: (props: { size?: number }) => ReactElement;
    active: boolean;
  }> = [
    {
      key: 'search',
      label: 'Search',
      icon: IconSearch,
      active: view.name !== 'report' && view.name !== 'admin',
    },
    ...(canViewReports
      ? [{ key: 'report' as const, label: 'Report', icon: IconChart, active: view.name === 'report' }]
      : []),
    ...(canAdminister
      ? [{ key: 'admin' as const, label: 'Admin', icon: IconUsers, active: view.name === 'admin' }]
      : []),
  ];

  return (
    <div className="app">
      <header className="app-nav">
        <div className="app-nav__brand">
          <span className="app-nav__mark" aria-hidden="true">
            <IconDocument size={20} />
          </span>
          <span className="app-nav__wordmark">
            <span className="app-nav__title">Ventura COA Portal</span>
            <span className="app-nav__subtitle">Certificate self-service</span>
          </span>
        </div>

        <nav className="app-nav__tabs" aria-label="Portal sections">
          {tabs.map((tab) => {
            const Icon = tab.icon;
            return (
              <button
                key={tab.key}
                type="button"
                className="tab"
                aria-current={tab.active ? 'page' : undefined}
                onClick={() => setView({ name: tab.key })}
              >
                <Icon size={16} />
                {tab.label}
              </button>
            );
          })}
        </nav>

        <div className="app-nav__identity">
          <span className="identity">
            <span className="identity__name">{session.username}</span>
            <span className="identity__roles">
              {session.roles.length > 0 ? session.roles.join(' · ') : 'no roles'}
            </span>
          </span>
          <button type="button" className="btn btn--secondary" onClick={onSignOut}>
            Sign Out
          </button>
        </div>
      </header>

      <main className="app-main">
        {sessionExpired ? (
          <div className="notice-slot">
            <Alert
              tone="error"
              title="Your session is no longer valid (HTTP 401)"
              actions={
                <button type="button" className="btn btn--primary btn--sm" onClick={onSignOut}>
                  Sign in again
                </button>
              }
            >
              <p className="small">
                The API rejected the request for {session.username}. Sign in again to obtain
                a fresh session before continuing.
              </p>
            </Alert>
          </div>
        ) : null}

        {view.name === 'search' ? (
          <SearchScreen session={session} onOpen={openDetail} />
        ) : null}
        {view.name === 'detail' && view.coaId ? (
          <DetailScreen
            coaId={view.coaId}
            session={session}
            onBack={() => setView({ name: 'search' })}
          />
        ) : null}
        {view.name === 'report' && canViewReports ? (
          <ReportScreen session={session} onOpen={openDetail} />
        ) : null}
        {view.name === 'admin' && canAdminister ? <AdminScreen session={session} /> : null}
      </main>

      <footer className="app-footer">
        Ventura COA self-service portal · session {session.user_id} · expires{' '}
        {session.expires_at || 'not reported'}
      </footer>
    </div>
  );
}

/** Exchange the identity token for the portal session exactly once. */
function usePortalSession(token: string) {
  const [session, setSession] = useState<LoginResult | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const exchanged = useRef(false);

  useEffect(() => {
    if (exchanged.current) {
      return;
    }
    exchanged.current = true;
    let active = true;

    login(token).then(
      (result) => {
        if (active) {
          setSession(result);
          setLoading(false);
        }
      },
      (cause: unknown) => {
        if (active) {
          setError(toApiError(cause));
          setLoading(false);
        }
      },
    );

    return () => {
      active = false;
    };
  }, [token]);

  return { session, error, loading };
}

function SessionGate({
  token,
  onSignOut,
}: {
  token: string;
  onSignOut: () => void;
}) {
  const { session, error, loading } = usePortalSession(token);

  if (loading) {
    return (
      <div className="gate">
        <Loading message="Establishing the portal session…" />
      </div>
    );
  }

  if (error || !session) {
    return (
      <div className="gate">
        <div className="card gate__card">
          <div className="card__body">
            <p className="page-head__eyebrow">Ventura COA Portal</p>
            <h1 className="gate__title">Sign-in could not be completed</h1>
            <p className="gate__lede">
              The API did not issue a portal session for this identity, so no screen is
              available yet.
            </p>
            <div className="gate__form">
              {error ? <ApiErrorNotice error={error} /> : null}
              <button type="button" className="btn btn--primary" onClick={onSignOut}>
                Sign in again
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return <PortalShell session={session} onSignOut={onSignOut} />;
}

/**
 * Unconfigured gate: the same `POST /api/auth/login` handshake, but the
 * identity is a local bootstrap token because no Cognito pool was compiled
 * into this bundle. An API that does not accept it answers 401 and the operator
 * is told to sign in again — the gate never grants a session on its own.
 */
function LocalGate() {
  const [operator, setOperator] = useState('');
  const [token, setToken] = useState<string | null>(null);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const identity = operator.trim();
    if (identity.length > 0) {
      setToken(`local-bootstrap:${identity}`);
    }
  }

  if (token) {
    return <SessionGate token={token} onSignOut={() => setToken(null)} />;
  }

  return (
    <div className="gate">
      <div className="card gate__card">
        <div className="card__body">
          <p className="page-head__eyebrow">Ventura COA Portal</p>
          <h1 className="gate__title">Operator sign-in</h1>
          <p className="gate__lede">
            This bundle was built without a Cognito user pool
            (<span className="mono">VITE_AWS_USER_POOL_ID</span> /{' '}
            <span className="mono">VITE_AWS_USER_POOL_CLIENT_ID</span>), so identity is
            established locally and then exchanged with the API.
          </p>
          {AUTH_STUB_ENABLED ? null : (
            <Alert
              tone="warn"
              title="This bundle was not built for the bootstrap gate"
              actions={
                <span className="small">
                  A production build refuses to compile without Cognito unless{' '}
                  <span className="mono">VITE_AUTH_STUB_ENABLED=true</span> is set.
                </span>
              }
            >
              <p className="small">
                Neither the pool id nor the client id reached the compiler, and the explicit
                stub opt-in is absent, so this is a development or preview build.
              </p>
            </Alert>
          )}
          <form className="gate__form" onSubmit={submit}>
            <Field label="Operator identity" htmlFor="gate-operator">
              <input
                id="gate-operator"
                className="input"
                type="text"
                required
                autoComplete="username"
                autoFocus
                value={operator}
                onChange={(event) => setOperator(event.target.value)}
              />
            </Field>
            <button type="submit" className="btn btn--primary">
              <IconSearch size={16} />
              Sign in
            </button>
          </form>
          <ul className="gate__list">
            <li>The API must accept the bootstrap token before any screen opens.</li>
            <li>
              A rejected token answers 401 and the portal asks you to sign in again.
            </li>
            {AUTH_STUB_ENABLED ? (
              <li>
                The non-OIDC bootstrap gate was compiled in deliberately (
                <span className="mono">VITE_AUTH_STUB_ENABLED=true</span>).
              </li>
            ) : null}
          </ul>
        </div>
      </div>
    </div>
  );
}

function App() {
  if (!COGNITO_CONFIGURED) {
    return <LocalGate />;
  }

  return (
    <Authenticator>
      {({ user, signOut }) => (
        <CognitoSessionBridge username={user?.username} onSignOut={() => void signOut?.()} />
      )}
    </Authenticator>
  );
}

function CognitoSessionBridge({
  username,
  onSignOut,
}: {
  username?: string;
  onSignOut: () => void;
}) {
  const [idToken, setIdToken] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let active = true;
    fetchAuthSession().then(
      (authSession) => {
        const token = authSession.tokens?.idToken?.toString();
        if (!token) {
          throw new Error('The Cognito session carries no id token.');
        }
        if (active) {
          setIdToken(token);
          setFailure(null);
        }
      },
      (cause: unknown) => {
        if (active) {
          setFailure(cause instanceof Error ? cause.message : String(cause));
        }
      },
    );
    return () => {
      active = false;
    };
  }, [generation]);

  if (failure) {
    return (
      <div className="gate">
        <div className="card gate__card">
          <div className="card__body">
            <h1 className="gate__title">Cognito session unavailable</h1>
            <p className="gate__lede">{failure}</p>
            <div className="gate__form">
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => {
                  setFailure(null);
                  setGeneration((current) => current + 1);
                }}
              >
                <IconRefresh size={16} />
                Retry
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!idToken) {
    return (
      <div className="gate">
        <Loading
          message={`Reading the Cognito session${username ? ` for ${username}` : ''}…`}
        />
      </div>
    );
  }

  return (
    <SessionGate
      token={idToken}
      onSignOut={() => {
        setIdToken(null);
        onSignOut();
      }}
    />
  );
}

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Failed to find the root element');
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
