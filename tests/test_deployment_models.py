"""Unit tests for deployment state and reporting models."""

import pytest
from datetime import datetime, timedelta

from ilha.commands.push.deployment_models import (
    DeploymentPhase,
    PhaseStatus,
    PhaseResult,
    HealthCheckResult,
    DeploymentReport,
    EnvironmentHealthReport,
    DeploymentMetrics,
    calculate_deployment_metrics,
)


class TestPhaseResult:
    """Test PhaseResult model."""

    def test_create_pending_phase(self):
        """Test creating pending phase."""
        result = PhaseResult(phase=DeploymentPhase.PREFLIGHT, status=PhaseStatus.PENDING)
        assert result.phase == DeploymentPhase.PREFLIGHT
        assert result.status == PhaseStatus.PENDING
        assert not result.is_terminal()

    def test_create_successful_phase(self):
        """Test creating successful phase."""
        now = datetime.utcnow()
        result = PhaseResult(
            phase=DeploymentPhase.BUILD,
            status=PhaseStatus.SUCCESS,
            started_at=now,
            completed_at=now + timedelta(seconds=30),
        )
        assert result.is_success()
        assert result.is_terminal()
        assert result.duration() == 30.0

    def test_create_failed_phase(self):
        """Test creating failed phase."""
        result = PhaseResult(
            phase=DeploymentPhase.DEPLOY,
            status=PhaseStatus.FAILED,
            error_message="Connection timeout",
            exit_code=1,
        )
        assert result.is_failed()
        assert result.error_message == "Connection timeout"
        assert result.exit_code == 1

    def test_duration_calculation(self):
        """Test phase duration calculation."""
        start = datetime(2024, 1, 1, 10, 0, 0)
        end = datetime(2024, 1, 1, 10, 1, 30)
        result = PhaseResult(
            phase=DeploymentPhase.PUSH,
            status=PhaseStatus.SUCCESS,
            started_at=start,
            completed_at=end,
        )
        assert result.duration() == 90.0

    def test_duration_none_if_not_completed(self):
        """Test duration returns None if not completed."""
        result = PhaseResult(
            phase=DeploymentPhase.PREFLIGHT,
            status=PhaseStatus.IN_PROGRESS,
            started_at=datetime.utcnow(),
        )
        assert result.duration() is None

    def test_metadata_field(self):
        """Test metadata field storage."""
        result = PhaseResult(
            phase=DeploymentPhase.MIGRATE,
            status=PhaseStatus.SUCCESS,
            metadata={"migrations_applied": 5, "rows_affected": 1000},
        )
        assert result.metadata["migrations_applied"] == 5
        assert result.metadata["rows_affected"] == 1000


class TestHealthCheckResult:
    """Test HealthCheckResult model."""

    def test_healthy_check(self):
        """Test healthy health check."""
        check = HealthCheckResult(
            service_name="web",
            endpoint="http://localhost:8000/health",
            status_code=200,
            response_time_ms=45.5,
            is_healthy=True,
        )
        assert check.is_healthy
        assert check.status_code == 200

    def test_unhealthy_check(self):
        """Test unhealthy health check."""
        check = HealthCheckResult(
            service_name="db",
            endpoint="localhost:5432",
            is_healthy=False,
            error="Connection refused",
        )
        assert not check.is_healthy
        assert check.error == "Connection refused"

    def test_check_with_timestamp(self):
        """Test health check with timestamp."""
        now = datetime.utcnow()
        check = HealthCheckResult(
            service_name="cache",
            endpoint="redis:6379",
            is_healthy=True,
            checked_at=now,
        )
        assert check.checked_at == now


class TestDeploymentReport:
    """Test DeploymentReport model."""

    def test_create_new_report(self):
        """Test creating new deployment report."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        assert report.droplet_name == "staging"
        assert report.branch == "main"
        assert report.overall_status == PhaseStatus.IN_PROGRESS
        assert len(report.deployment_id) > 0  # UUID generated

    def test_add_phase(self):
        """Test adding phases to report."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        result = PhaseResult(
            phase=DeploymentPhase.PREFLIGHT, status=PhaseStatus.SUCCESS
        )
        report.add_phase(DeploymentPhase.PREFLIGHT, result)

        assert DeploymentPhase.PREFLIGHT in report.phases
        assert report.phases[DeploymentPhase.PREFLIGHT].status == PhaseStatus.SUCCESS

    def test_add_failed_phase_updates_status(self):
        """Test that adding failed phase updates overall status."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        failed_result = PhaseResult(
            phase=DeploymentPhase.DEPLOY,
            status=PhaseStatus.FAILED,
            error_message="Connection failed",
        )
        report.add_phase(DeploymentPhase.DEPLOY, failed_result)

        assert report.overall_status == PhaseStatus.FAILED

    def test_mark_complete_success(self):
        """Test marking deployment as successfully complete."""
        start = datetime.utcnow()
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=start,
        )
        report.mark_complete(PhaseStatus.SUCCESS)

        assert report.overall_status == PhaseStatus.SUCCESS
        assert report.completed_at is not None
        assert report.total_duration_seconds is not None
        assert report.total_duration_seconds >= 0

    def test_mark_complete_failed(self):
        """Test marking deployment as failed."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        report.mark_complete(PhaseStatus.FAILED)

        assert report.overall_status == PhaseStatus.FAILED
        assert report.completed_at is not None

    def test_get_failed_phases(self):
        """Test retrieving failed phases."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        report.add_phase(
            DeploymentPhase.BUILD,
            PhaseResult(phase=DeploymentPhase.BUILD, status=PhaseStatus.SUCCESS),
        )
        report.add_phase(
            DeploymentPhase.DEPLOY,
            PhaseResult(phase=DeploymentPhase.DEPLOY, status=PhaseStatus.FAILED),
        )

        failed = report.get_failed_phases()
        assert len(failed) == 1
        assert DeploymentPhase.DEPLOY in failed

    def test_get_successful_phases(self):
        """Test retrieving successful phases."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        report.add_phase(
            DeploymentPhase.PREFLIGHT,
            PhaseResult(phase=DeploymentPhase.PREFLIGHT, status=PhaseStatus.SUCCESS),
        )
        report.add_phase(
            DeploymentPhase.BUILD,
            PhaseResult(phase=DeploymentPhase.BUILD, status=PhaseStatus.SUCCESS),
        )

        successful = report.get_successful_phases()
        assert len(successful) == 2

    def test_get_skipped_phases(self):
        """Test retrieving skipped phases."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        report.add_phase(
            DeploymentPhase.BACKUP,
            PhaseResult(phase=DeploymentPhase.BACKUP, status=PhaseStatus.SKIPPED),
        )

        skipped = report.get_skipped_phases()
        assert len(skipped) == 1
        assert DeploymentPhase.BACKUP in skipped

    def test_summary_generation(self):
        """Test human-readable summary generation."""
        report = DeploymentReport(
            droplet_name="production",
            branch="release/1.0",
            initiated_at=datetime.utcnow(),
        )
        report.add_phase(
            DeploymentPhase.PREFLIGHT,
            PhaseResult(phase=DeploymentPhase.PREFLIGHT, status=PhaseStatus.SUCCESS),
        )
        report.mark_complete(PhaseStatus.SUCCESS)

        summary = report.summary()
        assert "production" in summary
        assert "release/1.0" in summary
        assert "success" in summary
        assert "1/1" in summary

    def test_short_status(self):
        """Test short status generation."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        report.mark_complete(PhaseStatus.SUCCESS)

        status = report.short_status()
        assert "✅" in status
        assert "staging" in status
        assert "main" in status

    def test_phase_summary_table(self):
        """Test phase summary table generation."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        now = datetime.utcnow()
        report.add_phase(
            DeploymentPhase.PREFLIGHT,
            PhaseResult(
                phase=DeploymentPhase.PREFLIGHT,
                status=PhaseStatus.SUCCESS,
                started_at=now,
                completed_at=now + timedelta(seconds=10),
            ),
        )

        table = report.phase_summary_table()
        assert "preflight" in table
        assert "success" in table

    def test_add_health_checks(self):
        """Test adding health checks to report."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        report.health_checks.append(
            HealthCheckResult(
                service_name="web",
                endpoint="localhost:8000",
                is_healthy=True,
            )
        )
        assert len(report.health_checks) == 1
        assert report.health_checks[0].service_name == "web"


class TestEnvironmentHealthReport:
    """Test EnvironmentHealthReport model."""

    def test_healthy_environment(self):
        """Test reporting healthy environment."""
        report = EnvironmentHealthReport(
            droplet_id=12345,
            droplet_status="active",
            docker_daemon_running=True,
            compose_version="2.20.0",
            services_running=["web", "db", "cache"],
            services_down=[],
        )
        assert report.is_healthy()
        assert len(report.get_issues()) == 0

    def test_unhealthy_environment(self):
        """Test reporting unhealthy environment."""
        report = EnvironmentHealthReport(
            droplet_id=12345,
            droplet_status="active",
            docker_daemon_running=False,
            services_down=["db"],
            error="Docker daemon not responding",
        )
        assert not report.is_healthy()
        issues = report.get_issues()
        assert len(issues) > 0
        assert any("Docker" in issue for issue in issues)

    def test_high_disk_usage_issue(self):
        """Test detection of high disk usage."""
        report = EnvironmentHealthReport(
            droplet_id=12345,
            droplet_status="active",
            docker_daemon_running=True,
            disk_usage_percent=95.0,
        )
        issues = report.get_issues()
        assert any("disk" in issue.lower() for issue in issues)

    def test_high_memory_usage_issue(self):
        """Test detection of high memory usage."""
        report = EnvironmentHealthReport(
            droplet_id=12345,
            droplet_status="active",
            docker_daemon_running=True,
            memory_usage_percent=92.0,
        )
        issues = report.get_issues()
        assert any("memory" in issue.lower() for issue in issues)


class TestDeploymentMetrics:
    """Test DeploymentMetrics model."""

    def test_create_metrics(self):
        """Test creating deployment metrics."""
        metrics = DeploymentMetrics(
            total_phases=5,
            successful_phases=4,
            failed_phases=1,
            skipped_phases=0,
            total_duration_seconds=120.0,
            average_phase_duration=24.0,
        )
        assert metrics.total_phases == 5
        assert metrics.successful_phases == 4
        assert metrics.failed_phases == 1

    def test_success_rate_calculation(self):
        """Test success rate calculation."""
        metrics = DeploymentMetrics(
            total_phases=10,
            successful_phases=8,
            failed_phases=2,
            skipped_phases=0,
            total_duration_seconds=100.0,
            average_phase_duration=10.0,
        )
        assert metrics.success_rate() == 80.0

    def test_success_rate_all_successful(self):
        """Test success rate when all succeed."""
        metrics = DeploymentMetrics(
            total_phases=5,
            successful_phases=5,
            failed_phases=0,
            skipped_phases=0,
            total_duration_seconds=50.0,
            average_phase_duration=10.0,
        )
        assert metrics.success_rate() == 100.0

    def test_health_check_success_rate(self):
        """Test health check success rate."""
        metrics = DeploymentMetrics(
            total_phases=3,
            successful_phases=3,
            failed_phases=0,
            skipped_phases=0,
            total_duration_seconds=30.0,
            average_phase_duration=10.0,
            health_checks_passed=3,
            health_checks_failed=1,
        )
        assert metrics.health_check_success_rate() == 75.0

    def test_negative_phase_count_validation(self):
        """Test validation of negative phase counts."""
        with pytest.raises(ValueError):
            DeploymentMetrics(
                total_phases=5,
                successful_phases=-1,
                failed_phases=0,
                skipped_phases=0,
                total_duration_seconds=50.0,
                average_phase_duration=10.0,
            )


class TestCalculateDeploymentMetrics:
    """Test metrics calculation from deployment report."""

    def test_calculate_metrics_simple(self):
        """Test calculating metrics from simple report."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        now = datetime.utcnow()
        report.add_phase(
            DeploymentPhase.PREFLIGHT,
            PhaseResult(
                phase=DeploymentPhase.PREFLIGHT,
                status=PhaseStatus.SUCCESS,
                started_at=now,
                completed_at=now + timedelta(seconds=10),
            ),
        )
        report.mark_complete(PhaseStatus.SUCCESS)

        metrics = calculate_deployment_metrics(report)
        assert metrics.total_phases == 1
        assert metrics.successful_phases == 1
        assert metrics.failed_phases == 0

    def test_calculate_metrics_with_failures(self):
        """Test calculating metrics with failures."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        now = datetime.utcnow()
        report.add_phase(
            DeploymentPhase.BUILD,
            PhaseResult(
                phase=DeploymentPhase.BUILD,
                status=PhaseStatus.SUCCESS,
                started_at=now,
                completed_at=now + timedelta(seconds=30),
            ),
        )
        report.add_phase(
            DeploymentPhase.DEPLOY,
            PhaseResult(
                phase=DeploymentPhase.DEPLOY,
                status=PhaseStatus.FAILED,
                started_at=now + timedelta(seconds=30),
                completed_at=now + timedelta(seconds=35),
            ),
        )
        report.mark_complete(PhaseStatus.FAILED)

        metrics = calculate_deployment_metrics(report)
        assert metrics.total_phases == 2
        assert metrics.successful_phases == 1
        assert metrics.failed_phases == 1

    def test_calculate_metrics_with_health_checks(self):
        """Test calculating metrics with health checks."""
        report = DeploymentReport(
            droplet_name="staging",
            branch="main",
            initiated_at=datetime.utcnow(),
        )
        report.health_checks.extend(
            [
                HealthCheckResult(
                    service_name="web",
                    endpoint="localhost:8000",
                    is_healthy=True,
                ),
                HealthCheckResult(
                    service_name="db",
                    endpoint="localhost:5432",
                    is_healthy=False,
                ),
            ]
        )

        metrics = calculate_deployment_metrics(report)
        assert metrics.health_checks_passed == 1
        assert metrics.health_checks_failed == 1
