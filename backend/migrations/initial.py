"""Install the initial laboratory schema on an empty MySQL database."""
from sqlalchemy import inspect, text

from src import models
from src.db import engine

# Columns added after the first environment snapshot; create_all only creates
# missing tables, so existing volumes need these idempotent ALTER statements.
_ADDED_COLUMNS = {
    'recipe_lots': [('trial_opened_at', 'DATETIME NULL')],
}


def ensure_added_columns():
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing:
                continue
            present = {column['name'] for column in inspector.get_columns(table)}
            for name, ddl in columns:
                if name not in present:
                    connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))


if __name__ == '__main__':
    models.Base.metadata.create_all(engine)
    ensure_added_columns()
