-- Minimal hackathon persistence.

insert into storage.buckets (id, name, public)
values ('pitch-decks', 'pitch-decks', false)
on conflict (id) do nothing;

create table if not exists public.pitch_decks (
    id uuid primary key,
    filename text not null,
    storage_path text not null,
    full_text text not null,
    pages jsonb not null default '[]'::jsonb,
    parser_metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
