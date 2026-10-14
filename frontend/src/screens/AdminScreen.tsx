import { useState } from 'react';
import type { FormEvent } from 'react';

import { createAdminUser, formatDateTime, hasAdminRole, orDash } from '../api';
import type { AdminUser, LoginResult } from '../api';
import {
  Alert,
  ApiErrorNotice,
  Badge,
  EmptyState,
  Field,
  IconUsers,
  statusTone,
} from '../ui';
import { useAction } from '../useAsync';

/**
 * Administration screen — provision external portal users.
 *
 * `POST /api/admin/users` is the only user endpoint in the contract, so this
 * screen is the writer, not a browser of a directory: it keeps the users
 * created in this session and shows exactly what the API answered (201 with
 * the stored record, 409 duplicate, 422 rejected role/payload). Nothing is
 * added to the list optimistically — a row appears only once the API created
 * it.
 */

const ROLE_OPTIONS = [
  { value: 'viewer', label: 'Viewer — read-only portal access' },
  { value: 'customer_service', label: 'Customer Service — request and distribute' },
  { value: 'qa', label: 'QA — validate and print' },
  { value: 'admin', label: 'Administrator — full portal access' },
];

interface FormState {
  username: string;
  email: string;
  role: string;
  organization: string;
}

const EMPTY_FORM: FormState = { username: '', email: '', role: 'viewer', organization: '' };

interface AdminScreenProps {
  session: LoginResult;
}

export function AdminScreen({ session }: AdminScreenProps) {
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [created, setCreated] = useState<AdminUser[]>([]);
  const create = useAction((payload: FormState) =>
    createAdminUser({
      username: payload.username,
      email: payload.email,
      role: payload.role,
      organization: payload.organization,
    }),
  );

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const createdUser = await create.run(form);
    if (createdUser) {
      setCreated((current) => [createdUser, ...current]);
      setForm({ ...EMPTY_FORM, organization: form.organization });
    }
  }

  const canAdminister = hasAdminRole(session.roles);

  return (
    <div className="stack">
      <header className="page-head">
        <div className="page-head__text">
          <p className="page-head__eyebrow">Administration</p>
          <h1 className="page-head__title">External portal users</h1>
          <p className="page-head__lede">
            Provision a Customer Service, QA or viewer account for a counterparty. The role
            is validated by the API — a rejected role comes back as 422 with the reason.
          </p>
        </div>
      </header>

      {!canAdminister ? (
        <Alert tone="info" title="Signed in without an administrator role">
          <p className="small">
            Roles on this session: {session.roles.length > 0 ? session.roles.join(', ') : 'none'}.
            The API will refuse the creation request if your account is not entitled to
            provision users.
          </p>
        </Alert>
      ) : null}

      <section className="card" aria-labelledby="admin-create-title">
        <div className="card__head">
          <h2 className="card__title" id="admin-create-title">
            <span className="row">
              <IconUsers size={18} />
              Create user
            </span>
          </h2>
          <p className="card__hint">POST /api/admin/users</p>
        </div>
        <div className="card__body stack">
          <form className="stack" onSubmit={submit} noValidate>
            <div className="grid-2">
              <Field label="Username" htmlFor="admin-username">
                <input
                  id="admin-username"
                  className="input"
                  type="text"
                  required
                  autoComplete="off"
                  value={form.username}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, username: event.target.value }))
                  }
                />
              </Field>
              <Field label="Email" htmlFor="admin-email">
                <input
                  id="admin-email"
                  className="input"
                  type="email"
                  required
                  autoComplete="off"
                  value={form.email}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, email: event.target.value }))
                  }
                />
              </Field>
              <Field label="Role" htmlFor="admin-role">
                <select
                  id="admin-role"
                  className="select"
                  required
                  value={form.role}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, role: event.target.value }))
                  }
                >
                  {ROLE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Organization" htmlFor="admin-organization">
                <input
                  id="admin-organization"
                  className="input"
                  type="text"
                  required
                  autoComplete="organization"
                  value={form.organization}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, organization: event.target.value }))
                  }
                />
              </Field>
            </div>
            <div className="row">
              <button type="submit" className="btn btn--primary" disabled={create.pending}>
                {create.pending ? 'Creating…' : 'Create user'}
              </button>
              <span className="muted small">
                Provisioned as {session.username} · role {form.role}
              </span>
            </div>
          </form>

          {create.error ? (
            <ApiErrorNotice
              error={create.error}
              title={
                create.error.status === 409
                  ? 'User already exists (409)'
                  : create.error.status === 422
                    ? 'The API rejected this user (422)'
                    : 'The user could not be created'
              }
            />
          ) : null}

          {create.result ? (
            <Alert tone="success" title="User created">
              <p className="small">
                <span className="mono">{orDash(create.result.username)}</span> ·{' '}
                {orDash(create.result.email)} · {orDash(create.result.organization)}
              </p>
            </Alert>
          ) : null}
        </div>
      </section>

      <section className="card" aria-labelledby="admin-list-title">
        <div className="card__head">
          <h2 className="card__title" id="admin-list-title">
            Users created in this session
          </h2>
          <p className="card__hint">
            {created.length} user{created.length === 1 ? '' : 's'} confirmed by the API
          </p>
        </div>
        <div className="card__body card__body--flush">
          {created.length === 0 ? (
            <EmptyState
              title="No users created yet"
              hint="Users appear here as soon as the API answers 201 with the stored record."
            />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <caption className="sr-only">
                  Portal users created during this session
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Username</th>
                    <th scope="col">Email</th>
                    <th scope="col">Role</th>
                    <th scope="col">Organization</th>
                    <th scope="col">Created at</th>
                  </tr>
                </thead>
                <tbody>
                  {created.map((user) => (
                    <tr key={`${user.username}-${user.created_at ?? ''}`}>
                      <td className="mono">{orDash(user.username)}</td>
                      <td>{orDash(user.email)}</td>
                      <td>
                        <Badge
                          value={user.role}
                          tone={
                            user.role.toLowerCase() === 'admin' ? 'complete' : statusTone(user.role)
                          }
                        />
                      </td>
                      <td>{orDash(user.organization)}</td>
                      <td>{formatDateTime(user.created_at ?? null)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
