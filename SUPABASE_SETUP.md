# Optional free Supabase setup

This integration adds hosted owner authentication and a private encrypted database
backup. Existing SQLite searches, history, photos, alert delivery and buying guards
remain the live data source. It does not create a project, automatically upload
existing data or move the monitor to the reference project's browser-driven feed.

## Create and configure the free project

1. Sign in at <https://supabase.com/dashboard> and create a project in a **Free**
   organization. Choose a nearby region and store the database password privately.
   A paid plan is not required for this integration.
2. In Authentication → Users, create the single owner using your email and a
   password. Complete email confirmation if enabled. Copy that user's UUID.
   Disable public user signup in the authentication settings for this private app.
3. Run [the migration](supabase/migrations/202610080001_private_backups.sql) in the
   project's SQL editor. It creates an encrypted backup table with owner-scoped
   row-level security. Anonymous access has no table privileges.
4. In the bot hosting provider's private environment settings, set:

   | Variable | Private dashboard source |
   | --- | --- |
   | `SUPABASE_URL` | Project API URL, `https://<project-ref>.supabase.co` |
   | `SUPABASE_PUBLISHABLE_KEY` | Project publishable key, or legacy **anon** key |
   | `SUPABASE_OWNER_ID` | Owner's UUID from Authentication → Users |

   Do not use a service-role/secret key. The application rejects privileged keys
   and authenticates database requests using the signed-in owner's access token.
   Keep passwords, bearer tokens and any other production secrets out of chat.
5. Deploy the application changes with the existing persistent volume attached.
   Open `/supabase/login` and sign in with the Supabase owner account. When the
   three settings are present, hosted sign-in replaces the local dashboard login.
   A local password or old owner cookie cannot bypass it. Other Supabase users
   cannot access this single-owner dashboard.

If none of the three settings is present, the existing local login continues to
work. A partial or invalid configuration stops dashboard initialization instead
of silently falling back. Supabase downtime prevents new dashboard requests from
authenticating; it does not alter the independently running alerts or SQLite data.

## Backups and recovery

Open `/supabase/backup` after signing in, then choose **Save cloud backup**. The
application takes a consistent SQLite snapshot, removes Supabase web sessions
from the copy, compresses it and encrypts the entire snapshot before uploading.
Vinted session values are already encrypted locally and never appear as raw
Supabase columns. The latest backup replaces the previous backup for this owner.
Uploads are manual; there is no automatic restore. The snapshot limit is 32 MB
before compression; larger deployments should use the hosting provider's volume
backup instead. A Supabase free project can pause after inactivity, so keep a
separate volume backup for recovery.


A signed-in owner can choose **Verify saved cloud backup** on the same page. Its
CSRF-protected POST `/supabase/backup/verify` reuses the verified owner session
and owner-scoped cloud download. The check reads existing private keys only,
validates the snapshot header, bounds decompression, checks a separate read-only
temporary SQLite file, and then deletes that file. It reports safe counts,
backup timestamp and whether saved settings/buyer records match live data;
it never restores the live database or exports keys. A missing key, corrupt
snapshot, absent example reference or search count other than 44 is reported
as unverified. This diagnostic is scoped to the current 44-search deployment.

The application creates `supabase-backup.key` beside the live SQLite database on
its first backup, with owner-only file permissions. Keep that key **separately**
in your private password manager or secured volume backup. Also retain the
existing `vinted-buyer.key` from the same persistent volume: it is needed to read
the buyer session after restoring SQLite. Neither key is uploaded to Supabase or
included in the downloadable encrypted snapshot. Losing `supabase-backup.key`
makes the cloud backup unreadable. The database contains the dashboard signing
secret, so protect restored plaintext backups as production data.

The saved encrypted file can be downloaded from `/supabase/backup/download`.
For an offline recovery review, use `cryptography.fernet.Fernet` with the saved
backup key to decrypt it. Verify the plaintext starts with `MSJ SQLite snapshot
v1\n`, then gzip-decompress the remaining bytes into a **new** SQLite file.
Inspect that file before replacing production data. Restoring an old database
also restores old purchase-attempt state: reconcile completed and uncertain
purchases on Vinted before enabling buying after any restore.

## Integration contract

`install_dashboard(app, database_path_factory)` returns `None` when disabled,
otherwise a `DashboardIntegration`. The main dashboard guard must make
`supabase.login` public and require `integration.is_authenticated()` on all
private routes. Redirect legacy `/login` and `/setup` to the hosted-login route
when enabled, and call `integration.logout()` before clearing the Flask session.
Continue applying the existing CSRF check to POSTs. Supabase tokens are encrypted
in a local server session table; the Flask cookie contains only a random session
reference and the existing dashboard claims. Server-token encryption uses a
dedicated owner-only `supabase-session.key` on the persistent volume, outside
SQLite. Losing that key requires signing in again; cloud backups exclude these
web sessions. Owner identity is verified through
`/auth/v1/user`, including after refresh, before accepting sign-in.

Official references: [password authentication](https://supabase.com/docs/guides/auth/passwords),
[API keys](https://supabase.com/docs/guides/api/api-keys), and
[row-level security](https://supabase.com/docs/guides/database/postgres/row-level-security).

