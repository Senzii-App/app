"""SQL migration runner — creates all tables idempotently.
Direct port of src/db/migrations.rs from the Rust project.
All SQL is identical — do not modify the queries.
"""
import asyncpg

# All migration SQL statements in order. Each uses CREATE TABLE IF NOT EXISTS /
# CREATE INDEX IF NOT EXISTS so this is safe to call repeatedly.

MIGRATION_SQL = [
    # ── Migration tracking ──────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS _migrations (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL UNIQUE,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # ── Core: users ─────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email VARCHAR(255) NOT NULL,
        name VARCHAR(255),
        password_hash VARCHAR(255),
        role VARCHAR(50),
        organization_id INTEGER,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        stripe_subscription_id VARCHAR(255),
        subscription_status VARCHAR(50),
        subscription_plan VARCHAR(255),
        subscription_expires_at TIMESTAMPTZ,
        subscription_updated_at TIMESTAMPTZ
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique_idx ON users (LOWER(email))",
    "CREATE INDEX IF NOT EXISTS users_stripe_subscription_id_idx ON users (stripe_subscription_id)",

    # ── organizations ──────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS organizations (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        owner_user_id INTEGER,
        stripe_customer_id VARCHAR(255),
        stripe_subscription_id VARCHAR(255),
        subscription_status VARCHAR(50),
        subscription_plan VARCHAR(50),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS organizations_name_unique_idx ON organizations (LOWER(name))",
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_organization_id_fk') THEN
            ALTER TABLE users ADD CONSTRAINT users_organization_id_fk
                FOREIGN KEY (organization_id) REFERENCES organizations(id);
        END IF;
    END $$""",
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'organizations_owner_user_id_fk') THEN
            ALTER TABLE organizations ADD CONSTRAINT organizations_owner_user_id_fk
                FOREIGN KEY (owner_user_id) REFERENCES users(id);
        END IF;
    END $$""",
    """CREATE UNIQUE INDEX IF NOT EXISTS organizations_stripe_customer_id_idx
     ON organizations (stripe_customer_id)
     WHERE stripe_customer_id IS NOT NULL""",

    # ── work_sites ─────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS work_sites (
        id SERIAL PRIMARY KEY,
        organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        name VARCHAR(255) NOT NULL,
        address TEXT,
        latitude NUMERIC(10, 8) NOT NULL,
        longitude NUMERIC(11, 8) NOT NULL,
        required_skills JSONB NOT NULL DEFAULT '[]'::jsonb,
        timezone VARCHAR(255) NOT NULL DEFAULT 'UTC',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS work_sites_org_idx ON work_sites (organization_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS work_sites_name_org_uniq ON work_sites (name, organization_id)",

    # ── staff ──────────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS staff (
        id SERIAL PRIMARY KEY,
        organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        name VARCHAR(255) NOT NULL,
        email VARCHAR(255) NOT NULL,
        phone VARCHAR(50),
        address TEXT,
        latitude NUMERIC(10, 8),
        longitude NUMERIC(11, 8),
        timezone VARCHAR(255) NOT NULL DEFAULT 'UTC',
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        deleted_at TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS staff_org_idx ON staff (organization_id)",
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'staff' AND column_name = 'deleted_at') THEN
            ALTER TABLE staff ADD COLUMN deleted_at TIMESTAMPTZ;
        END IF;
    END $$""",
    "CREATE UNIQUE INDEX IF NOT EXISTS staff_email_org_uniq ON staff (email, organization_id) WHERE deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS staff_active_org_idx ON staff (organization_id) WHERE deleted_at IS NULL",
    # Make email uniqueness partial (only non-deleted)
    """DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_indexes
                   WHERE indexname = 'staff_email_org_uniq'
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_index i
                       JOIN pg_class c ON c.oid = i.indexrelid
                       WHERE c.relname = 'staff_email_org_uniq'
                       AND i.indpred IS NULL
                   )) THEN
            DROP INDEX staff_email_org_uniq;
        END IF;
    END $$""",
    "CREATE UNIQUE INDEX IF NOT EXISTS staff_email_org_uniq ON staff (email, organization_id) WHERE deleted_at IS NULL",

    # ── staff_certifications ───────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS staff_certifications (
        id SERIAL PRIMARY KEY,
        staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        name VARCHAR(255) NOT NULL,
        expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",

    # ── staff_availability ────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS staff_availability (
        id SERIAL PRIMARY KEY,
        staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        day_of_week SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
        start_time TIME NOT NULL,
        end_time TIME NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (staff_id, day_of_week, start_time, end_time)
    )""",

    # ── shifts ────────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS shifts (
        id SERIAL PRIMARY KEY,
        organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        site_id INTEGER NOT NULL REFERENCES work_sites(id) ON DELETE CASCADE,
        start_time TIMESTAMPTZ NOT NULL,
        end_time TIMESTAMPTZ NOT NULL,
        required_skills JSONB NOT NULL DEFAULT '[]'::jsonb,
        min_staff INTEGER NOT NULL DEFAULT 1,
        assigned_staff_id INTEGER REFERENCES staff(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS shifts_org_idx ON shifts (organization_id)",
    "CREATE INDEX IF NOT EXISTS shifts_site_idx ON shifts (site_id)",

    # ── assignments ───────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS assignments (
        id SERIAL PRIMARY KEY,
        shift_id INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
        staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        organization_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
        score NUMERIC(5, 3) NOT NULL DEFAULT 0,
        status VARCHAR(50) NOT NULL DEFAULT 'pending',
        confirmed_at TIMESTAMPTZ,
        confirmed_by VARCHAR(255),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS assignments_shift_idx ON assignments (shift_id)",
    "CREATE INDEX IF NOT EXISTS assignments_staff_idx ON assignments (staff_id)",

    # ── clients ───────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS clients (
        id SERIAL PRIMARY KEY,
        organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        name VARCHAR(255) NOT NULL,
        email VARCHAR(255) NOT NULL,
        phone VARCHAR(50),
        company_name VARCHAR(255),
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS clients_org_idx ON clients (organization_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS clients_email_org_uniq ON clients (email, organization_id)",

    # ── staffing_requests ─────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS staffing_requests (
        id SERIAL PRIMARY KEY,
        organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
        site_id INTEGER REFERENCES work_sites(id) ON DELETE SET NULL,
        shift_date DATE NOT NULL,
        start_time TIME NOT NULL,
        end_time TIME NOT NULL,
        required_skills JSONB NOT NULL DEFAULT '[]'::jsonb,
        min_staff INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        status VARCHAR(50) NOT NULL DEFAULT 'open',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS staffing_requests_org_idx ON staffing_requests (organization_id)",

    # ── magic_tokens (staff) ──────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS magic_tokens (
        id SERIAL PRIMARY KEY,
        staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        token VARCHAR(255) NOT NULL UNIQUE,
        expires_at TIMESTAMPTZ NOT NULL,
        used_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS magic_tokens_token_idx ON magic_tokens (token)",

    # ── client_magic_tokens ──────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS client_magic_tokens (
        id SERIAL PRIMARY KEY,
        client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
        client_email VARCHAR(255),
        client_name VARCHAR(255),
        company_name VARCHAR(255),
        organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        token VARCHAR(255) NOT NULL UNIQUE,
        expires_at TIMESTAMPTZ NOT NULL,
        used_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS client_magic_tokens_token_idx ON client_magic_tokens (token)",
    "ALTER TABLE client_magic_tokens ADD COLUMN IF NOT EXISTS client_email VARCHAR(255)",
    "ALTER TABLE client_magic_tokens ADD COLUMN IF NOT EXISTS client_name VARCHAR(255)",
    "ALTER TABLE client_magic_tokens ADD COLUMN IF NOT EXISTS company_name VARCHAR(255)",
    "ALTER TABLE client_magic_tokens ADD COLUMN IF NOT EXISTS organization_id INTEGER REFERENCES organizations(id) ON DELETE CASCADE",

    # ── admin_magic_tokens ───────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS admin_magic_tokens (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        token VARCHAR(255) NOT NULL UNIQUE,
        expires_at TIMESTAMPTZ NOT NULL,
        used_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS admin_magic_tokens_token_idx ON admin_magic_tokens (token)",

    # ── password_resets ──────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS password_resets (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token VARCHAR(255) NOT NULL UNIQUE,
        expires_at TIMESTAMPTZ NOT NULL,
        used_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS password_resets_token_idx ON password_resets (token)",
    "CREATE INDEX IF NOT EXISTS password_resets_user_idx ON password_resets (user_id)",

    # ── external_id for organizations ───────────────────────────────────
    "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS external_id UUID DEFAULT gen_random_uuid()",
    "UPDATE organizations SET external_id = gen_random_uuid() WHERE external_id IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS organizations_external_id_idx ON organizations (external_id)",

    # ── api_keys ────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS api_keys (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        key_hash VARCHAR(255) NOT NULL UNIQUE,
        label VARCHAR(255) NOT NULL DEFAULT 'Default',
        last_used_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS api_keys_user_idx ON api_keys (user_id)",
    "CREATE INDEX IF NOT EXISTS api_keys_org_idx ON api_keys (organization_id)",

    # ── fix staffing_requests status ────────────────────────────────────
    "UPDATE staffing_requests SET status = 'open' WHERE status = 'pending'",

    # ── prevent overlapping assignments trigger ──────────────────────────
    """CREATE OR REPLACE FUNCTION prevent_overlapping_assignments()
    RETURNS TRIGGER AS $$
    DECLARE
        conflict_count INTEGER;
    BEGIN
        SELECT COUNT(*) INTO conflict_count
        FROM assignments a
        JOIN shifts sh ON sh.id = a.shift_id
        JOIN shifts new_shift ON new_shift.id = NEW.shift_id
        WHERE a.staff_id = NEW.staff_id
          AND a.id IS DISTINCT FROM NEW.id
          AND a.status != 'rejected'
          AND sh.start_time < new_shift.end_time
          AND sh.end_time > new_shift.start_time;
        IF conflict_count > 0 THEN
            RAISE EXCEPTION 'Staff member already has an overlapping assignment';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS check_assignment_overlap ON assignments",
    """CREATE TRIGGER check_assignment_overlap
    BEFORE INSERT OR UPDATE ON assignments
    FOR EACH ROW EXECUTE FUNCTION prevent_overlapping_assignments()""",

    # ── sessions table ──────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS sessions (
        id VARCHAR(255) PRIMARY KEY,
        data JSONB NOT NULL DEFAULT '{}'::jsonb,
        expiry_date TIMESTAMPTZ NOT NULL
    )""",

    # ── page_views ──────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS page_views (
        id BIGSERIAL PRIMARY KEY,
        visitor_hash VARCHAR(64) NOT NULL,
        page VARCHAR(255) NOT NULL DEFAULT '/',
        referrer VARCHAR(1024),
        utm_source VARCHAR(255),
        utm_medium VARCHAR(255),
        utm_campaign VARCHAR(255),
        utm_content VARCHAR(255),
        utm_term VARCHAR(255),
        screen_width INTEGER,
        screen_height INTEGER,
        language VARCHAR(20),
        section VARCHAR(100),
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS page_views_created_at_idx ON page_views (created_at)",
    "CREATE INDEX IF NOT EXISTS page_views_page_idx ON page_views (page)",
    "CREATE INDEX IF NOT EXISTS page_views_visitor_hash_idx ON page_views (visitor_hash)",
    "ALTER TABLE page_views ADD COLUMN IF NOT EXISTS visitor_hash VARCHAR(64)",
    "UPDATE page_views SET visitor_hash = 'legacy-no-hash' WHERE visitor_hash IS NULL",
    """DO $$ BEGIN
       IF EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_name = 'page_views' AND column_name = 'visitor_hash'
                  AND is_nullable = 'YES') THEN
         ALTER TABLE page_views ALTER COLUMN visitor_hash SET NOT NULL;
       END IF;
    END $$""",

    # ── add address column to staff ────────────────────────────────────
    "ALTER TABLE staff ADD COLUMN IF NOT EXISTS address TEXT",

    # ── organization_certifications ────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS organization_certifications (
        id SERIAL PRIMARY KEY,
        organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        name VARCHAR(255) NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (organization_id, name)
    )""",
    "CREATE INDEX IF NOT EXISTS org_certs_org_idx ON organization_certifications (organization_id)",

    # ── cert_id FK on staff_certifications ─────────────────────────────
    "ALTER TABLE staff_certifications ADD COLUMN IF NOT EXISTS cert_id INTEGER NOT NULL REFERENCES organization_certifications(id) ON DELETE CASCADE",

    # ── drop staff.skills ────────────────────────────────────────────────
    "ALTER TABLE staff DROP COLUMN IF EXISTS skills",

    # ── request_id on shifts + auto-fill trigger ────────────────────────
    "ALTER TABLE shifts ADD COLUMN IF NOT EXISTS request_id INTEGER REFERENCES staffing_requests(id) ON DELETE SET NULL",
    """CREATE OR REPLACE FUNCTION check_request_filled(shift_row shifts)
    RETURNS VOID AS $$
    DECLARE
        confirmed_cnt INTEGER;
        min_needed INTEGER;
        req_id INTEGER;
    BEGIN
        SELECT COALESCE(shift_row.min_staff, 1) INTO min_needed;
        SELECT COUNT(*) INTO confirmed_cnt
        FROM assignments a
        WHERE a.shift_id = shift_row.id AND a.status = 'confirmed';

        IF confirmed_cnt >= min_needed AND shift_row.request_id IS NOT NULL THEN
            UPDATE staffing_requests
            SET status = 'filled', updated_at = NOW()
            WHERE id = shift_row.request_id AND status = 'accepted';
        END IF;
    END;
    $$ LANGUAGE plpgsql""",

    # ── org seat billing columns ────────────────────────────────────────
    "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS seats INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS stripe_subscription_item_id VARCHAR(255)",
    "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMPTZ",
    "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS trial_end TIMESTAMPTZ",

    # ── phone column on users ───────────────────────────────────────────
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(50)",

    # ── deleted_at on shifts ────────────────────────────────────────────
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'shifts' AND column_name = 'deleted_at') THEN
            ALTER TABLE shifts ADD COLUMN deleted_at TIMESTAMPTZ;
        END IF;
    END $$""",
    "CREATE INDEX IF NOT EXISTS shifts_active_org_idx ON shifts (organization_id) WHERE deleted_at IS NULL",

    # ── make work_sites lat/lng nullable ────────────────────────────────
    "ALTER TABLE work_sites ALTER COLUMN latitude DROP NOT NULL",
    "ALTER TABLE work_sites ALTER COLUMN longitude DROP NOT NULL",

    # ── leads table ─────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS leads (
        id SERIAL PRIMARY KEY,
        email VARCHAR(255) NOT NULL,
        tool VARCHAR(100) NOT NULL,
        industry VARCHAR(100),
        staff_size INTEGER,
        calculated_cost DOUBLE PRECISION,
        pain_point VARCHAR(255),
        quiz_scores JSONB,
        extra_data JSONB,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS leads_email_tool_idx ON leads (LOWER(email), tool)",
    "CREATE INDEX IF NOT EXISTS leads_created_at_idx ON leads (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS leads_tool_idx ON leads (tool)",

    # ── sales tables ────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS sales_agents (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        email VARCHAR(255) NOT NULL UNIQUE,
        phone VARCHAR(50),
        territory VARCHAR(255),
        commission_rate_first_year NUMERIC(5,4) DEFAULT 0.3000,
        commission_rate_renewal NUMERIC(5,4) DEFAULT 0.1500,
        is_active BOOLEAN DEFAULT true,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS sales_inquiries (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        email VARCHAR(255) NOT NULL,
        phone VARCHAR(50),
        company_name VARCHAR(255),
        staff_count INTEGER,
        industry VARCHAR(100),
        message TEXT,
        territory VARCHAR(100),
        source VARCHAR(50) DEFAULT 'landing_page',
        page_url VARCHAR(1024),
        status VARCHAR(50) DEFAULT 'new',
        pipeline_stage VARCHAR(50) DEFAULT 'new',
        assigned_agent_id INTEGER,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS sales_activities (
        id SERIAL PRIMARY KEY,
        inquiry_id INTEGER REFERENCES sales_inquiries(id) ON DELETE CASCADE,
        agent_id INTEGER REFERENCES sales_agents(id),
        activity_type VARCHAR(50) NOT NULL,
        summary TEXT NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='organizations' AND column_name='sales_agent_id') THEN
            ALTER TABLE organizations ADD COLUMN sales_agent_id INTEGER;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='organizations' AND column_name='sales_agent_name') THEN
            ALTER TABLE organizations ADD COLUMN sales_agent_name VARCHAR(255);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='organizations' AND column_name='sales_agent_email') THEN
            ALTER TABLE organizations ADD COLUMN sales_agent_email VARCHAR(255);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='organizations' AND column_name='acquired_via') THEN
            ALTER TABLE organizations ADD COLUMN acquired_via VARCHAR(50) DEFAULT 'sales_agent';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='organizations' AND column_name='signed_up_at') THEN
            ALTER TABLE organizations ADD COLUMN signed_up_at TIMESTAMPTZ;
        END IF;
    END $$""",
    "CREATE INDEX IF NOT EXISTS sales_inquiries_email_idx ON sales_inquiries (LOWER(email))",
    "CREATE INDEX IF NOT EXISTS sales_inquiries_stage_idx ON sales_inquiries (pipeline_stage)",
    "CREATE INDEX IF NOT EXISTS sales_inquiries_agent_idx ON sales_inquiries (assigned_agent_id)",
    "CREATE INDEX IF NOT EXISTS sales_activities_inquiry_idx ON sales_activities (inquiry_id)",
    "CREATE INDEX IF NOT EXISTS orgs_sales_agent_idx ON organizations (sales_agent_id)",

    # ── audit_log ───────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS audit_log (
        id BIGSERIAL PRIMARY KEY,
        organization_id INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        action VARCHAR(100) NOT NULL,
        entity_type VARCHAR(50) NOT NULL,
        entity_id INTEGER,
        before JSONB,
        after JSONB,
        ip_address VARCHAR(45),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS audit_log_org_idx ON audit_log (organization_id)",
    "CREATE INDEX IF NOT EXISTS audit_log_created_at_idx ON audit_log (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS audit_log_entity_idx ON audit_log (entity_type, entity_id)",
    "CREATE INDEX IF NOT EXISTS audit_log_user_idx ON audit_log (user_id)",
    """CREATE OR REPLACE FUNCTION prevent_audit_log_modification()
    RETURNS TRIGGER AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only — UPDATE and DELETE are not permitted';
    END;
    $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS block_audit_log_update ON audit_log",
    """CREATE TRIGGER block_audit_log_update
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_log_modification()""",
]

MIGRATION_NAMES = [
    "add_admin_auth_tables",
    "create_core_scheduling_tables",
    "add_admin_magic_tokens_and_org_stripe",
    "add_client_magic_token_client_fields",
    "add_super_role",
    "add_password_resets",
    "add_org_external_id",
    "add_api_keys",
    "fix_staffing_requests_pending_to_open",
    "prevent_overlapping_assignments_trigger",
    "add_sessions_table",
    "add_page_views",
    "add_page_views_visitor_hash",
    "add_staff_address_column",
    "add_organization_certifications",
    "add_staff_certifications_cert_id",
    "drop_staff_skills_column",
    "add_shift_request_id_and_auto_fill",
    "add_org_seat_columns",
    "add_user_phone_column",
    "add_shift_deleted_at",
    "drop_work_sites_lat_lng_not_null",
    "add_leads_table",
    "add_sales_tables",
    "add_audit_log_table",
]


async def run_migrations(conn: asyncpg.Connection):
    """Run all migrations in order. Each statement is idempotent."""
    for sql in MIGRATION_SQL:
        await conn.execute(sql)

    for name in MIGRATION_NAMES:
        await conn.execute(
            "INSERT INTO _migrations (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
            name,
        )