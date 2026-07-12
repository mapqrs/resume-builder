# Production deployment

Punch list, decision matrix, and starter stack for taking this tool from
**local-only** (current state) to **hosted** (multi-user, internet-facing).

The localhost binding is deliberate. Phase 9's privacy guarantees ("your
data stays on this machine") depend on the fact that everything runs in
loopback. Going hosted means inheriting a different threat model and a
different cost model. Nothing in this doc is wired today — it's a plan
for when we decide hosting is the next phase.

## How the threat & cost model changes

| Concern | Local-only (today) | Hosted |
|---|---|---|
| LLM call origin | User's own machine, with their `claude` CLI or `ANTHROPIC_API_KEY` | Your server, with **your** API key |
| Cost | User pays Anthropic directly (subscription or API) | You pay Anthropic, must recover from users (subscription, credits, pay-per-use) |
| Rate limiting | None needed | Per-user + global; back-pressure when Anthropic itself rate-limits |
| Data storage | User's disk (`sessions/`, `master.yaml`) | Your servers; you become a data controller under DPDPA/GDPR |
| Encryption at rest | OS-level only | Disk + optional column-level on resume content |
| Auth | None — it's their machine | Required; per-account sessions |
| Transport security | Loopback (`127.0.0.1`) | HTTPS, CORS, CSRF, CSP |
| Real "Delete my data" | `rm -rf sessions/` | Must purge DB rows, hard-delete columns, comply with 30-day GDPR/DPDPA timeline |
| Telemetry | None | Aggregates-only opt-in (never prompt content, never bullet content) |
| Privacy statement | "Stays on your machine" — literally true | Nuanced — prompts pass through your infra; you commit to a no-content-logging policy |

## The decisions you have to make first

Before any code: nail these.

### 1. Billing model

| Model | What it is | When to pick |
|---|---|---|
| Free + your-key-only | Tool is free; users bring their own `ANTHROPIC_API_KEY` | Smallest blast radius; preserves today's "no shared billing" property; the hosted version is just a UI for users who don't want to clone the repo |
| Pre-paid credits | $5 buys ~200 generations; refill in-app via Stripe | Mainstream consumers; predictable margin; cap abuse via credit balance |
| Subscription | ₹399/month for unlimited generations | Power users who tailor weekly; ARPU known; rate-limit per minute to prevent abuse |
| Pay-per-use post-paid | Card on file; bill at end of month | B2B / recruiting firms; risky for consumer (bill shock) |

**Recommended for v0:** free-with-bring-your-own-key. It's the smallest
change from today's privacy story and the most defensible "we don't see
your data" claim. Add credits/subscription only if a real user base
demands it.

### 2. Prompt-cache policy

| Policy | Privacy implication | Cost implication |
|---|---|---|
| No caching | Your server forwards prompts to Anthropic and forgets them. Logs strip prompt content. | 100% of Anthropic list price |
| Anthropic prompt caching | Use Claude's native prompt-cache (5-min TTL, ephemeral). Master content is sent each request but cached server-side at Anthropic. | 50-90% cheaper on repeated tailors with the same master |
| Cache responses in your DB | Cache `(master_hash + jd_hash + pointers_hash) → response` in your Postgres. | Cheapest, but you now store the user's resume; privacy statement must reflect this |

**Recommended for v0:** Anthropic prompt caching. Cheaper than no-cache,
and Anthropic's cache is ephemeral (5-min TTL) so the privacy story
holds: you don't durably store prompt content; Anthropic does for 5 min
and then forgets.

### 3. Data residency

If you have any Indian users at all, DPDPA (effective from late 2025)
requires you to know where their data sits. Anthropic API endpoints are
in the US. For Indian-user data, you need to decide:

- **Region pin** — host on AWS Mumbai / GCP Mumbai / Azure Pune, but
  the LLM call still leaves India to reach Anthropic. Disclose this.
- **Best-effort statement** — "Data is processed in India except for
  the LLM call itself; that goes to Anthropic in the US."

Either way: be explicit in the privacy policy. DPDPA expects clarity.

## The hosted-deploy punch list

In implementation order — each step depends on the ones above it.

### Phase A — Account system

- [ ] OAuth login (Google + GitHub). Skip email/password until you have
      a real reason to support it.
- [ ] User model: id, email, oauth_provider, created_at, deleted_at.
- [ ] Sessions become per-account. Migrate `session_store.py`'s YAML
      sessions to Postgres-backed rows keyed by `user_id + session_id`.
- [ ] Flask-Login or starlette-equivalent for the session cookies.
- [ ] Logout button.

### Phase B — Database

- [ ] Postgres (managed: AWS RDS, GCP Cloud SQL, Render Postgres,
      Supabase). 1 GB instance is enough for v0.
- [ ] Migration tool — Alembic or Atlas.
- [ ] Tables: `users`, `wizard_sessions`, `masters`, `generations`,
      `usage_log`.
- [ ] Connection pool — `psycopg2[binary]` + SQLAlchemy or `psycopg3`.
- [ ] Read replicas not needed until >1000 active users.

### Phase C — Real "Delete my data"

- [ ] `/api/delete-my-data` runs a hard delete: rows gone, audit log
      retains only `{user_id, deleted_at}`.
- [ ] 30-day retention exception only for the audit log line (GDPR
      requires you can prove you deleted on the date you say you did).
- [ ] User can also delete their account entirely (separate button,
      stronger confirmation). Account deletion cascades to every row.

### Phase D — Transport security

- [ ] HTTPS via Let's Encrypt + Cloudflare or platform-native (Fly.io
      / Render do this automatically).
- [ ] CSRF tokens on every POST route. Flask-WTF if you want a battery
      or roll your own.
- [ ] CORS origin allowlist (just your own domain).
- [ ] CSP header — `default-src 'self'; script-src 'self' 'unsafe-inline'`
      (drop `unsafe-inline` after auditing the inline scripts).
- [ ] HSTS header.
- [ ] Cookie flags: `Secure`, `HttpOnly`, `SameSite=Lax`.

### Phase E — Rate limiting

- [ ] Per-user: 5 generations/min, 50/hour, 200/day. Tunable.
- [ ] Per-IP for the auth-less endpoints (`/api/analyze-jd`,
      `/api/analyze-target-role`): 30 calls/min.
- [ ] Global: bail out at 80% of your Anthropic per-minute quota to
      give burst headroom for paying users.
- [ ] `Flask-Limiter` with Redis backend, or platform-native (Cloudflare
      rate limits for the cheap path).

### Phase F — Cost transparency UI

- [ ] Per-call cost shown inline: "Last generation: $0.04 · 3,200
      tokens".
- [ ] Running total this month: "Spent $1.20 of $5 credits".
- [ ] Warning at 80% credit balance.
- [ ] Stripe Checkout for top-ups if you go credit-based.

### Phase G — Telemetry (aggregates only)

- [ ] PostHog or self-hosted equivalent.
- [ ] Event shape: `{event_name, anon_id_or_user_id, properties:
      {role_family, lint_rule_id, guard_warning_count, phase_name}}`.
      NEVER: `bullet_text`, `master_yaml`, `jd_text`, or anything that
      could leak the user's resume.
- [ ] Funnel: wizard_started → role_picked → first_chunk_extracted →
      first_draft_categorized → first_polish → master_saved → first
      tailor → first .docx download.
- [ ] Drop-off analysis: where do users abandon the wizard?
- [ ] Opt-in only — default OFF.

### Phase H — Mobile + a11y

- [ ] Responsive `templates/index.html` (currently desktop-only).
- [ ] Keyboard navigation, focus rings.
- [ ] Screen-reader labels (`aria-label`, `aria-describedby` on every
      input).
- [ ] Color contrast audit — WCAG AA at minimum.
- [ ] Reduced-motion respect (`prefers-reduced-motion`).

### Phase I — Internationalization

Only when you have a second locale to ship:
- [ ] Extract strings to message catalogs (`flask-babel`).
- [ ] Hindi / Tamil / Bengali / Marathi UI strings as separate catalogs.
- [ ] Devanagari font support in the PDF render path (the current docx
      template uses Calibri — fine for English; needs an Indic-script
      font for non-Latin scripts).
- [ ] India-tuned JD vocabulary tables (already partial — extend).

### Phase J — Observability

- [ ] Sentry (or self-hosted GlitchTip) for error tracking. **Filter
      PII before sending** — strip prompt content from breadcrumbs.
- [ ] Structured logs — JSON to stdout, scraped by platform.
- [ ] Health endpoint: `/healthz` already exists; add a `/readyz` that
      checks DB connectivity.
- [ ] Uptime monitoring: UptimeRobot (free for 50 monitors) or
      Better Stack.

## Starter stack

A v0 hosted deployment that fits one person's evenings-and-weekends:

| Slot | Choice | Why |
|---|---|---|
| Hosting | Fly.io or Render | HTTPS automatic, Postgres add-on, free tier covers initial load |
| Database | Render Postgres (free tier) | Managed, automatic backups, 256MB ≈ 50K resumes worth |
| Auth | Auth.js / NextAuth pattern via Authlib (Flask) | OAuth done; don't roll your own |
| Telemetry | PostHog (self-hosted on the same Fly.io instance) | Aggregates only, EU-hosted option |
| Billing | Stripe Checkout + Customer Portal | If you go credit-based; otherwise skip |
| Error tracking | Sentry (free tier: 5K events/mo) | PII-scrubbing recipe known |
| Background jobs | `rq` + Redis | If LLM calls move off-request; not needed at v0 |
| CDN | Cloudflare (free tier) | Caches static assets, DDoS protection |
| CI/CD | GitHub Actions | Free for public repos, $4/mo for private |
| Secrets | Platform-native (Fly secrets / Render env) | Don't run a separate vault until you must |

## Cost model — back-of-envelope

100 active users, 5 tailors/user/month:

- Anthropic: 500 tailors × ~3K input tokens × ~1K output tokens ×
  Sonnet pricing ≈ **$15/mo**.
- With Anthropic prompt caching: ~$3-5/mo.
- Hosting (Fly.io 2x shared CPU, 1GB RAM): **$5/mo**.
- Postgres (Render free → starter): **$0-7/mo**.
- Sentry: **$0** (free tier covers).
- PostHog (self-hosted on same Fly): **$0**.
- **Total: ~$10-30/mo** for a 100-user, low-engagement service.

If you charge ₹199 (~$2.40) per user-month with 30% conversion to paid,
breakeven is around ~15 paying users. The unit economics are easy as
long as you keep ops simple.

## The "should we host?" decision

The honest answer: hosting only makes sense if you have **real users
asking for it**. The local tool is more secure, more private, faster,
and cheaper. It's a strictly better experience for the technical user.

Host when:

- Non-technical users repeatedly ask for "a link they can use" instead
  of cloning a repo.
- You want to charge.
- You want feedback signals (telemetry) that the local tool can't give.

Don't host because:

- It's a fun engineering project. (Cost: 2-4 weeks of focused work plus
  ongoing maintenance.)
- "Everyone hosts these days." (No, they don't, and the privacy story
  here is actually a competitive advantage.)

## When you do decide to host

Read this doc top-to-bottom, make the three decisions above (billing /
prompt-cache / data residency), and then walk Phase A → J in order.
Each phase is 2-5 days of work for one person.

Tests come along for the ride: existing 447 tests are platform-agnostic
and run unchanged against a Postgres-backed `session_store`. Add
auth-required-route tests as Phase A lands.

This doc lives alongside the code so the decisions stay in sync with
what's built. Update it as choices solidify.
