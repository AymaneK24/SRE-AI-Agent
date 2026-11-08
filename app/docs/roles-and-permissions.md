# Roles and permissions

Each person has one of four roles. The server checks the role on every request, so hiding something on a page is never the only protection.

## The four roles

| Role | Meant for | In one line |
|---|---|---|
| **Viewer** | Managers, support, other teams | Sees status and shared results |
| **Engineer** | SREs and developers on call | Investigates, runs queries, writes runbooks, makes reports |
| **Admin** | Platform owners | Also manages connections, AI, alerts, and members, and can read every investigation |
| **Owner** | One or two accountable people | Also manages security policies and single sign-on |

## What each role can do

| Area | Viewer | Engineer | Admin | Owner |
|---|---|---|---|---|
| **Overview** | Problems by workload, golden signals, saved charts. No queries, labels, addresses, logs, or diagnosis evidence | Full detail, with queries, targets, and log lines | Full | Full |
| **Investigate** | Questions and answers of shared and automatic investigations. No input box | Own private investigations, plus shared and automatic ones. Choose the model | All investigations | All |
| **Projects** | Status, incidents, issues, and shared reports | Create and edit projects, generate and share reports, record verdicts | Also delete projects | Same as admin |
| **Runbooks** | Read reviewed runbooks | Write drafts, edit, follow reviewed ones | Also review and archive | Same as admin |
| **Logs** | Hidden | Own and shared investigations, prompts sent, state changes | Also alerts sent and the audit trail | Same as admin |
| **Connections** | Hidden | Hidden. Can pick an AI model, without seeing its address or key | Full | Full |
| **Organization** | Hidden | Hidden | Usage, budget, and members | Also security policies and single sign-on |
| **Docs** | Yes | Yes | Yes | Yes |
| **Your account** | Yes | Yes | Yes | Yes |

## Why admins can read every investigation

Someone has to be able to oversee what's sent to an AI model, and the audit trail needs a person who can check it. If your organization wants this narrower, give fewer people the admin role.

## Managing people

Admins invite people in **Organization > Members** with a name, email, and role. There's no public sign-up: nobody can create an account without an invitation or single sign-on.

- An invitation link works once, for 72 hours. Without an email server, the app shows the link for you to send.
- **Change a role** at any time. The person's sessions end, so the new role applies straight away.
- **Deactivate** stops someone signing in, and keeps their history. **Reactivate** reverses it.
- **Remove** deletes the account. Their actions stay in the audit trail.
- Only an owner can make someone an owner, or change an owner.

With single sign-on, people from your allowed email domains get an account on their first sign-in, with the role set in **Role for new people** (viewer or engineer). Raise their role afterwards if needed. See [Single sign-on](/docs/single-sign-on).

## Security policies (owners)

| Policy | Default | Notes |
|---|---|---|
| Shortest password | 12 characters | 12 is the minimum allowed |
| Sign people out after | 12 hours | From 1 to 720 |
| Only invite these email domains | Any | Such as `company.com` |
| Require single sign-on for everyone | Off | Password sign-in is refused, except for the first account created at install, which stays as a break-glass account |
| Allow cloud AI models | On | Off blocks Claude and Gemini for everyone |
| Keep logs for | 365 days | The audit trail is kept |

## Least privilege, in practice

- Most people should be **viewers**. Make on-call engineers **engineers**.
- Keep **admins** to the few people who manage connections. An admin can make the server connect to internal addresses and can read every investigation.
- Have one or two **owners**, and protect their accounts with single sign-on and two-factor sign-in at your provider.
- Review **Organization > Members** every quarter, and deactivate people who left.
