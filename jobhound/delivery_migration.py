"""Explicit local outbox migration; never called by opening or sending a trial."""
from .delivery_outbox import transaction, _SCHEMA


def migrate_unsent_generations(conn):
    """Preserve every old ID, membership and receipt; add reissue generations.

    SQLite needs a table rebuild to change the old uniqueness constraint. Keep
    foreign-key names pointing at delivery_intents during the atomic swap, then
    validate all references before committing. No run/subject/revision is edited.
    """
    if conn.in_transaction:
        raise ValueError('migrate outside a transaction')
    version = conn.execute("SELECT value FROM delivery_meta WHERE key='version'").fetchone()
    if version is None or version[0] not in {'1', '2'}:
        raise ValueError('unsupported delivery schema')
    if version[0] == '2':
        return False
    foreign_keys = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    legacy_alter = conn.execute('PRAGMA legacy_alter_table').fetchone()[0]
    conn.execute('PRAGMA foreign_keys=OFF')
    conn.execute('PRAGMA legacy_alter_table=ON')
    columns = 'id,revision,channel,destination,status,payload,attempts,max_attempts,available,token,lease_until'
    statement = _SCHEMA.split('CREATE TABLE delivery_intents(', 1)[1].split('CREATE TABLE delivery_attempt_events', 1)[0]
    try:
        with transaction(conn):
            conn.execute('ALTER TABLE delivery_intents RENAME TO delivery_intents_v1')
            conn.execute('CREATE TABLE delivery_intents(' + statement.strip())
            conn.execute(f'INSERT INTO delivery_intents({columns}) SELECT {columns} FROM delivery_intents_v1')
            conn.execute('DROP TABLE delivery_intents_v1')
            conn.execute('CREATE INDEX delivery_ready ON delivery_intents(status,available,id)')
            if conn.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('migration would break receipt references')
            conn.execute("UPDATE delivery_meta SET value='2' WHERE key='version'")
    finally:
        conn.execute(f'PRAGMA legacy_alter_table={legacy_alter}')
        conn.execute(f'PRAGMA foreign_keys={foreign_keys}')
    return True
