"""Guard new assignments in the database as well as in the service.

Revision ID: 0005
Revises: 0004

Migration 0004 made existing assignments immutable. This adds the rules for
creating one: only for a running experiment, only up to its planned days, and
only after every day already assigned, so a skipped day can never be filled in
and no day can be back-dated.
"""

from __future__ import annotations

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None

STATEMENTS = [
    """
    CREATE FUNCTION assignments_insert_guard() RETURNS trigger AS $$
    DECLARE
        exp experiments%ROWTYPE;
        latest date;
        assigned integer;
    BEGIN
        -- The row lock also keeps two concurrent inserts from both passing the checks.
        SELECT * INTO exp FROM experiments WHERE id = NEW.experiment_id FOR UPDATE;
        IF NOT FOUND OR exp.status <> 'active' OR exp.started_on IS NULL THEN
            RAISE EXCEPTION 'assignments can only be added to a running experiment';
        END IF;
        IF NEW.local_date < exp.started_on THEN
            RAISE EXCEPTION 'assignment on % is before the experiment starts', NEW.local_date;
        END IF;
        SELECT max(local_date), count(*) INTO latest, assigned
        FROM assignments WHERE experiment_id = NEW.experiment_id;
        IF assigned >= exp.planned_days THEN
            RAISE EXCEPTION 'experiment % already has its planned days assigned', exp.id;
        END IF;
        IF latest IS NOT NULL AND NEW.local_date <= latest THEN
            RAISE EXCEPTION 'assignment on % is not after the latest assigned day (%)',
                NEW.local_date, latest;
        END IF;
        RETURN NEW;
    END $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER assignments_insert_guard BEFORE INSERT ON assignments
    FOR EACH ROW EXECUTE FUNCTION assignments_insert_guard()
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TRIGGER assignments_insert_guard ON assignments")
    op.execute("DROP FUNCTION assignments_insert_guard()")
