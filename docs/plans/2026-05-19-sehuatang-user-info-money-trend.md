# SehuatangSignin multi-account user info and money trend

## Goal

Add profile/asset parsing for each SehuatangSignin account and present it in the MoviePilot detail page as responsive account cards plus a per-account daily money trend chart.

## Approved UI direction

- Top summary card: account count, today's successful count, latest refresh time.
- Responsive horizontal account cards using `VRow`/`VCol`; cards wrap on smaller screens.
- Each card shows account name, sign-in status, user group/level, credits, money, registration time, and last refresh/error.
- Money trend chart keeps per-account daily money values, rendered with `VApexChart` as multiple line series.
- Existing execution history table remains below.
- Notifications include compact per-account asset summary.

## Data model

- `user_info_by_account`: map keyed by account id.
  - `username`, `user_group`, `credits`, `money`, `register_time`, `last_refresh`, `error`.
- `money_history`: list of day snapshots.
  - `{ "day": "YYYY-MM-DD", "values": { "account_id": number } }`
  - Same-day updates replace the latest value for that account.
  - Keep max 90 days.

## Tasks

1. Add helpers and tests for profile parsing.
   - Parse `/home.php?mod=space` and `/home.php?mod=spacecp&ac=credit&showcredit=1` HTML text.
   - Extract user group/level, credits, money, register time.
2. Add account profile refresh in sign-in flow.
   - After account sign-in/skip/failure, try to refresh profile without breaking sign-in result.
   - Store profile into `result["user_info"]` and save map after all accounts complete.
3. Add money history persistence.
   - Merge successful parsed money into daily snapshot.
   - Keep latest 90 days.
4. Rebuild detail page.
   - Summary card.
   - Responsive account cards.
   - Money trend chart.
   - Existing execution records retained.
5. Update notifications.
   - Add compact level/credits/money lines per account when available.
6. Bump version/package history to `1.0.0` for this larger feature.
7. Verify.
   - `py_compile`
   - `json.tool package.v2.json`
   - existing render/fetch/session-store tests
   - new standalone profile parsing/render test
   - `git diff --check`

## Constraints

- Do not push until Vito confirms the final checklist.
- Do not log or print cookie values.
- Do not let profile parsing failure mark a successful sign-in as failed.
- Preserve site-wide captcha locking behavior.

## Follow-up requirements added by Vito

- Profile refresh is kept after each account's sign-in attempt finishes, using the same account FS session/cookies before cleanup, so accounts remain independent.
- Plugin logs should include outgoing notification content for captcha, summary, and reminder notifications.
- Captcha images should not remain longer than needed: they are embedded base64 in the session JSON, not separate image files; after user submit, image fields are stripped immediately, and `destroy_session` removes solved/expired sessions.
- Add an independent sign-in reminder task with its own switch, cron expression, and notification content. It does not run sign-in. If all configured accounts already have successful local sign-in records for today, it skips notification.
- Serial multi-account execution should support randomized order per run; implemented as a config switch (`random_account_order`, default enabled). Parallel mode keeps existing behavior.

## Final layout polish

- Detail page was refined into a clearer hierarchy:
  - hero/summary card with version chip and four stat tiles
  - account status section wrapping responsive account cards
  - money trend chart
  - compact execution history table
- Config page explanations were expanded without becoming too long:
  - basic config explains plugin execution switches
  - account section explains account names and duplicate handling
  - access/captcha section explains `base_url`, FlareSolverr, proxy, and public relay URL purposes
  - timing section explains main cron vs serial/parallel behavior
  - reminder section explains independent reminder and local-history skip logic
  - bottom notes now keep important operational details: domain purpose, FlareSolverr/proxy, public captcha URL, Cookie refresh, image cleanup, reminder behavior

## Final config grouping adjustment

- Notification settings moved out of Basic Config into a dedicated `通知与提醒` card.
- `notify` switch now lives with reminder settings (`reminder_enabled`, `reminder_cron`, `reminder_text`).
- Added `refresh_profile` switch (`签到后刷新个人资料`), default enabled. When disabled, sign-in flow is unchanged and existing profile/history data is reused instead of refreshing profile/credits pages.
- Basic Config now only contains plugin execution-level switches: enabled, run once after save, and FlareSolverr usage.
