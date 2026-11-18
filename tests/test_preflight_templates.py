import re
from eval import git_preflight as preflight


def test_only_exact_environment_templates_are_allowed():
    for path in ('.env.example','config/.env.sample'):
        assert not any(re.search(rule,path) for rule in preflight.SENSITIVE_PATH)
    for path in ('.env','.env.example.live','config/.env.sample.bak'):
        assert any(re.search(rule,path) for rule in preflight.SENSITIVE_PATH)


def test_new_directory_children_and_ambiguous_names_are_all_scanned(monkeypatch,tmp_path):
    monkeypatch.setattr(preflight,'REPO',tmp_path)
    def git(*args):
        assert args==('status','--porcelain','-z','--untracked-files=all')
        return ' M 修改文件.py\0?? new/server.key\0?? new/a -> b.py\0?? new/ spaced .txt\0'
    monkeypatch.setattr(preflight,'git',git)
    assert {p.relative_to(tmp_path).as_posix() for p in preflight.candidate_files()}=={
        '修改文件.py','new/server.key','new/a -> b.py','new/ spaced .txt'}
