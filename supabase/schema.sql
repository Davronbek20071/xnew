-- ============================================================================
-- X Obunachi Multi-Userbot Tizimi — Supabase (PostgreSQL) Ma'lumotlar Bazasi
-- Supabase Dashboard -> SQL Editor ga nusxalab, "Run" tugmasini bosing.
-- ============================================================================

-- 1. Proksilar jadvali
CREATE TABLE IF NOT EXISTS public.proxies (
    id BIGSERIAL PRIMARY KEY,
    protocol VARCHAR(16) NOT NULL DEFAULT 'socks5', -- socks5 | http | socks4
    host VARCHAR(255) NOT NULL,
    port INTEGER NOT NULL CHECK (port > 0 AND port <= 65535),
    username VARCHAR(255),
    password VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (protocol, host, port, username)
);

-- 2. Telegram Userbot akkauntlari jadvali
CREATE TABLE IF NOT EXISTS public.accounts (
    id BIGSERIAL PRIMARY KEY,
    phone VARCHAR(32) NOT NULL UNIQUE,
    session_name VARCHAR(128) NOT NULL UNIQUE,
    session_string TEXT NOT NULL, -- Telethon StringSession (ixtiyoriy Fernet bilan shifrlangan)
    api_id BIGINT NOT NULL,
    api_hash VARCHAR(128) NOT NULL,
    proxy_id BIGINT REFERENCES public.proxies(id) ON DELETE SET NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'paused'
        CHECK (status IN ('active', 'paused', 'flood_wait', 'banned')),
    status_reason TEXT,
    balance NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    last_bonus_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    flood_until TIMESTAMPTZ,
    last_active_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_accounts_status ON public.accounts(status);
CREATE INDEX IF NOT EXISTS idx_accounts_next_run ON public.accounts(next_run_at);

-- 3. Obuna bo'lingan kanallar jadvali (16 kunlik Auto-Leave nazorati uchun)
CREATE TABLE IF NOT EXISTS public.subscriptions (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    chat_id BIGINT,
    username_or_link TEXT NOT NULL,
    title TEXT,
    is_mandatory BOOLEAN NOT NULL DEFAULT FALSE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    leave_after TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '16 days'),
    is_left BOOLEAN NOT NULL DEFAULT FALSE,
    left_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_due_leave
    ON public.subscriptions(account_id, is_left, leave_after);
CREATE INDEX IF NOT EXISTS idx_subscriptions_account_link
    ON public.subscriptions(account_id, username_or_link);

-- 4. Statistika va hodisalar jurnali
CREATE TABLE IF NOT EXISTS public.stats_logs (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT REFERENCES public.accounts(id) ON DELETE CASCADE,
    action_type VARCHAR(32) NOT NULL
        CHECK (action_type IN ('subscribe', 'mandatory_sub', 'bonus', 'leave', 'view_post', 'error', 'info')),
    amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    details TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stats_logs_created_at ON public.stats_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stats_logs_account_action ON public.stats_logs(account_id, action_type, created_at DESC);

-- 5. Dinamik tizim sozlamalari (Admin Panel orqali o'zgartiriladi)
CREATE TABLE IF NOT EXISTS public.settings (
    key VARCHAR(64) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Standart sozlamalarni kiritish
INSERT INTO public.settings (key, value) VALUES
    ('target_bot_username', 'XObunachiBot'),
    ('cycle_min_minutes', '20'),
    ('cycle_max_minutes', '25'),
    ('action_delay_min_sec', '3'),
    ('action_delay_max_sec', '8'),
    ('auto_leave_days', '16'),
    ('max_tasks_per_cycle', '25')
ON CONFLICT (key) DO NOTHING;

-- 6. Aiogram 3 FSM holatlari jadvali (Vercel serverless muhiti uchun)
CREATE TABLE IF NOT EXISTS public.fsm_states (
    storage_key VARCHAR(255) PRIMARY KEY,
    state VARCHAR(255),
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Avtomatik updated_at trigger funksiyasi
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_accounts_updated_at ON public.accounts;
CREATE TRIGGER trg_accounts_updated_at
    BEFORE UPDATE ON public.accounts
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_settings_updated_at ON public.settings;
CREATE TRIGGER trg_settings_updated_at
    BEFORE UPDATE ON public.settings
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_fsm_states_updated_at ON public.fsm_states;
CREATE TRIGGER trg_fsm_states_updated_at
    BEFORE UPDATE ON public.fsm_states
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- Eslatma: Backend Supabase Service Role Key bilan ulanganligi uchun
-- RLS yoqilsa ham service_role to'liq ruxsatga ega bo'ladi.
ALTER TABLE public.proxies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stats_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fsm_states ENABLE ROW LEVEL SECURITY;
