create extension if not exists pgcrypto;

create table if not exists fb_accounts (
    id uuid primary key default gen_random_uuid(),
    account_id text not null unique,
    label text not null default '',
    cookie_ciphertext text not null,
    active boolean not null default true,
    cookie_status text not null default 'unverified',
    cookie_status_detail text not null default '',
    proxy_ciphertext text not null default '',
    cookie_status_checked_at timestamptz,
    cookie_status_updated_at timestamptz not null default now(),
    created_by bigint,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table fb_accounts add column if not exists cookie_status text not null default 'unverified';
alter table fb_accounts add column if not exists cookie_status_detail text not null default '';
alter table fb_accounts add column if not exists proxy_ciphertext text not null default '';
alter table fb_accounts add column if not exists cookie_status_checked_at timestamptz;
alter table fb_accounts add column if not exists cookie_status_updated_at timestamptz not null default now();

create table if not exists fb_pages (
    id uuid primary key default gen_random_uuid(),
    account_id text not null references fb_accounts(account_id) on delete cascade,
    page_id text not null,
    page_name text not null default '',
    follower_count text not null default '',
    page_url text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(account_id, page_id)
);

alter table fb_pages add column if not exists follower_count text not null default '';

create table if not exists fb_account_runtime (
    account_id text primary key references fb_accounts(account_id) on delete cascade,
    last_cookie_used_at timestamptz,
    locked_until timestamptz,
    locked_by text,
    updated_at timestamptz not null default now()
);

create table if not exists telegram_user_state (
    telegram_user_id bigint primary key,
    active_account_id text references fb_accounts(account_id) on delete set null,
    last_chat_id bigint,
    first_name text not null default '',
    last_name text not null default '',
    username text not null default '',
    lang text not null default 'en',
    approval_status text not null default 'approved',
    approved_by bigint,
    approved_at timestamptz,
    approval_requested_at timestamptz,
    created_at timestamptz not null default now(),
    last_seen_at timestamptz,
    updated_at timestamptz not null default now()
);

alter table telegram_user_state add column if not exists last_chat_id bigint;
alter table telegram_user_state add column if not exists first_name text not null default '';
alter table telegram_user_state add column if not exists last_name text not null default '';
alter table telegram_user_state add column if not exists username text not null default '';
alter table telegram_user_state add column if not exists last_seen_at timestamptz;
alter table telegram_user_state add column if not exists lang text not null default 'en';
alter table telegram_user_state add column if not exists approval_status text not null default 'approved';
alter table telegram_user_state add column if not exists approved_by bigint;
alter table telegram_user_state add column if not exists approved_at timestamptz;
alter table telegram_user_state add column if not exists approval_requested_at timestamptz;

create table if not exists bot_meta (
    key text primary key,
    value text not null default '',
    updated_at timestamptz not null default now()
);

create table if not exists fb_post_jobs (
    id uuid primary key default gen_random_uuid(),
    telegram_chat_id bigint,
    telegram_user_id bigint,
    account_id text not null references fb_accounts(account_id) on delete cascade,
    account_label text not null default '',
    page_id_or_url text not null,
    page_name text not null default '',
    post_type text not null check (post_type in ('text', 'image', 'video')),
    caption text not null default '',
    media_path text not null default '',
    status text not null default 'queued',
    result jsonb not null default '{}'::jsonb,
    error text not null default '',
    created_at timestamptz not null default now(),
    started_at timestamptz,
    completed_at timestamptz
);

alter table fb_post_jobs add column if not exists account_label text not null default '';

update fb_post_jobs j
set account_label = a.label
from fb_accounts a
where j.account_id = a.account_id
  and coalesce(j.account_label, '') = ''
  and coalesce(a.label, '') <> '';

create index if not exists idx_fb_accounts_active on fb_accounts(active);
create index if not exists idx_fb_accounts_created_by on fb_accounts(created_by);
create index if not exists idx_fb_pages_account_id on fb_pages(account_id);
create index if not exists idx_fb_account_runtime_locked_until on fb_account_runtime(locked_until);
create index if not exists idx_telegram_user_state_active_account on telegram_user_state(active_account_id);
create index if not exists idx_fb_post_jobs_account_status on fb_post_jobs(account_id, status);
create index if not exists idx_fb_post_jobs_user_status on fb_post_jobs(telegram_user_id, status);
create index if not exists idx_fb_post_jobs_created_at on fb_post_jobs(created_at desc);
