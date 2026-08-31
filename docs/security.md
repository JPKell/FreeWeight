# Security

FreeWeight is local-first. The default installation binds to loopback, requires no credentials,
sends nothing off the machine, and keeps every byte of your content local. Everything that changes
any of those facts is explicit, documented, and visible in the UI. This guide is the operator's
view; the full standard is [Security Standards](standards/security-standards.md).

## The default is safe and needs no configuration

* **Loopback only.** The server binds `127.0.0.1`. Nothing on your network can reach it.
* **No credentials.** On loopback, the OS user boundary is the security boundary (ADR-0014).
* **No egress.** No telemetry, analytics, update checks, CDN or font fetches. All assets are
  vendored; the UI works offline. Remote model providers are off by default and require two
  separate opt-ins, and every such provider is badged as egress in the UI.

## DNS-rebinding and CSRF are closed by default

* Every request's `Host` header is validated before routing, so a web page that rebinds DNS to
  `127.0.0.1` cannot reach the service (a mismatch is `421`). This runs before everything else.
* Every HTML form carries a double-submit CSRF token (a `__Host-` cookie plus a hidden field). The
  JSON API is exempt on stated grounds — a cross-origin form cannot produce a JSON body, and CORS
  is off. See [ADR-0026](adr/0026-local-http-hardening.md).

> **A note on plain HTTP.** The CSRF cookie is `__Host-`-prefixed and `Secure`. Browsers treat
> `http://localhost` and `http://127.0.0.1` as secure contexts, so the cookie works on the default
> loopback bind. It does **not** work over plain HTTP reached through any other hostname — reach the
> UI as `localhost`, or terminate TLS in front of a LAN deployment.

## Exposing to a network is a deliberate act

Binding to anything but loopback **refuses to start** unless you set, together:

* `server.host` to the non-loopback address,
* `server.allowed_hosts` naming every hostname the service will answer to (against DNS rebinding),
* at least one `auth.tokens` bearer token,
* and, for `0.0.0.0`, `server.allow_lan_exposure = true` as an acknowledgement.

TLS is terminated by a reverse proxy in front; the application speaks HTTP. A non-loopback bind logs
a warning naming the reverse-proxy requirement.

## Untrusted data is never trusted

* **Model output is data, never code.** It is never executed, never used to build a path or a URL,
  and always rendered escaped. Code-execution benchmarks run only in a sandbox that has no network,
  no home directory, no database and no credentials — and refuse rather than run on the host when no
  sandbox tier is available.
* **External benchmark datasets and imported goal packs** are size-capped, path-containment-checked,
  schema-validated and hash-verified before a single byte is written. Archive extraction is hardened
  against traversal, links, device files and decompression bombs.
* **User-authored goal content** renders through a sandboxed template environment with no filesystem
  or network access, and user regex is guarded by a dialect lint at load time.

## Your data

* The database, artifacts and backups live under your data root with owner-only permissions
  (`0600`/`0700`). Backups are never uploaded.
* Full prompt and response text is stored only when a run or export explicitly asks for it; the
  default stores hashes and a short, capped excerpt. Your calibration grades never leave the
  machine.

## Reporting a vulnerability

See [`SECURITY.md`](../SECURITY.md) at the repository root.
