alter table public.calendar_oauth_states add column created_at timestamptz not null default now();
create table public.calendar_disconnects (
  user_id uuid primary key references auth.users(id) on delete cascade,
  disconnected_at timestamptz not null default '-infinity'
);
alter table public.calendar_disconnects enable row level security;
revoke all on public.calendar_disconnects from public, anon, authenticated;
grant all on public.calendar_disconnects to service_role;

create function public.save_calendar_google_connection(p_user_id uuid, p_state_created_at timestamptz, p_encrypted_refresh_token text, p_scope text)
returns boolean language plpgsql security definer set search_path = public as $$
declare cutoff timestamptz;
begin
  insert into calendar_disconnects(user_id) values (p_user_id) on conflict do nothing;
  select disconnected_at into cutoff from calendar_disconnects where user_id = p_user_id for update;
  if p_state_created_at is null or p_state_created_at <= cutoff then return false; end if;
  insert into calendar_connections(user_id, provider, encrypted_refresh_token, granted_scope)
  values (p_user_id, 'google', p_encrypted_refresh_token, p_scope)
  on conflict (user_id, provider) do update set encrypted_refresh_token = excluded.encrypted_refresh_token,
    granted_scope = excluded.granted_scope, updated_at = clock_timestamp();
  return true;
end;
$$;
create function public.disconnect_calendar_google(p_user_id uuid)
returns void language plpgsql security definer set search_path = public as $$
begin
  insert into calendar_disconnects(user_id) values (p_user_id) on conflict do nothing;
  perform 1 from calendar_disconnects where user_id = p_user_id for update;
  update calendar_disconnects set disconnected_at = clock_timestamp() where user_id = p_user_id;
  delete from calendar_connections where user_id = p_user_id and provider = 'google';
  delete from calendar_oauth_states where user_id = p_user_id;
end;
$$;
revoke all on function public.save_calendar_google_connection(uuid, timestamptz, text, text) from public, anon, authenticated;
revoke all on function public.disconnect_calendar_google(uuid) from public, anon, authenticated;
grant execute on function public.save_calendar_google_connection(uuid, timestamptz, text, text) to service_role;
grant execute on function public.disconnect_calendar_google(uuid) to service_role;
