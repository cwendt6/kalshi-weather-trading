"""
Tests for Docker configuration validation.

These tests verify that Docker configuration files are valid
and properly structured without actually building containers.
"""
import os
import re
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).parent.parent


class TestDockerfile:
    """Tests for Dockerfile validation."""

    @pytest.fixture
    def dockerfile_content(self):
        """Load Dockerfile content."""
        dockerfile_path = PROJECT_ROOT / "Dockerfile"
        if not dockerfile_path.exists():
            pytest.skip("Dockerfile not found")
        return dockerfile_path.read_text()

    def test_dockerfile_exists(self):
        """Test Dockerfile exists."""
        assert (PROJECT_ROOT / "Dockerfile").exists()

    def test_dockerfile_has_from(self, dockerfile_content):
        """Test Dockerfile has FROM instruction."""
        assert "FROM" in dockerfile_content

    def test_dockerfile_uses_python_310(self, dockerfile_content):
        """Test Dockerfile uses Python 3.10."""
        assert "python:3.10" in dockerfile_content

    def test_dockerfile_has_workdir(self, dockerfile_content):
        """Test Dockerfile sets WORKDIR."""
        assert "WORKDIR" in dockerfile_content

    def test_dockerfile_has_copy(self, dockerfile_content):
        """Test Dockerfile has COPY instructions."""
        assert "COPY" in dockerfile_content

    def test_dockerfile_has_run(self, dockerfile_content):
        """Test Dockerfile has RUN instructions."""
        assert "RUN" in dockerfile_content

    def test_dockerfile_has_cmd_or_entrypoint(self, dockerfile_content):
        """Test Dockerfile has CMD or ENTRYPOINT."""
        assert "CMD" in dockerfile_content or "ENTRYPOINT" in dockerfile_content

    def test_dockerfile_multistage_build(self, dockerfile_content):
        """Test Dockerfile uses multi-stage build."""
        # Should have multiple FROM statements
        from_count = len(re.findall(r"^FROM\s+", dockerfile_content, re.MULTILINE))
        assert from_count >= 2, "Expected multi-stage build"

    def test_dockerfile_has_production_stage(self, dockerfile_content):
        """Test Dockerfile has production stage."""
        assert "as production" in dockerfile_content.lower() or "AS production" in dockerfile_content

    def test_dockerfile_has_development_stage(self, dockerfile_content):
        """Test Dockerfile has development stage."""
        assert "as development" in dockerfile_content.lower() or "AS development" in dockerfile_content

    def test_dockerfile_non_root_user(self, dockerfile_content):
        """Test Dockerfile creates non-root user."""
        assert "useradd" in dockerfile_content or "adduser" in dockerfile_content

    def test_dockerfile_has_healthcheck(self, dockerfile_content):
        """Test Dockerfile has healthcheck."""
        assert "HEALTHCHECK" in dockerfile_content

    def test_dockerfile_no_secrets(self, dockerfile_content):
        """Test Dockerfile doesn't contain secrets."""
        # Check for common secret patterns
        secret_patterns = [
            r"password\s*=\s*['\"][^'\"]+['\"]",
            r"api_key\s*=\s*['\"][^'\"]+['\"]",
            r"secret\s*=\s*['\"][^'\"]+['\"]",
        ]
        for pattern in secret_patterns:
            matches = re.findall(pattern, dockerfile_content, re.IGNORECASE)
            assert len(matches) == 0, f"Found potential secret: {matches}"


class TestDockerCompose:
    """Tests for docker-compose.yml validation."""

    @pytest.fixture
    def compose_content(self):
        """Load docker-compose.yml content."""
        compose_path = PROJECT_ROOT / "docker-compose.yml"
        if not compose_path.exists():
            pytest.skip("docker-compose.yml not found")
        return compose_path.read_text()

    @pytest.fixture
    def compose_data(self, compose_content):
        """Parse docker-compose.yml."""
        return yaml.safe_load(compose_content)

    def test_compose_file_exists(self):
        """Test docker-compose.yml exists."""
        assert (PROJECT_ROOT / "docker-compose.yml").exists()

    def test_compose_valid_yaml(self, compose_content):
        """Test docker-compose.yml is valid YAML."""
        try:
            yaml.safe_load(compose_content)
        except yaml.YAMLError as e:
            pytest.fail(f"Invalid YAML: {e}")

    def test_compose_has_version(self, compose_data):
        """Test docker-compose has version."""
        assert "version" in compose_data

    def test_compose_has_services(self, compose_data):
        """Test docker-compose has services."""
        assert "services" in compose_data
        assert len(compose_data["services"]) > 0

    def test_compose_has_trading_service(self, compose_data):
        """Test docker-compose has trading service."""
        services = compose_data["services"]
        trading_services = [s for s in services if "trading" in s.lower()]
        assert len(trading_services) > 0, "Expected trading service"

    def test_compose_has_test_service(self, compose_data):
        """Test docker-compose has test service."""
        services = compose_data["services"]
        assert "test" in services, "Expected test service"

    def test_compose_has_dev_service(self, compose_data):
        """Test docker-compose has dev service."""
        services = compose_data["services"]
        assert "dev" in services, "Expected dev service"

    def test_compose_uses_profiles(self, compose_data):
        """Test docker-compose uses profiles."""
        services = compose_data["services"]
        profiles_found = False
        for service in services.values():
            if "profiles" in service:
                profiles_found = True
                break
        assert profiles_found, "Expected profiles for service organization"

    def test_compose_has_volumes(self, compose_data):
        """Test docker-compose has volumes defined."""
        assert "volumes" in compose_data
        assert len(compose_data["volumes"]) > 0

    def test_compose_has_networks(self, compose_data):
        """Test docker-compose has networks defined."""
        assert "networks" in compose_data
        assert len(compose_data["networks"]) > 0

    def test_compose_environment_uses_variables(self, compose_data):
        """Test services use environment variables."""
        services = compose_data["services"]
        for name, service in services.items():
            if "environment" in service:
                env = service["environment"]
                # Check that sensitive values use variable substitution
                for item in env:
                    if isinstance(item, str):
                        # Format: KEY=value
                        if "api_key" in item.lower() or "secret" in item.lower():
                            assert "${" in item or ":-" in item, f"Service {name} should use env var substitution for sensitive values"

    def test_compose_data_persistence(self, compose_data):
        """Test data volumes are persisted."""
        services = compose_data["services"]
        volumes = compose_data.get("volumes", {})

        # Check trading services have persistent volumes
        for name, service in services.items():
            if "trading" in name:
                service_volumes = service.get("volumes", [])
                has_data_volume = any("data" in str(v) for v in service_volumes)
                assert has_data_volume, f"Service {name} should have data volume"

    def test_compose_no_hardcoded_secrets(self, compose_content):
        """Test no hardcoded secrets in compose file."""
        # Check for common secret patterns (actual values, not variables)
        lines = compose_content.split("\n")
        for line in lines:
            line_lower = line.lower()
            # Skip comments
            if line.strip().startswith("#"):
                continue
            # Check for hardcoded credentials
            if any(word in line_lower for word in ["api_key", "secret", "password"]):
                # Should use variable substitution
                if "=" in line:
                    value = line.split("=", 1)[1].strip()
                    # Value should be empty, a variable, or use default syntax
                    assert value == "" or "${" in value or ":-" in value or value.startswith("$"), \
                        f"Possible hardcoded secret: {line}"


class TestDockerIgnore:
    """Tests for .dockerignore validation."""

    @pytest.fixture
    def dockerignore_content(self):
        """Load .dockerignore content."""
        path = PROJECT_ROOT / ".dockerignore"
        if not path.exists():
            pytest.skip(".dockerignore not found")
        return path.read_text()

    def test_dockerignore_exists(self):
        """Test .dockerignore exists."""
        assert (PROJECT_ROOT / ".dockerignore").exists()

    def test_ignores_git(self, dockerignore_content):
        """Test .dockerignore ignores .git."""
        assert ".git" in dockerignore_content

    def test_ignores_pycache(self, dockerignore_content):
        """Test .dockerignore ignores __pycache__."""
        assert "__pycache__" in dockerignore_content

    def test_ignores_env_files(self, dockerignore_content):
        """Test .dockerignore ignores .env files."""
        assert ".env" in dockerignore_content

    def test_ignores_venv(self, dockerignore_content):
        """Test .dockerignore ignores virtual environments."""
        assert "venv" in dockerignore_content.lower() or ".venv" in dockerignore_content

    def test_ignores_pytest_cache(self, dockerignore_content):
        """Test .dockerignore ignores pytest cache."""
        assert ".pytest_cache" in dockerignore_content

    def test_ignores_mypy_cache(self, dockerignore_content):
        """Test .dockerignore ignores mypy cache."""
        assert ".mypy_cache" in dockerignore_content

    def test_ignores_db_files(self, dockerignore_content):
        """Test .dockerignore ignores database files."""
        assert "*.db" in dockerignore_content or "*.sqlite" in dockerignore_content

    def test_ignores_secrets(self, dockerignore_content):
        """Test .dockerignore ignores secret files."""
        # Should ignore .pem files
        assert "*.pem" in dockerignore_content or ".pem" in dockerignore_content


class TestEnvExample:
    """Tests for .env.example validation."""

    @pytest.fixture
    def env_example_content(self):
        """Load .env.example content."""
        path = PROJECT_ROOT / ".env.example"
        if not path.exists():
            pytest.skip(".env.example not found")
        return path.read_text()

    def test_env_example_exists(self):
        """Test .env.example exists."""
        assert (PROJECT_ROOT / ".env.example").exists()

    def test_has_api_key_placeholder(self, env_example_content):
        """Test .env.example has API key placeholder."""
        assert "KALSHI_API_KEY" in env_example_content

    def test_has_database_url(self, env_example_content):
        """Test .env.example has database URL."""
        assert "DATABASE_URL" in env_example_content

    def test_has_log_level(self, env_example_content):
        """Test .env.example has log level."""
        assert "LOG_LEVEL" in env_example_content

    def test_has_paper_trading(self, env_example_content):
        """Test .env.example has paper trading option."""
        content_lower = env_example_content.lower()
        assert "paper" in content_lower

    def test_no_real_secrets(self, env_example_content):
        """Test .env.example doesn't contain real secrets."""
        lines = env_example_content.split("\n")
        for line in lines:
            if line.startswith("#") or not line.strip():
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                # Strip inline comments (e.g. "value  # comment")
                if "#" in value:
                    value = value[:value.index("#")]
                value = value.strip()
                # Values should be empty or placeholder
                if any(word in key.lower() for word in ["key", "secret", "password", "token"]):
                    # Should be placeholder or empty or path placeholder
                    is_placeholder = (
                        value == "" or
                        "your_" in value.lower() or
                        "your/" in value.lower() or  # Path placeholder like /path/to/your/
                        "placeholder" in value.lower() or
                        value.startswith("$") or
                        "here" in value.lower()
                    )
                    assert is_placeholder, f"Possible real secret in .env.example: {key}"
