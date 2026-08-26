# Plugins

A plugin is a separate package the panel installs into itself: it brings its own pages, its own permissions, its own database tables and background tasks. Installed and updated from the **Plugins** section.

## Installing

**Plugins** → the card you want → **Install**. The panel downloads the package, verifies its checksum, installs it and picks it up without a restart.

Permissions declared by the plugin appear for the superadmin immediately. Other roles get them by hand, like any other permission — see [Access and roles](/en/guide/access).

::: tip Panel version requirement
Every plugin release states the minimum panel version. If yours is older, the panel refuses to install and says which version is needed, rather than installing a package that would not work.
:::

## Free and paid

A free plugin installs right away, with no purchase: no prices, no quotas, no paid-until date. A paid one requires a subscription with an expiry date, and the card shows its state.

There is also a separate state for a plugin withdrawn from sale: the card stays, the purchase buttons are gone, and everyone who already paid keeps working.

## Release channels

Plugins have two channels, **stable** and **dev**. Everyone is on stable by default. Switching happens on the store side, per panel rather than for everybody at once. The dev channel carries versions that are still being run in.

## What plugins can do

- Their own pages in the panel and entries in the sidebar
- Their own permissions inside the shared role system
- Their own tables and migrations, applied together with the panel migrations
- Scheduled background tasks
- Buttons under Telegram notifications: the plugin describes an action as text, action and object, and knows nothing about Telegram — the panel assembles the button and checks the permissions of whoever taps it

## Existing plugins

**Block Radar 0.7.4** watches where and when online drops. Soft-throttle compatibility requires Node Agent 1.7.3 or newer; old and unknown versions get a dedicated warning. A planned restart suppresses a new alert only when an explicit restart audit event exists. An invalid Sonnet response is stored as unavailable and never changes the deterministic incident.

**Smart Support 1.4.4** diagnoses a user problem in one click. An active administrative rate throttle is shown read-only with its reason and expiry. The configured AI provider distinguishes a recommendation from an executed measure and does not suggest removing the throttle without authorization. Before the migration creates the table, this section is simply absent.

**Retention Radar 1.2.4** runs retention campaigns with the existing server-side no-send guards. Users under an active administrative throttle are excluded from live delivery; dry-run reports a separate `skipped_throttled` count and still sends nothing.

**Incident Center 0.1.2** is the shared infrastructure-event queue. It shows the Agent 1.7.3 rollout, explicit restart audit events, and aggregate `violation.throttle.add/remove` activity. A failed-squad-restore incident is created only from explicit `restore_failed` or `restore_required` evidence; `squads_restored=false` alone is not enough.

The exact compatibility provenance is recorded in `plugins-src/PROVENANCE.md`.

## Removing

The **Remove** button on the card. The panel uninstalls the package and drops its pages from the interface; plugin data stays in the database in case you come back.
