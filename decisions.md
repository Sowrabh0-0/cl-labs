# Decisions

Date: 2026-06-17

## Goal

Build a lightweight web application where a user logs in, is mapped to a specific Azure VM, and then opens a stable browser-based remote session to that VM with low latency and minimal interruption.

Current known machines:

- Guacamole VM: `135.235.218.233`
- Test client VM: `4.186.72.63`

Current architecture reference: `image.png`

## Recommended Direction

Use Apache Guacamole for the remote desktop session path, and use FastAPI as the application control plane.

This keeps the latency-sensitive stream out of FastAPI. FastAPI should authenticate users, decide which VM they are allowed to use, create or resolve the VM connection metadata, and return a session page. Guacamole and `guacd` should handle the actual RDP/VNC/SSH traffic.

## High-Level Architecture

1. Browser opens the application over HTTPS.
2. Nginx terminates SSL and proxies requests.
3. Lightweight UI serves two primary pages:
   - Login / VM resolution page.
   - Active VM session page.
4. FastAPI handles:
   - User authentication.
   - User-to-VM mapping.
   - Session records.
   - Guacamole connection lookup or provisioning.
5. Apache Guacamole handles:
   - Browser remote desktop UI.
   - WebSocket or HTTP tunnel.
   - Session continuity while the browser remains connected.
6. `guacd` bridges Guacamole to target VMs over RDP/xRDP, VNC, or SSH.
7. Azure VMs should eventually sit in a private subnet, reachable from the Guacamole VM but not exposed publicly.

## UI Stack Decision

Use a lightweight frontend:

- MVP option: TypeScript with plain browser APIs and a static build.
- If richer UI is needed: add React or Preact later.

Recommendation for the first version: TypeScript without a bundler.

Reasoning:

- The app only needs login, VM selection/status, and session launch.
- It keeps the frontend lightweight while still giving type safety.
- It separates the UI from the FastAPI backend cleanly.
- It is enough for polling VM status, showing reconnect state, and launching the active session page.
- It avoids Vite/esbuild process-spawn failures on restricted company laptops.

Move to React/Preact only if the session dashboard becomes highly interactive.

## Backend Stack Decision

Use FastAPI for the control plane.

Core backend responsibilities:

- Authenticate username/password.
- Store users, roles, VM mappings, and active session records.
- Resolve which VM belongs to the logged-in user.
- Create or update Guacamole connection records.
- Generate a launch URL or embedded session target.
- Track session heartbeat and audit events.

FastAPI should not proxy the remote desktop stream.

## Session Design

Use two application pages:

1. `/login`
   - User enters username and password.
   - FastAPI validates credentials.
   - FastAPI resolves the assigned VM.
   - FastAPI creates an app session using a secure HTTP-only cookie.

2. `/session`
   - Shows the assigned VM and connection state.
   - Launches or embeds the Guacamole session.
   - Provides reconnect behavior if the browser reloads.

Session records should include:

- `user_id`
- `vm_id`
- `guacamole_connection_id`
- `session_status`
- `created_at`
- `last_seen_at`
- `expires_at`
- `disconnect_reason`

For the MVP, one user should map to one VM at a time.

Confirmed session policy:

- Maximum session duration: 4 hours.
- Idle timeout: 15 minutes.
- Reconnect is allowed only while the session has not crossed the idle timeout.

## VM Mapping Decision

Create an internal mapping table rather than hardcoding machines in code.

Suggested tables:

- `users`
- `vms`
- `user_vm_assignments`
- `sessions`

The `vms` table should store:

- VM name.
- Private IP or DNS name.
- Protocol: RDP initially.
- Port: usually `3389` for xRDP.
- Guacamole connection identifier.
- Optional RDP username/domain metadata for Guacamole connection provisioning.
- Current state: available, assigned, offline, maintenance.

For now, manually create VMs in Azure and register them in the database. Admin users can create application users and map each user to a fixed VM. Later, add Azure SDK automation to provision, tag, start, stop, and assign VMs dynamically.

Application-level VM mapping rule:

- One non-admin user per VM.
- Admin users may inspect/manage connections.
- App user creation and password reset should sync matching Guacamole users and permissions when Guacamole DB sync is enabled.
- App credentials and VM/RDP credentials are separate. App credentials authenticate to the application and matching Guacamole user. VM/RDP credentials are stored as Guacamole connection parameters and authenticate to the target VM.

## Guacamole Integration Decision

Use Guacamole as the primary remote access gateway.

Two possible integration paths:

1. Guacamole database integration.
   - FastAPI writes users, connections, and permissions into the Guacamole database.
   - Guacamole handles login and connection authorization.
   - Good for MVP if using Guacamole's MySQL/PostgreSQL auth extension.

2. Custom token or extension integration.
   - FastAPI authenticates the user and passes Guacamole a signed token.
   - Guacamole extension validates the token and exposes only the allowed connection.
   - Better long-term experience, but more custom work.

Recommendation:

- MVP: use Guacamole's database auth extension and let FastAPI manage mapping records.
- Current next step: use Guacamole's encrypted JSON authentication extension for normal user launch so the app authenticates the user and Guacamole receives a short-lived signed/encrypted connection definition.
- Later: consider a custom Guacamole auth extension if deeper lifecycle control is needed.

For normal users, the app should launch `/guacamole/?data=...` with a JSON auth token generated by FastAPI. The token should include only the VM mapped to the logged-in user. Do not synthesize `/#/client/{vm_name}` URLs from raw VM names; Guacamole client identifiers require Guacamole-specific encoding/session context.

## Low-Latency Decisions

To keep sessions responsive:

- Keep Guacamole VM and client VMs in the same Azure region and VNet.
- Put target VMs in a private subnet.
- Avoid routing RDP through FastAPI.
- Enable WebSocket support through Nginx.
- Increase Nginx proxy read/send timeouts for long sessions.
- Disable proxy buffering for Guacamole tunnel routes.
- Use VM sizes with enough CPU for desktop streaming.
- Prefer xRDP with a lightweight Linux desktop environment.
- Avoid scaling active sessions across multiple `guacd` instances until sticky routing is designed.

Important: active Guacamole sessions are stateful. If `guacd` restarts, the active remote session will usually drop. For early versions, run a single stable Guacamole/`guacd` host and prioritize reliability.

## Reliability Decisions

MVP reliability:

- One Guacamole server.
- One `guacd` daemon.
- One FastAPI app instance.
- One database.
- Nginx in front.
- Health checks for FastAPI, Guacamole, `guacd`, and target VMs.

Later reliability:

- External PostgreSQL or MySQL.
- Redis for FastAPI session/cache state if scaling API workers.
- Sticky routing for Guacamole/`guacd` if adding more gateway nodes.
- VM lifecycle automation through Azure SDK.
- Session audit logs and admin controls.

## Security Decisions

Minimum security baseline:

- Use HTTPS only.
- Store app sessions in secure HTTP-only cookies.
- Hash passwords with Argon2 or bcrypt.
- Do not expose client VMs directly to the public internet.
- Restrict RDP/SSH to the Guacamole VM or private subnet.
- Store VM credentials securely, not in source files.
- Rotate credentials used by Guacamole.
- Log login, session start, reconnect, disconnect, and failed auth events.
- For the MVP, Guacamole connection passwords may be written into the Guacamole database because Guacamole needs them for unattended RDP connection. Move this to Azure Key Vault or a dedicated secret manager before production.

For production:

- Prefer SSO/OIDC over local username/password.
- Use Azure Key Vault for VM credentials.
- Use Azure Network Security Groups to restrict VM access.
- Add admin approval and audit trails for VM assignment changes.

## MVP Build Plan

1. Confirm Guacamole can manually connect to the test VM.
2. Add a FastAPI app with:
   - Login route.
   - Session route.
   - User-to-VM lookup.
   - Secure cookie session.
3. Add a small database schema for users, VMs, assignments, and sessions.
4. Use Guacamole database auth extension or pre-created Guacamole connections.
5. On login, route the user to their assigned VM session.
6. Add Nginx reverse proxy config for:
   - FastAPI.
   - Guacamole.
   - WebSocket/tunnel timeouts.
7. Add reconnect behavior on the session page.
8. Add health checks and basic logs.

## Open Questions

- Should users have one permanent VM or should VMs be assigned from a pool?
- Should the user see a VM selection page, or should login immediately open the assigned VM?
- Are VM credentials unique per user, or shared per VM through Guacamole?
- Is this for internal users only, or public users on the internet?
- What maximum session duration is required?
- Should disconnected sessions be preserved, terminated, or reusable for a period?

## Immediate Next Step

Start with a thin MVP:

- TypeScript static frontend.
- PostgreSQL or SQLite for local development.
- Guacamole database auth.
- Manual VM registration.
- One assigned VM per user.

Once that works end to end, automate VM lifecycle and improve seamless Guacamole authentication.

## Deployment Decision

Use Docker Compose on the Azure VM for the MVP:

- `frontend`: Nginx serves the compiled TypeScript app and proxies `/api` to FastAPI.
- `backend`: FastAPI app with SQLite-backed users, sessions, and VM mappings.
- `backend-data`: named volume for persistent SQLite data.

This keeps deployment simple while preserving the option to move the database to PostgreSQL later.

Guacamole should now run inside Docker Compose using official, version-matched Guacamole containers. Host Tomcat 10 caused servlet/API compatibility problems with the current Guacamole WAR/extensions. Host Nginx terminates TLS and routes `/` to the app on `127.0.0.1:8088`, while `/guacamole/` maps to Docker Guacamole on `127.0.0.1:8090/guacamole/`.
