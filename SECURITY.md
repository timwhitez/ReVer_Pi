# Security

## Reporting

Open a private security advisory on the repository, or contact the maintainer directly. Do not file
a public issue for anything that could expose credentials, private endpoints or user data.

## Design boundaries

- **Not a sandbox.** Tool calls execute with the privileges of the Pi process. Run untrusted code in
  a container or VM of your own.
- **Loopback by default.** The gateway binds to `127.0.0.1`; `--allow-network` is required for other
  addresses and the operator owns the isolation.
- **Capability tokens, not keys.** The gateway authenticates Pi with a per-session token, stored
  hashed in `sessions.sqlite`. Provider credentials live only in the environment.
- **Single-use sessions.** A session with recorded attempts cannot be replayed; this prevents double
  billing and silent reruns of an already-paid request.
- **Opt-in execution.** Revalidation runs only when the operator pins a registry, a runner and their
  SHA-256 digests; nothing lets the model choose a command to execute.
- **Local artifacts.** Run directories contain wire traces, ledgers and archives. Treat them as
  sensitive: they may hold task content and provider responses. They are git-ignored.

## Known limitations

- The research harness's offline guard is a defense against accidental egress by cooperating
  programs, not a hostile-code sandbox.
- Archive handles are content digests, which prove which bytes were stored; they do not prove the
  bytes were true, current, or produced by a trusted party.
- Local files are not encrypted at rest.
