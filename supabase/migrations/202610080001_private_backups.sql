-- Apply once in the free Supabase project's SQL editor. No application data is
-- moved: SQLite remains the live source for alert delivery and buying guards.
begin;

create table if not exists public.msj_private_backups (
    owner_id uuid not null references auth.users(id) on delete cascade,
    backup_kind text not null check (backup_kind = 'sqlite-v1'),
    ciphertext text not null check (
        ciphertext like 'gAAAA%' and octet_length(ciphertext) <= 48234496
    ),
    updated_at timestamptz not null default now(),
    primary key (owner_id, backup_kind)
);

alter table public.msj_private_backups enable row level security;
alter table public.msj_private_backups force row level security;
revoke all on table public.msj_private_backups from anon;
grant select, insert, update, delete on table public.msj_private_backups to authenticated;

drop policy if exists msj_owner_select on public.msj_private_backups;
create policy msj_owner_select on public.msj_private_backups
    for select to authenticated using ((select auth.uid()) = owner_id);
drop policy if exists msj_owner_insert on public.msj_private_backups;
create policy msj_owner_insert on public.msj_private_backups
    for insert to authenticated with check ((select auth.uid()) = owner_id);
drop policy if exists msj_owner_update on public.msj_private_backups;
create policy msj_owner_update on public.msj_private_backups
    for update to authenticated using ((select auth.uid()) = owner_id)
    with check ((select auth.uid()) = owner_id);
drop policy if exists msj_owner_delete on public.msj_private_backups;
create policy msj_owner_delete on public.msj_private_backups
    for delete to authenticated using ((select auth.uid()) = owner_id);

create or replace function public.msj_backup_updated_at()
returns trigger language plpgsql set search_path = '' as $$
begin
    new.updated_at = now();
    return new;
end;
$$;
drop trigger if exists msj_backup_updated_at on public.msj_private_backups;
create trigger msj_backup_updated_at before update on public.msj_private_backups
    for each row execute function public.msj_backup_updated_at();

commit;
