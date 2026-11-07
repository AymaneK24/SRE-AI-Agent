# Concepts

These words have a specific meaning in the console.

## Connections

A **connection** is something the console reads from. There are three kinds:

| Kind | Examples | How many per installation |
|---|---|---|
| Metric source | Prometheus (also Amazon, Azure, and Google managed), Amazon CloudWatch, Azure Monitor | As many as you need |
| Log store | Loki | One |
| Kubernetes cluster | Any cluster with an API server, including AKS, EKS, GKE, and Docker Desktop | One |

Every connection is optional, and any single one is enough to run the console. See [Connections](/docs/connections) for what each one adds.

## Live context

Every poll (every 15 seconds by default), the context engine reads every connection and builds the **live context**: firing alerts, targets that are down, Kubernetes problems, failing rules, golden signals, recent changes, and log error counts. The overview shows it, and it's what the AI model receives first when you investigate.

## Problems

A **problem** is something wrong right now, such as a firing alert, a target that stopped answering, or a crash-looping Deployment. Problems come from every connection and are sorted worst first.

Kubernetes problems are counted per workload, not per pod. A Deployment with five crash-looping pods is one problem. A Kubernetes problem clears 75 seconds after the cluster stops showing it, so a pod that is briefly running between restarts doesn't flap the problem on and off.

For known Kubernetes failures, the console also writes a **diagnosis** in code, without AI: an error on start, out of memory, a liveness probe kill, a missing Secret or ConfigMap key, an image that can't be pulled, a pod that can't be scheduled, a stuck rollout, or node trouble.

## Events

When a problem starts or clears, the console records an **event**. Events drive alert messages, automatic investigations, and project statistics. Alertmanager alerts and deployments sent by webhook are recorded as events too.

## Incidents

Problems that start close together are usually one outage. The console groups problems that start within 10 minutes of each other, or during an incident's first hour, into one **incident**. Projects calculate the average time to clear, the time between incidents, and the share of time with no incident from these.

## Investigations

An **investigation** is a conversation with the AI model about your systems. It's private to the person who started it until they share it. Automatic investigations of critical alerts are visible to the whole team. See [Investigations](/docs/investigations).

## Projects

A **project** groups the metric sources and Kubernetes namespaces behind one system, such as "Shop" or "Payments", and shows its health, recurring issues, patterns, and reports. See [Projects and reports](/docs/projects-and-reports).

## Runbooks

A **runbook** (shown as Workflows in the menu) is a written fix: steps and commands for a problem that comes back. Engineers write them, admins review them, and everyone can read reviewed ones. The console never runs them. See [Runbooks](/docs/runbooks).

## Roles

Everyone has one of four roles: **viewer**, **engineer**, **admin**, or **owner**. The server checks the role on every request. See [Roles and permissions](/docs/roles-and-permissions).
