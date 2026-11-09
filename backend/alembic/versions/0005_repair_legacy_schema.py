"""Repair clean bootstrap without rewriting existing history.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '0005'
down_revision = '0004'
branch_labels = depends_on = None


def _ensure_table(name, columns):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if name not in inspector.get_table_names():
        op.create_table(name, *columns)
    # Existing drift must not be silently called a successful repair.
    actual = {c['name']: c for c in sa.inspect(bind).get_columns(name)}
    for column in columns:
        if not isinstance(column, sa.Column):
            continue
        if column.name not in actual:
            raise RuntimeError('Existing table %s is missing required column %s' % (name, column.name))
        observed = actual[column.name]['type']
        if observed._type_affinity is not column.type._type_affinity:
            raise RuntimeError('Incompatible type for %s.%s' % (name, column.name))
        expected_length = getattr(column.type, 'length', None)
        if expected_length is not None and getattr(observed, 'length', None) != expected_length:
            raise RuntimeError('Incompatible length for %s.%s' % (name, column.name))


def _index(table, name, columns):
    if name not in {x['name'] for x in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns)


def upgrade():
    _ensure_table('users', [
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('username', sa.String(50), nullable=False, unique=True),
        sa.Column('password_hash', sa.String(255), nullable=False), sa.Column('email', sa.String(255)),
        sa.Column('role', sa.String(20), server_default=sa.text("'operator'")),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'))])
    _ensure_table('settings', [
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('key', sa.String(100), nullable=False, unique=True), sa.Column('value', sa.Text()),
        sa.Column('description', sa.String(255)),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'))])
    _ensure_table('trusted_emails', [
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('email', sa.String(255), nullable=False, unique=True), sa.Column('description', sa.String(255)),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'))])
    for table, name, cols in [('users','idx_users_username',['username']),('users','idx_users_active',['is_active']),
                             ('trusted_emails','idx_trusted_emails_active',['is_active'])]:
        _index(table, name, cols)
    columns = {x['name']:x for x in sa.inspect(op.get_bind()).get_columns('objects')}
    for name in ['eldis_id', 'calculator_number']:
        if name not in columns:
            op.add_column('objects', sa.Column(name, sa.String(50)))
        elif getattr(columns[name]['type'], 'length', None) != 50:
            raise RuntimeError('Incompatible objects.%s' % name)
    op.alter_column('objects', 'object_date', type_=sa.String(50), existing_type=columns['object_date']['type'])
    for item in sa.inspect(op.get_bind()).get_unique_constraints('objects'):
        if item['column_names'] == ['name_norm']:
            op.drop_constraint(item['name'], 'objects', type_='unique')
    uniques = sa.inspect(op.get_bind()).get_unique_constraints('objects')
    if not any(x['column_names']==['calculator_number'] for x in uniques):
        op.create_unique_constraint('objects_calculator_number_key','objects',['calculator_number'])
    _index('objects','idx_objects_calculator',['calculator_number'])


def downgrade():
    # Repair is deliberately additive: these objects already existed in live 0004.
    # Dropping them on rollback would erase users/settings/history. Old code accepts
    # the repaired schema. Pipeline 0006 has a separate guarded empty-data rollback.
    pass
