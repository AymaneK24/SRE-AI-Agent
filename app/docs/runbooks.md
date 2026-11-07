# Runbooks

**Workflows** in the menu is your team's runbook library: fixes for problems that come back, written once as steps and commands, reviewed, and shared. Nobody has to rebuild the context from memory or ask the AI from scratch at 3 a.m.

> **Security:** The console can't run these steps. A person reads each one, decides, and carries it out on the servers with their own access. That's deliberate: a runbook that runs itself is a way into production.

## Who can do what

| Role | Runbooks |
|---|---|
| Viewer | Reads reviewed runbooks: steps, commands, project, and servers. Can't write or follow them |
| Engineer | Also writes drafts, edits, and follows reviewed runbooks |
| Admin, owner | Also reviews drafts and archives runbooks |

## Write a runbook

Select **New workflow**, then fill in:

| Field | What to write |
|---|---|
| Name | The problem, as people search for it: *Payment can't reach its database* |
| Summary | One line on when to use it |
| Risk | The highest risk of any step: low, medium, or high |
| Project | Optional. Links it to a [project](/docs/projects-and-reports), and lets people filter by project |
| Servers or workloads it is for | Host names, IP addresses, or workload names, separated by commas, such as `db-01, 10.20.0.15, payment`. Problems that name one of these show this runbook |
| Match alert names containing | Alert names it fixes, such as `KubePodCrashLooping` |
| Match text containing | Words that appear in the problem, such as `connection refused` |
| Steps | One action each, in order |

Each step has:

- **Title and detail:** what to do, and why.
- **Command for a person to run:** optional. Write it with placeholders, such as `kubectl -n NAMESPACE rollout undo deployment/NAME`, so nobody runs it against the wrong thing.
- **What this step can change:** *Nothing, it only checks*, low, medium, or high risk.
- **How to undo it:** required in practice for medium and high risk.
- **How to tell it worked.**

A runbook is saved as a **draft**. Any edit returns it to draft, so someone reviews the change. A run already in progress keeps its own copy of the steps.

## Review

Admins open a draft, check every step and command, then select **Mark as reviewed**. Only reviewed runbooks can be followed, and only reviewed ones are shown to viewers.

## Find the right runbook

- **Search** covers names, steps, commands, and servers.
- **Project filter** shows one project's runbooks.
- **From a problem:** **Runbooks** next to a problem on the overview opens the library filtered to that workload and kind of problem. Runbooks match when the problem names one of their servers or workloads, contains one of their words, or is the kind of problem they fix.

Seven built-in runbooks cover common problems: *Target can't be scraped*, *High CPU use*, *Memory running out*, *Disk filling up*, *Error rate is high*, *Responses are slow*, and *Containers keep restarting*. They're reviewed already, and you can edit or archive them. Their commands use placeholders, so adapt them to your systems.

## Follow a runbook

Engineers select **Start following these steps**. This records progress. It doesn't run anything.

- Steps are done in order. Each one is marked done, or skipped with a note for the team.
- A medium or high risk step asks the person to confirm *I've read the risk and how to undo this*.
- Everyone on the team sees the same progress and who did each step, so a shift can hand over halfway.
- At the end, the person records whether it worked: it helped, it partly helped, or it didn't help. The results show which runbooks actually work.

Everything is recorded in the audit trail.

## Runbooks and the AI model

Up to 4 runbooks that match the question or the active problems are included in the model's context, reviewed ones first, marked as untrusted text, so the model can point to your team's fix. The investigation page lists which runbooks were shared with the model. Answers and alert emails list the commands of matching reviewed runbooks for people to run themselves.

From an investigation, **Draft a remediation workflow** asks the model to write the fix as a draft runbook. Check every command before marking it as reviewed: models invent flags.
