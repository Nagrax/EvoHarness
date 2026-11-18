"""Deployments can decrypt OAuth records using a key configured only in .env."""
from cryptography.fernet import Fernet
from app.core.config import Settings
from app.services import oauth


def test_dotenv_token_secret_is_used_without_environment_export(tmp_path, monkeypatch):
    key = Fernet.generate_key()
    path = tmp_path / '.env'
    path.write_text('FEISHU_TOKEN_SECRET=' + key.decode() + '\n', encoding='utf-8')
    monkeypatch.delenv('FEISHU_TOKEN_SECRET', raising=False)
    settings = Settings(_env_file=path)
    monkeypatch.setattr(oauth, '_settings', lambda: settings)
    encrypted = oauth.encrypt_token('test-user-token')
    assert Fernet(key).decrypt(encrypted.encode()) == b'test-user-token'
    assert key.decode() not in repr(settings)
