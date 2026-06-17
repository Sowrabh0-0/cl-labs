type VmSummary = {
  id: string;
  name: string;
  host: string;
  protocol: string;
  status: string;
  guacamoleConnectionId: string;
  guacamoleLaunchUrl: string;
};

type UserSummary = {
  id: number;
  username: string;
  isAdmin: boolean;
  vmId: string | null;
  vmName: string | null;
  createdAt: number;
};

type SessionSummary = {
  username: string;
  isAdmin: boolean;
  vm: VmSummary | null;
  expiresAt: number;
  idleExpiresAt: number;
};

type SetupStatus = {
  needsSetup: boolean;
};

const API_BASE_URL =
  window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost"
    ? "http://localhost:8000"
    : "";
const app = document.querySelector<HTMLDivElement>("#app");

if (!app) {
  throw new Error("App root was not found");
}

const root = app;
let heartbeatTimer: number | undefined;

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
    ...options,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(body.detail ?? "Request failed");
  }

  return response.json() as Promise<T>;
}

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatTime(epochSeconds: number): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    day: "2-digit",
    month: "short",
  }).format(new Date(epochSeconds * 1000));
}

function renderShell(content: string): void {
  root.innerHTML = `
    <main class="shell">
      <section class="brand-panel">
        <p class="eyebrow">Clahan Labs</p>
        <h1>VM Gateway</h1>
        <p class="lede">Authenticate, resolve the assigned VM, and continue into the browser session through Guacamole.</p>
      </section>
      <section class="work-panel">${content}</section>
    </main>
  `;
}

function stopHeartbeat(): void {
  if (heartbeatTimer) {
    window.clearInterval(heartbeatTimer);
    heartbeatTimer = undefined;
  }
}

function startHeartbeat(): void {
  stopHeartbeat();
  heartbeatTimer = window.setInterval(async () => {
    try {
      await api<SessionSummary>("/api/session/heartbeat", { method: "POST" });
    } catch {
      stopHeartbeat();
      renderLogin("Session expired. Please sign in again.");
    }
  }, 60_000);
}

function renderSetup(message = ""): void {
  stopHeartbeat();
  renderShell(`
    <form id="setup-form" class="login-form">
      <div>
        <h2>Setup admin</h2>
        <p class="muted">Create the first admin account. Credentials will be stored in SQLite, not source code.</p>
      </div>
      <label>
        Admin username
        <input name="username" autocomplete="username" required />
      </label>
      <label>
        Admin password
        <input name="password" type="password" autocomplete="new-password" minlength="8" required />
      </label>
      ${message ? `<p class="error">${escapeHtml(message)}</p>` : ""}
      <button type="submit">Create admin</button>
    </form>
  `);

  document.querySelector<HTMLFormElement>("#setup-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget as HTMLFormElement);

    try {
      const session = await api<SessionSummary>("/api/setup/admin", {
        method: "POST",
        body: JSON.stringify({
          username: form.get("username"),
          password: form.get("password"),
        }),
      });
      renderSession(session);
    } catch (error) {
      renderSetup(error instanceof Error ? error.message : "Setup failed");
    }
  });
}

function renderLogin(message = ""): void {
  stopHeartbeat();
  renderShell(`
    <form id="login-form" class="login-form">
      <div>
        <h2>Sign in</h2>
        <p class="muted">Use an account created by the admin dashboard.</p>
      </div>
      <label>
        Username
        <input name="username" autocomplete="username" required />
      </label>
      <label>
        Password
        <input name="password" type="password" autocomplete="current-password" required />
      </label>
      ${message ? `<p class="error">${escapeHtml(message)}</p>` : ""}
      <button type="submit">Continue</button>
    </form>
  `);

  document.querySelector<HTMLFormElement>("#login-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget as HTMLFormElement);

    try {
      const session = await api<SessionSummary>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({
          username: form.get("username"),
          password: form.get("password"),
        }),
      });
      renderSession(session);
    } catch (error) {
      renderLogin(error instanceof Error ? error.message : "Login failed");
    }
  });
}

function renderSession(session: SessionSummary): void {
  startHeartbeat();
  const vm = session.vm;
  renderShell(`
    <div class="dashboard">
      <div class="dashboard-topbar">
        <div>
          <p class="eyebrow">Workspace</p>
          <h2>${escapeHtml(session.username)}</h2>
        </div>
        <div class="actions compact-actions">
          ${session.isAdmin ? `<button id="admin-button" class="secondary-button" type="button">Admin</button>` : ""}
          <button id="logout-button" class="secondary-button" type="button">Logout</button>
        </div>
      </div>

      <div class="dashboard-grid">
        <section class="primary-panel">
          <div>
            <p class="eyebrow">${vm ? "Assigned VM" : "No VM assigned"}</p>
            <h3>${escapeHtml(vm?.name ?? "Waiting for assignment")}</h3>
            <p class="muted">${vm ? "Your remote desktop is ready through Apache Guacamole." : "An admin needs to map your account to a VM before you can connect."}</p>
          </div>

          <dl class="detail-grid">
            <div><dt>Host</dt><dd>${escapeHtml(vm?.host ?? "-")}</dd></div>
            <div><dt>Protocol</dt><dd>${escapeHtml(vm?.protocol?.toUpperCase() ?? "-")}</dd></div>
            <div><dt>Status</dt><dd>${escapeHtml(vm?.status ?? "-")}</dd></div>
            <div><dt>Connection</dt><dd>${escapeHtml(vm?.guacamoleConnectionId ?? "-")}</dd></div>
          </dl>

          <div class="actions">
            ${
              vm
                ? `<a class="primary-link" href="${escapeHtml(vm.guacamoleLaunchUrl)}" target="_blank" rel="noreferrer">Open VM Session</a>`
                : ""
            }
            <button id="refresh-button" class="secondary-button" type="button">Refresh</button>
          </div>
        </section>

        <aside class="side-panel">
          <div>
            <p class="eyebrow">Session</p>
            <dl class="stacked-details">
              <div><dt>Role</dt><dd>${session.isAdmin ? "Admin" : "User"}</dd></div>
              <div><dt>Max session</dt><dd>${formatTime(session.expiresAt)}</dd></div>
              <div><dt>Idle timeout</dt><dd>${formatTime(session.idleExpiresAt)}</dd></div>
            </dl>
          </div>
          <div>
            <p class="eyebrow">Access policy</p>
            <p class="muted">A VM can be mapped to one regular user at a time. Admin users can manage and inspect mappings.</p>
          </div>
        </aside>
      </div>
    </div>
  `);

  document.querySelector<HTMLButtonElement>("#admin-button")?.addEventListener("click", () => {
    void renderAdmin();
  });
  document.querySelector<HTMLButtonElement>("#refresh-button")?.addEventListener("click", boot);
  document.querySelector<HTMLButtonElement>("#logout-button")?.addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST" });
    renderLogin();
  });
}

async function renderAdmin(message = ""): Promise<void> {
  startHeartbeat();
  try {
    const [users, vms] = await Promise.all([
      api<UserSummary[]>("/api/admin/users"),
      api<VmSummary[]>("/api/admin/vms"),
    ]);

    const vmOptions = [
      `<option value="">No VM</option>`,
      ...vms.map((vm) => `<option value="${escapeHtml(vm.id)}">${escapeHtml(vm.name)} (${escapeHtml(vm.host)})</option>`),
    ].join("");
    const userOptions = users
      .map((user) => `<option value="${escapeHtml(user.username)}">${escapeHtml(user.username)}</option>`)
      .join("");

    renderShell(`
      <div class="admin-panel">
        <div class="session-header">
          <div>
            <p class="eyebrow">Admin dashboard</p>
            <h2>Users and VM mapping</h2>
          </div>
          <button id="back-button" class="secondary-button" type="button">Back</button>
        </div>

        ${message ? `<p class="notice">${escapeHtml(message)}</p>` : ""}

        <form id="create-user-form" class="admin-form">
          <h3>Create user</h3>
          <label>Username<input name="username" required /></label>
          <label>Password<input name="password" type="password" minlength="8" required /></label>
          <label>VM<select name="vmId">${vmOptions}</select></label>
          <label class="check-row"><input name="isAdmin" type="checkbox" /> Admin user</label>
          <button type="submit">Create user</button>
        </form>

        <form id="reset-password-form" class="admin-form">
          <h3>Reset password and sync Guacamole</h3>
          <label>User<select name="username" required>${userOptions}</select></label>
          <label>New password<input name="password" type="password" minlength="8" required /></label>
          <button type="submit">Reset and sync</button>
        </form>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>User</th>
                <th>Role</th>
                <th>Mapped VM</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              ${users
                .map(
                  (user) => `
                    <tr>
                      <td>${escapeHtml(user.username)}</td>
                      <td>${user.isAdmin ? "Admin" : "User"}</td>
                      <td>${escapeHtml(user.vmName ?? user.vmId ?? "-")}</td>
                      <td>${formatTime(user.createdAt)}</td>
                    </tr>
                  `,
                )
                .join("")}
            </tbody>
          </table>
        </div>

        <form id="create-vm-form" class="admin-form">
          <h3>Register VM</h3>
          <label>VM ID<input name="id" required /></label>
          <label>Name<input name="name" required /></label>
          <label>Host/IP<input name="host" required /></label>
          <label>Guacamole connection ID<input name="guacamoleConnectionId" required /></label>
          <label>Launch URL<input name="guacamoleLaunchUrl" /></label>
          <button type="submit">Register VM</button>
        </form>
      </div>
    `);

    document.querySelector<HTMLButtonElement>("#back-button")?.addEventListener("click", boot);
    document.querySelector<HTMLFormElement>("#create-user-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget as HTMLFormElement);
      try {
        await api("/api/admin/users", {
          method: "POST",
          body: JSON.stringify({
            username: form.get("username"),
            password: form.get("password"),
            vmId: form.get("vmId") || null,
            isAdmin: form.get("isAdmin") === "on",
          }),
        });
        await renderAdmin("User created and synced to Guacamole.");
      } catch (error) {
        await renderAdmin(error instanceof Error ? error.message : "User creation failed.");
      }
    });

    document.querySelector<HTMLFormElement>("#reset-password-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget as HTMLFormElement);
      const username = String(form.get("username") ?? "");
      try {
        await api(`/api/admin/users/${encodeURIComponent(username)}/password`, {
          method: "PUT",
          body: JSON.stringify({
            password: form.get("password"),
          }),
        });
        await renderAdmin("Password reset and Guacamole sync completed.");
      } catch (error) {
        await renderAdmin(error instanceof Error ? error.message : "Password reset failed.");
      }
    });

    document.querySelector<HTMLFormElement>("#create-vm-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget as HTMLFormElement);
      try {
        await api("/api/admin/vms", {
          method: "POST",
          body: JSON.stringify({
            id: form.get("id"),
            name: form.get("name"),
            host: form.get("host"),
            protocol: "rdp",
            status: "manual-ready",
            guacamoleConnectionId: form.get("guacamoleConnectionId"),
            guacamoleLaunchUrl: form.get("guacamoleLaunchUrl") || null,
          }),
        });
        await renderAdmin("VM registered and synced to Guacamole.");
      } catch (error) {
        await renderAdmin(error instanceof Error ? error.message : "VM registration failed.");
      }
    });
  } catch (error) {
    renderLogin(error instanceof Error ? error.message : "Admin access failed");
  }
}

async function boot(): Promise<void> {
  try {
    const setup = await api<SetupStatus>("/api/setup/status");
    if (setup.needsSetup) {
      renderSetup();
      return;
    }

    const session = await api<SessionSummary>("/api/session");
    renderSession(session);
  } catch {
    renderLogin();
  }
}

void boot();
