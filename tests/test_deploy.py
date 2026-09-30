from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_single_service_and_only_local_mounts() -> None:
    config = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text())
    assert set(config["services"]) == {"reader"} and "volumes" not in config
    service = config["services"]["reader"]
    assert service["command"] == ["python", "-m", "simpread"]
    assert service["env_file"] == ["../.env"]
    assert service["environment"]["READER_HOST"] == "0.0.0.0"
    assert service["ports"][0].startswith("127.0.0.1:")
    for mount in service["volumes"]:
        assert mount["type"] == "bind"
        assert (ROOT / "deploy" / mount["source"]).resolve() == ROOT / "data"
    assert service["healthcheck"]["test"] == ["CMD", "python", "-m", "simpread.healthcheck"]


def test_project_has_no_sibling_source_dependency() -> None:
    files = list((ROOT / "src").rglob("*.py"))
    for path in files:
        content = path.read_text()
        assert "from ext." not in content and "sys.path" not in content
    dockerfile = (ROOT / "deploy/Dockerfile").read_text()
    assert "COPY . ." not in dockerfile
    assert "--frozen --no-dev --no-editable" in dockerfile
    assert "**" in (ROOT / ".dockerignore").read_text()
