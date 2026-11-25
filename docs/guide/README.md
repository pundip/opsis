# Opsis user guide

Opsis is a self-hosted business intelligence tool. It copies tables from your databases and files into its own storage, lets you describe them once (names, measures, joins, who may see which rows), and turns them into dashboards that people and AI agents build and read.

This guide covers everything Opsis does today (version 1, as of 25 November 2026).

## How the pieces fit

```
Your sources                Opsis                                        People and agents
------------                -----                                        -----------------
MySQL, PostgreSQL,   -->  Connection  -->  Dataset  -->  Semantic  -->  Dashboard  -->  Viewers
CSV, Excel,               (how to          (a local      model          (visuals,       Share links
Google Sheets,             reach it)        copy of       (fields,       filters)       Email
Salesforce                                  one table)    measures,                     MCP and the
                                                          joins, rules)                 assistant
```

| Word | What it means |
|---|---|
| **Connection** | The settings for one source: a database server, an uploaded file, a spreadsheet, a Salesforce org. Secrets are stored encrypted. |
| **Dataset** | A copy of one table (or view, sheet or Salesforce object) held by Opsis. Dashboards read the copy, never the source. A **refresh** reads the source again. |
| **Semantic model** | One per dataset. It names the **dimensions** (things you group and filter by), the **measures** (numbers you add up, count or average), the **joins** to other datasets and the **row-level security** rules. |
| **Dashboard** | Visuals arranged on a grid, with filters. Every save is a version; **publishing** makes a version the one viewers see. |

## Contents

| Chapter | What is in it |
|---|---|
| [1. Getting started](01-getting-started.md) | Install, laptop and server modes, signing in, the screens, roles, demo data |
| [2. Connections](02-connections.md) | CSV and Excel, PostgreSQL, MySQL, Google Sheets, Salesforce |
| [3. Datasets](03-datasets.md) | Creating datasets, refresh modes, schedules, untidy data, column types, access |
| [4. Semantic models](04-semantic-models.md) | Dimensions, measures, segments, dates and calendars, trying queries, versions |
| [5. Joining datasets](05-joins.md) | **Datasets with joins**, step by step, the rules, database views, common errors |
| [6. Dashboards](06-dashboards.md) | The editor, every visual, filters, publishing, history, viewing |
| [7. Sharing and security](07-sharing-and-security.md) | People, groups, sharing, share links, restricted data, row-level security |
| [8. AI agents and the assistant](08-agents.md) | The assistant in the editor, MCP with Claude Code and Claude Desktop, the REST API |
| [9. Running Opsis](09-operations.md) | Jobs, schedules, backups, email, upgrades, settings, troubleshooting, limits |

## Your first dashboard in ten minutes

1. [Start Opsis](01-getting-started.md#install-on-a-laptop) and open http://localhost:8080.
2. [Load the demo data](01-getting-started.md#demo-data) to see a finished example, or [add a connection](02-connections.md) to your own data.
3. [Create a dataset](03-datasets.md#create-a-dataset) from a table and wait for its first refresh.
4. [Open its semantic model](04-semantic-models.md) and add the measures you care about.
5. If you need fields from another table, [join the datasets](05-joins.md).
6. [Build a dashboard](06-dashboards.md#a-worked-example): add visuals, give them fields, add filters.
7. **Save**, then **Publish**.
8. [Share it](07-sharing-and-security.md#sharing-a-dashboard) (on a server with sign-in).

## About the examples

The screenshots come from a throwaway test install running in server mode, with made-up data. On a laptop the screens look the same, without the sign-in, sharing and people features. The worked examples use the **demo data** built into Opsis and the **sample MySQL database** that ships with it, so you can follow them exactly.

Other documents: the [project README](../../README.md), the [Windows guide](../../deploy/windows/README.md), the [Proxmox server guide](../../deploy/proxmox/README.md), [architecture](../ARCHITECTURE.md) and [decisions](../DECISIONS.md).
