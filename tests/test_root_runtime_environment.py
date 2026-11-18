"""A new Worker terminal can use the same root model settings as the Web app."""
from evoharness.__main__ import _load_runtime_environment


def test_root_dotenv_loads_without_overriding_explicit_environment(tmp_path,monkeypatch):
    path=tmp_path/'.env'
    path.write_text('APIKEY=file-only-key\nMODEL=file-model\n',encoding='utf-8')
    monkeypatch.delenv('EVOHARNESS_TEST_MODE',raising=False)
    monkeypatch.delenv('FEISHU_TEST_MODE',raising=False)
    monkeypatch.delenv('APIKEY',raising=False)
    monkeypatch.setenv('MODEL','explicit-model')
    _load_runtime_environment(path)
    import os
    assert os.environ['APIKEY']=='file-only-key'
    assert os.environ['MODEL']=='explicit-model'


def test_offline_cli_never_opens_dotenv(tmp_path,monkeypatch):
    import dotenv
    monkeypatch.setenv('EVOHARNESS_TEST_MODE','1')
    def forbidden(*args,**kwargs):
        raise AssertionError('offline tests must not read deployment .env')
    monkeypatch.setattr(dotenv,'load_dotenv',forbidden)
    _load_runtime_environment(tmp_path/'not-read.env')
