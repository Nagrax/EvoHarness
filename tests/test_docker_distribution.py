"""发布镜像必须携带依赖闭包和根工作台所需的产品包。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _copy_sources_before(marker=None):
    sources = set()
    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if marker and marker in line:
            break
        fields = line.split()
        if fields and fields[0].upper() == "COPY":
            sources.update(fields[1:-1])
    return sources


def test_requirements_includes_are_available_at_install_time():
    copied = _copy_sources_before("pip install --no-cache-dir -r requirements.txt")
    pending = [Path("requirements.txt")]
    seen = set()
    while pending:
        relative = pending.pop()
        if relative in seen:
            continue
        seen.add(relative)
        assert relative.as_posix() in copied, f"Docker dependency input missing: {relative}"
        for line in (ROOT / relative).read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("-r "):
                pending.append(relative.parent / line.strip()[3:].strip())


def test_root_api_and_worker_packages_are_distributed():
    copied = _copy_sources_before()
    for package in ("agents", "server", "evoharness", "ops"):
        assert package in copied, f"Docker runtime package missing: {package}"
        assert (ROOT / package / "__init__.py").is_file()


def test_optional_channels_share_the_complete_product_image():
    copied = _copy_sources_before()
    assert {"integration", "feishu-daily-agent"} <= copied
    before_install = _copy_sources_before("pip install --no-cache-dir -r requirements.txt")
    assert "feishu-daily-agent/requirements.txt" in before_install
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "image: evoharness:latest" in compose
    assert compose.count("<<: *product") == 4
    assert "EVOHARNESS_DATABASE_URL: sqlite:////data/org.db" in compose
    assert "DATABASE_URL: sqlite:////data/org.db" in compose
    assert "./data:/data" in compose
    assert "entrypoint: [python, -m, evoharness]" in compose
    assert "working_dir: /app/feishu-daily-agent" in compose
