from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine


def migration():
    path=Path(__file__).resolve().parents[1]/'migrations/versions/0130_retire_vantage_master.py'
    spec=spec_from_file_location('retired_master_migration',path)
    module=module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retires_only_the_exact_obsolete_master_and_preserves_current_accounts_and_history(monkeypatch):
    engine=create_engine('sqlite://')
    module=migration()
    with engine.begin() as conn:
        for table in ('mt5_accounts','mt5_account_profiles'):
            conn.exec_driver_sql(f'''CREATE TABLE {table} (id integer primary key,login text,broker text,
                server text,account_environment text,status text,is_active boolean,last_error_code text,updated_at text)''')
            values=[(1,'25911913','vantage','VantageMarkets-Demo','demo'),
                    (2,'700207861','puprime','PUPrime-Demo','demo'),
                    (3,'700203288','puprime','PUPrime-Demo','demo'),
                    (4,'61613731','pepperstone','Pepperstone-Demo','demo'),
                    (5,'25911913','vantage','Different-Server','demo')]
            for row in values:
                conn.exec_driver_sql(f'INSERT INTO {table} VALUES (?,?,?,?,?,\'connected\',true,NULL,NULL)',row)
        conn.exec_driver_sql('CREATE TABLE broker_deals (id integer,mt5_account_id integer,profit real)')
        conn.exec_driver_sql('INSERT INTO broker_deals VALUES (77,1,12.34)')
        monkeypatch.setattr(module,'op',SimpleNamespace(execute=conn.exec_driver_sql))
        module.upgrade()
        for table in ('mt5_accounts','mt5_account_profiles'):
            rows=conn.exec_driver_sql(f'SELECT id,status,is_active,last_error_code FROM {table} ORDER BY id').all()
            assert rows[0][1]=='revoked'
            assert rows[0][3]=='retired_master_25911913'
            if table=='mt5_account_profiles':assert rows[0][2]==0
            assert all(row[1]=='connected' for row in rows[1:])
        assert conn.exec_driver_sql('SELECT * FROM broker_deals').all()==[(77,1,12.34)]
        module.upgrade()
        module.downgrade()
        assert conn.exec_driver_sql('SELECT status FROM mt5_accounts WHERE id=1').scalar_one()=='revoked'
        assert conn.exec_driver_sql('SELECT is_active FROM mt5_account_profiles WHERE id=1').scalar_one()==0
    engine.dispose()


def test_owner_demo_feed_is_independent_of_retired_connection_profiles():
    import inspect
    from app.routes.local_bridge import owner_demo_700207861_feed
    source=inspect.getsource(owner_demo_700207861_feed)
    assert 'JOIN sources' in source
    assert 'mt5_account_profiles' not in source
    assert 'mt5_accounts' not in source
