type VmSummary = {
  id: string;
  name: string;
  host: string;
  protocol: string;
  status: string;
  guacamoleConnectionId: string;
  guacamoleLaunchUrl: string;
  rdpUsername: string | null;
  rdpDomain: string | null;
  security: string;
  ignoreCert: boolean;
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

type GuacamoleLaunchResponse = {
  launchUrl: string;
  expiresAt: number;
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

function displayProtocol(protocol?: string): string {
  if (!protocol) {
    return "-";
  }
  return protocol.toLowerCase() === "rdp" ? "Remote Desktop" : protocol.toUpperCase();
}

function displaySecurity(security?: string): string {
  const labels: Record<string, string> = {
    any: "Automatic",
    nla: "Network Level",
    rdp: "Standard",
    tls: "Encrypted",
  };
  return labels[String(security ?? "").toLowerCase()] ?? "Automatic";
}

function securityOptions(selected = "any"): string {
  return [
    ["any", "Automatic"],
    ["nla", "Network Level"],
    ["rdp", "Standard"],
    ["tls", "Encrypted"],
  ]
    .map(
      ([value, label]) =>
        `<option value="${value}" ${selected === value ? "selected" : ""}>${label}</option>`,
    )
    .join("");
}

function vmFormFields(vm?: VmSummary): string {
  return `
    <label>Name<input name="name" value="${escapeHtml(vm?.name ?? "")}" required /></label>
    <label>Host/IP<input name="host" value="${escapeHtml(vm?.host ?? "")}" required /></label>
    <label>Connection profile ID<input name="guacamoleConnectionId" value="${escapeHtml(vm?.guacamoleConnectionId ?? "")}" required /></label>
    <label>Status<input name="status" value="${escapeHtml(vm?.status ?? "manual-ready")}" required /></label>
    <label>Remote username<input name="rdpUsername" value="${escapeHtml(vm?.rdpUsername ?? "")}" autocomplete="off" /></label>
    <label>Remote password<input name="rdpPassword" type="password" autocomplete="new-password" placeholder="${vm ? "Leave blank to keep current password" : ""}" /></label>
    <label>Remote domain<input name="rdpDomain" value="${escapeHtml(vm?.rdpDomain ?? "")}" autocomplete="off" /></label>
    <label>Security mode<select name="security">${securityOptions(vm?.security ?? "any")}</select></label>
    <label class="check-row"><input name="ignoreCert" type="checkbox" ${vm?.ignoreCert ?? true ? "checked" : ""} /> Trust remote certificate</label>
    <label>Launch URL<input name="guacamoleLaunchUrl" value="${escapeHtml(vm?.guacamoleLaunchUrl === "/guacamole/" ? "" : vm?.guacamoleLaunchUrl ?? "")}" /></label>
  `;
}

function vmPayload(form: FormData): Record<string, FormDataEntryValue | boolean | null> {
  return {
    name: form.get("name") ?? "",
    host: form.get("host") ?? "",
    protocol: "rdp",
    status: form.get("status") || "manual-ready",
    guacamoleConnectionId: form.get("guacamoleConnectionId") ?? "",
    guacamoleLaunchUrl: form.get("guacamoleLaunchUrl") || null,
    rdpUsername: form.get("rdpUsername") || null,
    rdpPassword: form.get("rdpPassword") || null,
    rdpDomain: form.get("rdpDomain") || null,
    security: form.get("security") || "any",
    ignoreCert: form.get("ignoreCert") === "on",
  };
}

function renderAuthShell(content: string): void {
  root.innerHTML = `
    <main class="shell">
      <section class="brand-panel">
        <p class="eyebrow">Clahan Labs</p>
        <h1>Workspace Gateway</h1>
        <p class="lede">Sign in and open your assigned cloud workspace in a secure browser session.</p>
      </section>
      <section class="work-panel">${content}</section>
    </main>
  `;
}

function renderDashboardShell(content: string): void {
  root.innerHTML = `
    <main class="dashboard-shell">
      ${content}
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
  renderAuthShell(`
    <form id="setup-form" class="login-form">
      <div>
        <h2>Setup admin</h2>
        <p class="muted">Create the first administrator account for your workspace.</p>
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
  renderAuthShell(`
    <form id="login-form" class="login-form">
      <div>
        <h2>Sign in</h2>
        <p class="muted">Access your assigned cloud workspace.</p>
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
  renderDashboardShell(`
    <div class="dashboard">
      <div class="dashboard-topbar">
        <div>
          <p class="eyebrow">Clahan Labs</p>
          <h2>Workspace dashboard</h2>
          <p class="muted">Signed in as ${escapeHtml(session.username)}</p>
        </div>
        <div class="actions compact-actions">
          ${session.isAdmin ? `<button id="admin-button" class="secondary-button" type="button">Admin</button>` : ""}
          <button id="logout-button" class="secondary-button" type="button">Logout</button>
        </div>
      </div>

      <section class="metrics-row">
        <div><p class="eyebrow">Role</p><strong>${session.isAdmin ? "Admin" : "User"}</strong></div>
        <div><p class="eyebrow">Max session</p><strong>${formatTime(session.expiresAt)}</strong></div>
        <div><p class="eyebrow">Idle timeout</p><strong>${formatTime(session.idleExpiresAt)}</strong></div>
      </section>

      <section class="primary-panel">
        <div>
          <p class="eyebrow">${vm ? "Assigned workspace" : "No workspace assigned"}</p>
          <h3>${escapeHtml(vm?.name ?? "Waiting for assignment")}</h3>
          <p class="muted">${vm ? "Your workspace is ready to open." : "Your account does not have a workspace assigned yet."}</p>
        </div>

        <dl class="detail-grid">
          <div><dt>Host</dt><dd>${escapeHtml(vm?.host ?? "-")}</dd></div>
          <div><dt>Access type</dt><dd>${escapeHtml(displayProtocol(vm?.protocol))}</dd></div>
          <div><dt>Status</dt><dd>${escapeHtml(vm?.status ?? "-")}</dd></div>
          <div><dt>Connection</dt><dd>${escapeHtml(vm?.guacamoleConnectionId ?? "-")}</dd></div>
        </dl>

        <div class="actions">
          ${vm ? `<button id="launch-button" type="button">Open Workspace</button>` : ""}
          <button id="refresh-button" class="secondary-button" type="button">Refresh</button>
        </div>
      </section>
    </div>
  `);

  document.querySelector<HTMLButtonElement>("#admin-button")?.addEventListener("click", () => {
    void renderAdmin();
  });
  document.querySelector<HTMLButtonElement>("#refresh-button")?.addEventListener("click", boot);
  document.querySelector<HTMLButtonElement>("#launch-button")?.addEventListener("click", async () => {
    try {
      const launch = await api<GuacamoleLaunchResponse>("/api/session/guacamole-launch", { method: "POST" });
      window.open(launch.launchUrl, "_blank", "noreferrer");
    } catch (error) {
      renderSession({
        ...session,
        vm: session.vm,
      });
      window.alert(error instanceof Error ? error.message : "Unable to open workspace.");
    }
  });
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
      `<option value="">No workspace</option>`,
      ...vms.map((vm) => `<option value="${escapeHtml(vm.id)}">${escapeHtml(vm.name)} (${escapeHtml(vm.host)})</option>`),
    ].join("");
    const userOptions = users
      .map((user) => `<option value="${escapeHtml(user.username)}">${escapeHtml(user.username)}</option>`)
      .join("");

    renderDashboardShell(`
      <div class="admin-panel">
        <div class="session-header">
          <div>
            <p class="eyebrow">Clahan Labs</p>
            <h2>Admin dashboard</h2>
            <p class="muted">Manage users and workspace assignments.</p>
          </div>
          <div class="actions compact-actions">
            <button id="vm-page-button" class="secondary-button" type="button">Workspaces</button>
            <button id="back-button" class="secondary-button" type="button">Workspace</button>
            <button id="admin-logout-button" class="secondary-button" type="button">Logout</button>
          </div>
        </div>

        ${message ? `<p class="notice">${escapeHtml(message)}</p>` : ""}

        <form id="create-user-form" class="admin-form">
          <h3>Create user</h3>
          <label>Username<input name="username" required /></label>
          <label>Password<input name="password" type="password" minlength="8" required /></label>
          <label>Workspace<select name="vmId">${vmOptions}</select></label>
          <label class="check-row"><input name="isAdmin" type="checkbox" /> Admin user</label>
          <button type="submit">Create user</button>
        </form>

        <form id="reset-password-form" class="admin-form">
          <h3>Reset password</h3>
          <label>User<select name="username" required>${userOptions}</select></label>
          <label>New password<input name="password" type="password" minlength="8" required /></label>
          <button type="submit">Reset password</button>
        </form>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>User</th>
                <th>Role</th>
                <th>Assigned workspace</th>
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
        <section class="primary-panel">
          <div class="section-heading">
            <div>
            <p class="eyebrow">Workspace registry</p>
            <h3>${vms.length} registered workspace${vms.length === 1 ? "" : "s"}</h3>
            </div>
            <button id="vm-page-inline-button" class="secondary-button" type="button">Open workspaces</button>
          </div>
        </section>
      </div>
    `);

    document.querySelector<HTMLButtonElement>("#back-button")?.addEventListener("click", boot);
    document.querySelector<HTMLButtonElement>("#vm-page-button")?.addEventListener("click", () => {
      void renderVmRegistry();
    });
    document.querySelector<HTMLButtonElement>("#vm-page-inline-button")?.addEventListener("click", () => {
      void renderVmRegistry();
    });
    document.querySelector<HTMLButtonElement>("#admin-logout-button")?.addEventListener("click", async () => {
      await api("/api/auth/logout", { method: "POST" });
      renderLogin();
    });
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
        await renderAdmin("User created.");
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
        await renderAdmin("Password reset.");
      } catch (error) {
        await renderAdmin(error instanceof Error ? error.message : "Password reset failed.");
      }
    });

  } catch (error) {
    renderLogin(error instanceof Error ? error.message : "Admin access failed");
  }
}

async function renderVmRegistry(message = ""): Promise<void> {
  startHeartbeat();
  try {
    const vms = await api<VmSummary[]>("/api/admin/vms");

    renderDashboardShell(`
      <div class="admin-panel">
        <div class="session-header">
          <div>
            <p class="eyebrow">Admin dashboard</p>
            <h2>Workspaces</h2>
            <p class="muted">Register and maintain cloud workspace connection settings.</p>
          </div>
          <div class="actions compact-actions">
            <button id="users-page-button" class="secondary-button" type="button">Users</button>
            <button id="workspace-button" class="secondary-button" type="button">Workspace</button>
            <button id="vm-logout-button" class="secondary-button" type="button">Logout</button>
          </div>
        </div>

        ${message ? `<p class="notice">${escapeHtml(message)}</p>` : ""}

        <form id="create-vm-form" class="admin-form">
          <h3>Register workspace</h3>
          <label>Workspace ID<input name="id" required /></label>
          ${vmFormFields()}
          <button type="submit">Register workspace</button>
        </form>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Workspace</th>
                <th>Host</th>
                <th>Remote User</th>
                <th>Security</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              ${
                vms.length
                  ? vms
                      .map(
                        (vm) => `
                          <tr>
                            <td>${escapeHtml(vm.name)}<br /><span class="muted small-text">${escapeHtml(vm.id)}</span></td>
                            <td>${escapeHtml(vm.host)}</td>
                            <td>${escapeHtml(vm.rdpUsername ?? "-")}</td>
                            <td>${escapeHtml(displaySecurity(vm.security))}</td>
                            <td>${escapeHtml(vm.status)}</td>
                          </tr>
                        `,
                      )
                      .join("")
                  : `<tr><td colspan="5">No workspaces registered yet.</td></tr>`
              }
            </tbody>
          </table>
        </div>

        <div class="vm-edit-list">
          ${vms
            .map(
              (vm) => `
                <form class="admin-form edit-vm-form" data-vm-id="${escapeHtml(vm.id)}">
                  <h3>Edit ${escapeHtml(vm.name)}</h3>
                  ${vmFormFields(vm)}
                  <button type="submit">Save workspace</button>
                </form>
              `,
            )
            .join("")}
        </div>
      </div>
    `);

    document.querySelector<HTMLButtonElement>("#users-page-button")?.addEventListener("click", () => {
      void renderAdmin();
    });
    document.querySelector<HTMLButtonElement>("#workspace-button")?.addEventListener("click", boot);
    document.querySelector<HTMLButtonElement>("#vm-logout-button")?.addEventListener("click", async () => {
      await api("/api/auth/logout", { method: "POST" });
      renderLogin();
    });

    document.querySelector<HTMLFormElement>("#create-vm-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget as HTMLFormElement);
      try {
        await api("/api/admin/vms", {
          method: "POST",
          body: JSON.stringify({
            id: form.get("id"),
            ...vmPayload(form),
          }),
        });
        await renderVmRegistry("Workspace registered.");
      } catch (error) {
        await renderVmRegistry(error instanceof Error ? error.message : "Workspace registration failed.");
      }
    });

    document.querySelectorAll<HTMLFormElement>(".edit-vm-form").forEach((formElement) => {
      formElement.addEventListener("submit", async (event) => {
        event.preventDefault();
        const currentForm = event.currentTarget as HTMLFormElement;
        const vmId = currentForm.dataset.vmId ?? "";
        const form = new FormData(currentForm);
        try {
          await api(`/api/admin/vms/${encodeURIComponent(vmId)}`, {
            method: "PUT",
            body: JSON.stringify(vmPayload(form)),
          });
          await renderVmRegistry("Workspace configuration updated.");
        } catch (error) {
          await renderVmRegistry(error instanceof Error ? error.message : "Workspace update failed.");
        }
      });
    });
  } catch (error) {
    renderLogin(error instanceof Error ? error.message : "Workspace registry access failed");
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
