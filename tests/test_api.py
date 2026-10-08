import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import mysql

from backend.app import Base, User, Entry, AuthSession, create_app, now


@pytest.fixture
def app(tmp_path):
    app = create_app({'TESTING': True, 'DATABASE_URL': f'sqlite:///{tmp_path / "test.db"}',
                      'COOKIE_SECURE': False, 'APP_ORIGIN': 'http://localhost:8000'})
    Base.metadata.create_all(app.extensions['engine'])
    yield app
    app.extensions['engine'].dispose()


def register(client, name='Alice', email='alice@example.test'):
    response = client.post('/api/auth/register', json={'name': name, 'email': email,
                           'password': 'A strong test password 123!'})
    assert response.status_code == 200, response.json
    return response.json['csrf']


def write(client, token, path='/api/entries', data=None, key=None):
    return client.post(path, json=data or {'kind': 'income', 'amount': '100.00', 'category': 'Trabalho', 'description': 'Projeto'},
                       headers={'X-CSRF-Token': token, 'Idempotency-Key': key or str(uuid.uuid4())})


def test_register_hash_private_sessions_and_logout(app):
    client = app.test_client()
    token = register(client)
    with Session(app.extensions['engine']) as db:
        user = db.scalar(select(User))
        assert user.password_hash.startswith('$argon2id$')
        assert 'test password' not in user.password_hash
    assert client.get('/api/auth/me').json['user']['balance_cents'] == 0
    assert client.get_cookie('aurora_session').http_only
    assert client.post('/api/auth/logout', json={}, headers={'X-CSRF-Token': token}).status_code == 200
    assert client.get('/api/auth/me').status_code == 401
    assert client.post('/api/auth/login', json={'email':'alice@example.test','password':'incorrect'}).status_code == 401
    response = client.post('/api/auth/login', json={'email':'ALICE@example.test','password':'A strong test password 123!', 'remember':True})
    assert response.status_code == 200
    assert 'Max-Age=2592000' in response.headers['Set-Cookie']


def test_data_persists_after_application_restart(app):
    client = app.test_client(); token = register(client)
    assert write(client, token).status_code == 201
    second = create_app({'TESTING':True, 'DATABASE_URL':str(app.extensions['engine'].url), 'COOKIE_SECURE':False})
    visitor = second.test_client()
    response = visitor.post('/api/auth/login',json={'email':'alice@example.test','password':'A strong test password 123!'})
    assert response.status_code == 200
    assert visitor.get('/api/dashboard').json['user']['balance_cents'] == 10000
    assert len(visitor.get('/api/dashboard').json['entries']) == 1
    second.extensions['engine'].dispose()


def test_transfer_atomic_ledger_and_user_isolation(app):
    alice, bob = app.test_client(), app.test_client()
    a = register(alice); register(bob,'Bob','bob@example.test')
    assert write(alice,a).status_code == 201
    assert bob.get('/api/dashboard').json['entries'] == []
    response = write(alice,a,'/api/transfers',{'email':'bob@example.test','amount':'20.35'})
    assert response.status_code == 201
    assert alice.get('/api/dashboard').json['user']['balance_cents'] == 7965
    assert bob.get('/api/dashboard').json['user']['balance_cents'] == 2035
    with Session(app.extensions['engine']) as db:
        pair = db.scalars(select(Entry).where(Entry.transfer_id == response.json['id'])).all()
        assert len(pair) == 2
        assert sum(e.amount_cents for e in pair) == 0
    # Client-provided user IDs never select a different account.
    assert bob.get('/api/dashboard?user_id=1').json['user']['name'] == 'Bob'


def test_insufficient_funds_leave_both_accounts_unchanged(app):
    a,b = app.test_client(),app.test_client(); token=register(a);register(b,'Bob','bob@example.test')
    response=write(a,token,'/api/transfers',{'email':'bob@example.test','amount':'0.01'})
    assert response.status_code == 409
    assert a.get('/api/dashboard').json['entries'] == []
    assert b.get('/api/dashboard').json['entries'] == []


def test_idempotency_and_conflicting_payload(app):
    c=app.test_client();token=register(c);key=str(uuid.uuid4())
    first=write(c,token,key=key); second=write(c,token,key=key)
    assert first.json['id']==second.json['id']
    assert c.get('/api/dashboard').json['user']['balance_cents']==10000
    data={'kind':'income','amount':'200.00','category':'Trabalho','description':'Projeto'}
    assert write(c,token,data=data,key=key).status_code==409


def test_duplicate_transfer_not_charged_twice(app):
    a,b=app.test_client(),app.test_client();token=register(a);register(b,'Bob','bob@example.test');write(a,token)
    data={'email':'bob@example.test','amount':'10.00'};key=str(uuid.uuid4())
    assert write(a,token,'/api/transfers',data,key).status_code==201
    assert write(a,token,'/api/transfers',data,key).json['duplicate'] is True
    assert b.get('/api/dashboard').json['user']['balance_cents']==1000


@pytest.mark.parametrize('value',['0','-1','NaN','1.999','1e3',12,None,'1000000.01'])
def test_money_validation(app,value):
    c=app.test_client();token=register(c)
    assert write(c,token,data={'kind':'income','amount':value,'category':'Trabalho','description':'Projeto'}).status_code==400
    assert c.get('/api/dashboard').json['user']['balance_cents']==0


def test_csrf_origin_and_unauthenticated_requests(app):
    c=app.test_client()
    assert c.get('/api/dashboard').status_code==401
    token=register(c)
    assert write(c,'wrong').status_code==403
    assert c.post('/api/auth/logout',json={},headers={'X-CSRF-Token':token,'Origin':'https://evil.test'}).status_code==403
    assert c.post('/api/auth/login',data='email=x').status_code==415


def test_expired_session(app):
    c=app.test_client();register(c)
    with Session(app.extensions['engine']) as db:
        session=db.scalar(select(AuthSession));session.expires_at=now()-timedelta(seconds=1);db.commit()
    assert c.get('/api/auth/me').status_code==401


def test_rate_limit(app):
    c=app.test_client()
    for _ in range(10):
        assert c.post('/api/auth/login',json={'email':'unknown@example.test','password':'wrong'}).status_code==401
    assert c.post('/api/auth/login',json={'email':'unknown@example.test','password':'wrong'}).status_code==429


def test_export_and_month_filter(app):
    c=app.test_client();token=register(c)
    assert write(c,token,data={'kind':'income','amount':'10.25','category':'Outros','description':'=HYPERLINK("bad")'}).status_code==201
    export=c.get('/api/export');assert export.status_code==200
    assert "'=HYPERLINK" in export.data.decode('utf-8')
    assert c.get('/api/dashboard?month=2020-01').json['entries']==[]
    assert c.get('/api/dashboard?month=2026-99').status_code==400


def test_mysql_schema_compiles():
    for table in Base.metadata.sorted_tables:
        sql=str(CreateTable(table).compile(dialect=mysql.dialect()))
        assert 'CREATE TABLE' in sql


def test_production_requires_mysql():
    with pytest.raises(RuntimeError):
        create_app({'DATABASE_URL':'sqlite:///:memory:'})
