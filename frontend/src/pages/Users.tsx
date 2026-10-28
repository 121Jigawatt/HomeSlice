import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useMe } from "../auth";
import { Icon } from "../icons";
import { useToast } from "../toast";
import { OverflowMenu, useConfirm } from "../ui";

interface UserRow {
  id: number;
  username: string;
  disabled: boolean;
  must_change_password: boolean;
  created_at: number;
  last_login: number | null;
  roles: number[];
}

interface Role {
  id: number;
  name: string;
  description: string;
  permissions: string[];
  builtin: boolean;
  users: number;
}

interface PermissionInfo {
  key: string;
  group: string;
  label: string;
  description: string;
}

const ADMIN = "Admin";

function generatePassword(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_");
}

/** Users and roles, for people who can manage users. */
export function UsersAndRoles() {
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [roles, setRoles] = useState<Role[]>([]);
  const [perms, setPerms] = useState<PermissionInfo[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [u, r] = await Promise.all([
        api<UserRow[]>("/api/users"),
        api<{ roles: Role[]; permissions: PermissionInfo[] }>("/api/roles"),
      ]);
      setUsers(u);
      setRoles(r.roles);
      setPerms(r.permissions);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  if (!users) return <div className="card muted">{error ?? "Loading users…"}</div>;
  return (
    <>
      <Users users={users} roles={roles} reload={load} />
      <Roles roles={roles} perms={perms} reload={load} />
    </>
  );
}

function RolePicker({ roles, value, onChange, label }: { roles: Role[]; value: number[]; onChange: (v: number[]) => void; label: string }) {
  return (
    <fieldset className="role-picker">
      <legend className="small">{label}</legend>
      {roles.map((r) => (
        <label key={r.id} className="check role-option">
          <input
            type="checkbox"
            checked={value.includes(r.id)}
            onChange={(e) => onChange(e.target.checked ? [...value, r.id] : value.filter((x) => x !== r.id))}
          />
          <span>
            <b>{r.name}</b>
            {r.description && <span className="muted small block">{r.description}</span>}
          </span>
        </label>
      ))}
    </fieldset>
  );
}

function PasswordField({ value, onChange, label }: { value: string; onChange: (v: string) => void; label: string }) {
  return (
    <label>
      {label}
      <div className="row nowrap">
        <input
          className="grow"
          type="text"
          autoComplete="off"
          spellCheck={false}
          minLength={12}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required
        />
        <button type="button" className="small secondary" onClick={() => onChange(generatePassword())}>
          Generate
        </button>
      </div>
    </label>
  );
}

function Users({ users, roles, reload }: { users: UserRow[]; roles: Role[]; reload: () => void }) {
  const me = useMe();
  const notify = useToast();
  const confirm = useConfirm();
  const [open, setOpen] = useState<{ id: number; mode: "roles" | "password" } | null>(null);
  const [adding, setAdding] = useState(false);
  const roleName = (id: number) => roles.find((r) => r.id === id)?.name ?? "?";

  async function run(fn: () => Promise<unknown>, done: string) {
    try {
      await fn();
      notify(done, { tone: "success" });
      setOpen(null);
      reload();
      return true;
    } catch (e) {
      notify((e as Error).message, { tone: "error" });
      return false;
    }
  }

  async function toggleDisabled(u: UserRow) {
    if (!u.disabled) {
      const ok = await confirm({
        title: `Disable ${u.username}?`,
        body: <p>They're signed out everywhere and can't sign in until you turn the account back on. Their models and jobs stay.</p>,
        confirmLabel: "Disable account",
        danger: true,
      });
      if (!ok) return;
    }
    run(
      () => api(`/api/users/${u.id}`, { method: "PATCH", json: { disabled: !u.disabled } }),
      u.disabled ? `${u.username} can sign in again.` : `${u.username} is disabled.`,
    );
  }

  async function remove(u: UserRow) {
    const ok = await confirm({
      title: `Delete ${u.username}?`,
      body: <p>The account is removed. Models and jobs they made stay, still labelled with their name.</p>,
      confirmLabel: "Delete user",
      danger: true,
    });
    if (ok) run(() => api(`/api/users/${u.id}`, { method: "DELETE" }), `Deleted ${u.username}.`);
  }

  return (
    <div className="card stack">
      <div className="row between">
        <h2>Users</h2>
        {!adding && (
          <button type="button" className="small" onClick={() => setAdding(true)}>
            <Icon name="user" size={16} /> Add user
          </button>
        )}
      </div>
      {adding && <AddUser roles={roles} onDone={(added) => (setAdding(false), added && reload())} />}
      <ul className="list">
        {users.map((u) => {
          const self = me.owns(u.username);
          return (
            <li key={u.id} className="stack user-row">
              <div className="row nowrap">
                <span className="user-avatar" aria-hidden="true">
                  {u.username.slice(0, 1).toUpperCase()}
                </span>
                <div className="grow">
                  <div className="title">
                    {u.username}
                    {self && <span className="muted small"> (you)</span>}
                  </div>
                  <div className="row small role-badges">
                    {u.roles.length ? (
                      u.roles.map((r) => (
                        <span key={r} className="badge">
                          {roleName(r)}
                        </span>
                      ))
                    ) : (
                      <span className="badge warn">No role: can't do anything</span>
                    )}
                    {u.disabled && <span className="badge failed">Disabled</span>}
                    {u.must_change_password && !u.disabled && <span className="badge queued">Must set password</span>}
                  </div>
                  <div className="muted small">
                    {u.last_login ? `Last signed in ${new Date(u.last_login * 1000).toLocaleString()}` : "Never signed in"}
                  </div>
                </div>
                <OverflowMenu
                  label={`Actions for ${u.username}`}
                  items={[
                    { label: "Change roles…", icon: "edit", onSelect: () => setOpen({ id: u.id, mode: "roles" }) },
                    ...(self
                      ? []
                      : [
                          { label: "Reset password…", icon: "key" as const, onSelect: () => setOpen({ id: u.id, mode: "password" }) },
                          {
                            label: u.disabled ? "Enable account" : "Disable account…",
                            icon: "offline" as const,
                            onSelect: () => toggleDisabled(u),
                          },
                          { label: "Delete user…", icon: "trash" as const, danger: true, onSelect: () => remove(u) },
                        ]),
                  ]}
                />
              </div>
              {open?.id === u.id && open.mode === "roles" && (
                <EditRoles
                  user={u}
                  roles={roles}
                  onCancel={() => setOpen(null)}
                  onSave={(ids) =>
                    run(() => api(`/api/users/${u.id}`, { method: "PATCH", json: { roles: ids } }), `Updated ${u.username}'s roles.`)
                  }
                />
              )}
              {open?.id === u.id && open.mode === "password" && (
                <ResetPassword
                  user={u}
                  onCancel={() => setOpen(null)}
                  onSave={(password, mustChange) =>
                    run(
                      () => api(`/api/users/${u.id}/password`, { method: "POST", json: { password, must_change_password: mustChange } }),
                      `${u.username}'s password was reset and they were signed out.`,
                    )
                  }
                />
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function AddUser({ roles, onDone }: { roles: Role[]; onDone: (added: boolean) => void }) {
  const notify = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState(generatePassword);
  const [roleIds, setRoleIds] = useState<number[]>(() => {
    const maker = roles.find((r) => r.name === "Maker");
    return maker ? [maker.id] : [];
  });
  const [mustChange, setMustChange] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/users", { method: "POST", json: { username, password, roles: roleIds, must_change_password: mustChange } });
      notify(`Added ${username}. Share the password with them privately.`, { tone: "success" });
      onDone(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack sub-card" onSubmit={submit} aria-label="Add a user">
      <strong>New user</strong>
      <label>
        Username
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" pattern="[A-Za-z0-9_.\-]{3,32}" title="3-32 letters, digits, . _ -" required />
      </label>
      <PasswordField label="Temporary password (12+ characters)" value={password} onChange={setPassword} />
      <RolePicker label="Roles" roles={roles} value={roleIds} onChange={setRoleIds} />
      <label className="row check">
        <input type="checkbox" checked={mustChange} onChange={(e) => setMustChange(e.target.checked)} />
        Ask them to choose their own password when they first sign in
      </label>
      {error && <p className="error">{error}</p>}
      <div className="row end">
        <button type="button" className="secondary small" onClick={() => onDone(false)}>
          Cancel
        </button>
        <button className="small" disabled={busy}>
          {busy ? "Adding…" : "Add user"}
        </button>
      </div>
    </form>
  );
}

function EditRoles({ user, roles, onCancel, onSave }: { user: UserRow; roles: Role[]; onCancel: () => void; onSave: (ids: number[]) => void }) {
  const [ids, setIds] = useState(user.roles);
  return (
    <form
      className="stack sub-card"
      onSubmit={(e) => {
        e.preventDefault();
        onSave(ids);
      }}
    >
      <RolePicker label={`Roles for ${user.username}`} roles={roles} value={ids} onChange={setIds} />
      <div className="row end">
        <button type="button" className="secondary small" onClick={onCancel}>
          Cancel
        </button>
        <button className="small">Save roles</button>
      </div>
    </form>
  );
}

function ResetPassword({ user, onCancel, onSave }: { user: UserRow; onCancel: () => void; onSave: (p: string, mustChange: boolean) => void }) {
  const [password, setPassword] = useState(generatePassword);
  const [mustChange, setMustChange] = useState(true);
  return (
    <form
      className="stack sub-card"
      onSubmit={(e) => {
        e.preventDefault();
        onSave(password, mustChange);
      }}
    >
      <PasswordField label={`New password for ${user.username}`} value={password} onChange={setPassword} />
      <label className="row check">
        <input type="checkbox" checked={mustChange} onChange={(e) => setMustChange(e.target.checked)} />
        Ask them to choose their own password at next sign-in
      </label>
      <p className="muted small">They're signed out everywhere. Share the new password with them privately.</p>
      <div className="row end">
        <button type="button" className="secondary small" onClick={onCancel}>
          Cancel
        </button>
        <button className="small">Reset password</button>
      </div>
    </form>
  );
}

function Roles({ roles, perms, reload }: { roles: Role[]; perms: PermissionInfo[]; reload: () => void }) {
  const notify = useToast();
  const confirm = useConfirm();
  // null: nothing open; 0: a new role; otherwise the role being edited.
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState<Omit<Role, "id" | "builtin" | "users"> | null>(null);
  const label = (key: string) => perms.find((p) => p.key === key)?.label ?? key;

  const edit = (r: Role | null, copy = false) => {
    setEditing(r && !copy ? r.id : 0);
    setDraft(
      r
        ? { name: copy ? `${r.name} copy` : r.name, description: r.description, permissions: r.permissions }
        : { name: "", description: "", permissions: [] },
    );
  };

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!draft) return;
    try {
      if (editing) await api(`/api/roles/${editing}`, { method: "PUT", json: draft });
      else await api("/api/roles", { method: "POST", json: draft });
      notify(`Saved the ${draft.name} role.`, { tone: "success" });
      setEditing(null);
      setDraft(null);
      reload();
    } catch (err) {
      notify((err as Error).message, { tone: "error" });
    }
  }

  async function remove(r: Role) {
    const ok = await confirm({
      title: `Delete the ${r.name} role?`,
      body: (
        <p>
          {r.users
            ? `${r.users} user${r.users === 1 ? "" : "s"} will lose the permissions it gives them.`
            : "No one has this role."}
        </p>
      ),
      confirmLabel: "Delete role",
      danger: true,
    });
    if (!ok) return;
    try {
      await api(`/api/roles/${r.id}`, { method: "DELETE" });
      notify(`Deleted the ${r.name} role.`, { tone: "success" });
      reload();
    } catch (err) {
      notify((err as Error).message, { tone: "error" });
    }
  }

  const groups = [...new Set(perms.map((p) => p.group))];
  const editor = draft && (
    <form className="stack sub-card" onSubmit={save} aria-label={editing ? `Edit ${draft.name}` : "New role"}>
      <label>
        Name
        <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} maxLength={40} required />
      </label>
      <label>
        Description
        <input value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} maxLength={200} />
      </label>
      {groups.map((g) => (
        <fieldset key={g} className="perm-group">
          <legend>{g}</legend>
          {perms
            .filter((p) => p.group === g)
            .map((p) => (
              <label key={p.key} className="check perm-option">
                <input
                  type="checkbox"
                  checked={draft.permissions.includes(p.key)}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      permissions: e.target.checked ? [...draft.permissions, p.key] : draft.permissions.filter((x) => x !== p.key),
                    })
                  }
                />
                <span>
                  <b>{p.label}</b>
                  <span className="muted small block">{p.description}</span>
                </span>
              </label>
            ))}
        </fieldset>
      ))}
      <div className="row end">
        <button type="button" className="secondary small" onClick={() => (setEditing(null), setDraft(null))}>
          Cancel
        </button>
        <button className="small">{editing ? "Save role" : "Create role"}</button>
      </div>
    </form>
  );

  return (
    <div className="card stack">
      <div className="row between">
        <h2>Roles</h2>
        {editing === null && (
          <button type="button" className="small secondary" onClick={() => edit(null)}>
            New role
          </button>
        )}
      </div>
      <p className="muted small users-intro">
        A role is a set of permissions. People can have several roles and get everything any of them allows. Changes
        apply straight away.
      </p>
      {editing === 0 && editor}
      <ul className="list">
        {roles.map((r) => (
          <li key={r.id} className="stack user-row">
            <div className="row nowrap">
              <div className="grow">
                <div className="title">
                  {r.name} <span className="muted small">· {r.users} user{r.users === 1 ? "" : "s"}</span>
                </div>
                {r.description && <div className="muted small">{r.description}</div>}
                <div className="small perm-summary">
                  {r.name === ADMIN ? "Every permission, including future ones" : r.permissions.map(label).join(" · ") || "No permissions"}
                </div>
              </div>
              <OverflowMenu
                label={`Actions for the ${r.name} role`}
                items={[
                  ...(r.name === ADMIN ? [] : [{ label: "Edit…", icon: "edit" as const, onSelect: () => edit(r) }]),
                  { label: "Duplicate…", icon: "library", onSelect: () => edit(r, true) },
                  ...(r.name === ADMIN ? [] : [{ label: "Delete role…", icon: "trash" as const, danger: true, onSelect: () => remove(r) }]),
                ]}
              />
            </div>
            {editing === r.id && editor}
          </li>
        ))}
      </ul>
    </div>
  );
}
