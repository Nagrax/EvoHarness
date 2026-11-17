"""Legacy migration adapter; the product core owns the schema and migrations."""
from evoharness.migrations import SCHEMA_VERSION, MigrationContext
from evoharness import migrations as _core
from app.core.config import get_settings

def _context():
    settings = get_settings()
    return MigrationContext(tenant_id=settings.tenant_id, owner_id=settings.feishu_owner_open_id,
                            backup_keep=settings.migration_backup_keep or 3)

def migrate(engine, *, backup_dir=None):
    return _core.migrate(engine, backup_dir=backup_dir, context=_context())

def backup_database(url, *, directory=None):
    return _core.backup_database(url, directory=directory, keep=_context().backup_keep)

def ensure_schema(engine):
    return migrate(engine)
