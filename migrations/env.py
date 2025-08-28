# migrations/env.py minimal stub for SQLite
from alembic import context
config = context.config

def run_migrations_online():
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_offline():
    context.configure(url=config.get_main_option('sqlalchemy.url'), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

run_migrations_offline()
