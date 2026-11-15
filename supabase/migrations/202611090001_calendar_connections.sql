create table public.calendar_connections (
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null check (provider = 'google'),
  encrypted_refresh_token text not null,
  granted_scope text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (user_id, provider)
);
create table public.calendar_oauth_states (
  state_hash text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  encrypted_verifier text not null,
  expires_at timestamptz not null default (now() + interval '10 minutes')
);
alter table public.calendar_connections enable row level security;
alter table public.calendar_oauth_states enable row level security;
revoke all on public.calendar_connections, public.calendar_oauth_states from public, anon, authenticated;
grant all on public.calendar_connections, public.calendar_oauth_states to service_role;
create index calendar_oauth_states_expiry on public.calendar_oauth_states (expires_at);
