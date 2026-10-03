# Security

## Webhook URLs are secrets

Anyone with a Discord webhook URL can post to that channel. In this project:

- All webhook URLs live in the encrypted `DISCORD_WEBHOOKS` Actions secret, never in the repository.
- The bot masks every URL in workflow logs and never includes URLs in error messages.
- `webhooks.json` is in `.gitignore`.
- Pull requests from forks run CI only, which has no access to secrets.

If a webhook URL leaks, delete that webhook in Discord, create a new one and update the secret.

## Parsing untrusted content

Feeds and web pages are untrusted input. The bot strips XML DOCTYPE declarations before parsing (preventing entity-expansion and external-entity attacks), caps every response at 8 MB, applies timeouts to every request, and escapes markdown and mentions in every title it posts.

## Reporting a vulnerability

Please open a private security advisory on the repository rather than a public issue.
