-- Append-only audit chain enforcement.
-- A row's `row_hash` is sha256(prev_hash || canonical(payload) || occurred_at).
-- The chain is verifiable by walking rows in occurred_at order.

CREATE OR REPLACE FUNCTION audit_log_chain() RETURNS TRIGGER AS $$
DECLARE
    prev TEXT;
BEGIN
    SELECT row_hash INTO prev
    FROM audit_log
    WHERE id < NEW.id
    ORDER BY id DESC
    LIMIT 1;
    NEW.prev_hash := prev;
    NEW.row_hash := encode(digest(
        coalesce(prev, '') ||
        NEW.tenant_id || '|' ||
        NEW.automaton_id || '|' ||
        NEW.actor_kind || '|' ||
        NEW.action || '|' ||
        COALESCE(NEW.target_id, '') || '|' ||
        NEW.payload::text || '|' ||
        NEW.occurred_at::text,
        'sha256'
    ), 'hex');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_chain ON audit_log;
CREATE TRIGGER audit_log_chain
    BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_chain();

-- Disallow UPDATE/DELETE on audit_log
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
