# Deploying the Slack app (T26)

`launchd` runs `t2c slack` from a tagged release on one always-on Mac. It starts at login and restarts if it stops for any reason but `t2c-service`. Slack reaches it over Socket Mode, so it needs no public address.

## Where it lives

macOS refuses a `launchd` job access to `~/Documents` ("Operation not permitted"), so the service keeps its own home, and needs no extra permission:

| Path | What it holds |
|---|---|
| `~/.t2c/releases/<tag>/` | That tag's code and its dependencies |
| `~/.t2c/current` | A link to the release that's running |
| `~/.t2c/env` | The secrets, readable only by you |
| `~/.t2c/films/` | The Films the service can run: only those you've added |
| `~/.t2c/runs/` | Its Run Logs, which can hold spoilers |
| `~/.t2c/logs/slack.log` | What the app printed |

## Install

Run these from the repo, in a shell where your settings are exported:

```
deploy/t2c-service secrets            # copies the settings into ~/.t2c/env; prints none of them
deploy/t2c-service add-film <slug>    # once per Film the service should run
deploy/t2c-service install v0.1.0     # any tag from T26 on
deploy/t2c-service status             # should say: state = running
```

The settings are `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `T2C_MARKETERS`, plus `FILM_BUDGET_USD`, `T2C_WATCHER` and `T2C_WHISPER_MODEL` if they're set. Run `secrets` again after changing one, then `install` to restart with it.

Then mention the bot in the test channel with no Film named. It should reply that it couldn't tell which Film you mean. That reply lists every Film in `~/.t2c/films/`, so do this with your screen unshared.

## Upgrade, go back, stop

| To | Run |
|---|---|
| Upgrade | `git fetch --tags`, then `deploy/t2c-service install <new tag>` |
| Go back | `deploy/t2c-service install <old tag>` |
| See what it's doing | `deploy/t2c-service logs` |
| Stop it for good | `deploy/t2c-service uninstall`, which removes `~/.t2c/env` and leaves the rest of `~/.t2c` |

Run `t2c-service` from a checkout that has that tag. If the tag can't run as a service, its dependencies can't be installed, or the plist isn't valid, `install` stops and the running release stays. A tag that moved after it was installed is refused: cut a new one.

Installing stops the running service first. A Run waiting for Approval ends in an error, never in an Approval. A Run in the middle of a stage finishes that stage first, and `install` waits for it. After 15 minutes `launchd` forces it to stop, so its Run Log may then lack a total.

## Films

The service reads its own copy of a Film. **After changing a Film's Spoiler List or catalog, run `add-film` again**: until then, the service checks posts against the old list. `status` and `install` warn when a copy differs from the repo's `film.toml`, but nothing warns a Marketer in Slack, so check before a Run. `add-film` keeps the frames the service has cached.

## Limits

- **The Mac must be on and logged in.** `caffeinate` keeps it awake while the service runs, but it doesn't survive closing the lid on battery. It's a per-user service, so it starts at login, not at boot.
- **One Run at a time, with no cancel.** A Marketer who walks away blocks everyone. To clear it, run `deploy/t2c-service install <the same tag>`. See #38.
- **One Mac.** There's no failover, and the Run Logs live only there, so back up `~/.t2c/runs/` if you need them.
- **A stray restart loop is throttled.** If it can't start, such as with a missing setting, `launchd` tries again every 30 seconds, and `logs` says why.
- **The log isn't rotated.** `~/.t2c/logs/slack.log` grows until you empty it.
