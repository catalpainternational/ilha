"""Pydantic models for deployment state and reporting."""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum
import uuid


class DeploymentPhase(str, Enum):
    """Deployment phase identifier."""

    PREFLIGHT = "preflight"
    BACKUP = "backup"
    PULL = "pull"
    BUILD = "build"
    PUSH = "push"
    MIGRATE = "migrate"
    DEPLOY = "deploy"
    HEALTH_CHECK = "health_check"
    CLEANUP = "cleanup"
    COMPLETE = "complete"


class PhaseStatus(str, Enum):
    """Phase execution status."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class PhaseResult(BaseModel):
    """Result of a single deployment phase."""

    phase: DeploymentPhase
    status: PhaseStatus
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    stdout: Optional[str] = Field(
        default=None, exclude=True, description="Large output, not serialized by default"
    )
    stderr: Optional[str] = Field(
        default=None, exclude=True, description="Large output, not serialized by default"
    )
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def is_terminal(self) -> bool:
        """Check if phase is in terminal state."""
        return self.status in (
            PhaseStatus.SUCCESS,
            PhaseStatus.FAILED,
            PhaseStatus.SKIPPED,
        )

    def duration(self) -> Optional[float]:
        """Calculate duration if completed."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    def is_success(self) -> bool:
        """Check if phase succeeded."""
        return self.status == PhaseStatus.SUCCESS

    def is_failed(self) -> bool:
        """Check if phase failed."""
        return self.status == PhaseStatus.FAILED


class HealthCheckResult(BaseModel):
    """Health check for deployed services."""

    service_name: str
    endpoint: str
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    is_healthy: bool
    error: Optional[str] = None
    checked_at: Optional[datetime] = None


class DeploymentReport(BaseModel):
    """Complete deployment execution report."""

    deployment_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique deployment identifier",
    )
    droplet_name: str
    branch: str
    initiated_by: str = Field(default="automated", description="Username or 'automated'")
    initiated_at: datetime
    completed_at: Optional[datetime] = None
    overall_status: PhaseStatus = PhaseStatus.IN_PROGRESS
    total_duration_seconds: Optional[float] = None
    phases: Dict[DeploymentPhase, PhaseResult] = Field(default_factory=dict)
    health_checks: List[HealthCheckResult] = Field(default_factory=list)
    rollback_info: Optional[Dict[str, Any]] = None
    notes: str = ""

    def add_phase(self, phase: DeploymentPhase, result: PhaseResult) -> None:
        """Add or update phase result and update overall status if needed."""
        self.phases[phase] = result
        # Update overall status if this phase failed
        if result.is_failed():
            self.overall_status = PhaseStatus.FAILED

    def mark_complete(self, status: PhaseStatus) -> None:
        """Mark deployment as complete with final status."""
        self.completed_at = datetime.utcnow()
        self.overall_status = status
        if self.initiated_at:
            self.total_duration_seconds = (
                self.completed_at - self.initiated_at
            ).total_seconds()

    def get_failed_phases(self) -> List[DeploymentPhase]:
        """Get list of failed phases."""
        return [
            phase
            for phase, result in self.phases.items()
            if result.status == PhaseStatus.FAILED
        ]

    def get_skipped_phases(self) -> List[DeploymentPhase]:
        """Get list of skipped phases."""
        return [
            phase
            for phase, result in self.phases.items()
            if result.status == PhaseStatus.SKIPPED
        ]

    def get_successful_phases(self) -> List[DeploymentPhase]:
        """Get list of successful phases."""
        return [
            phase
            for phase, result in self.phases.items()
            if result.status == PhaseStatus.SUCCESS
        ]

    def get_pending_phases(self) -> List[DeploymentPhase]:
        """Get list of pending phases."""
        return [
            phase
            for phase, result in self.phases.items()
            if result.status == PhaseStatus.PENDING
        ]

    def summary(self) -> str:
        """Generate human-readable summary."""
        lines = [
            f"Deployment {self.deployment_id}",
            f"Droplet: {self.droplet_name}, Branch: {self.branch}",
            f"Status: {self.overall_status.value}",
        ]

        if self.total_duration_seconds:
            minutes = int(self.total_duration_seconds // 60)
            seconds = int(self.total_duration_seconds % 60)
            lines.append(f"Duration: {minutes}m {seconds}s")

        # Phase summary
        total_phases = len(self.phases)
        if total_phases > 0:
            successful = len(self.get_successful_phases())
            failed = len(self.get_failed_phases())
            skipped = len(self.get_skipped_phases())
            lines.append(f"Phases: {successful}/{total_phases} successful", )
            if failed > 0:
                lines.append(f"  ❌ Failed: {', '.join(p.value for p in self.get_failed_phases())}")
            if skipped > 0:
                lines.append(f"  ⊘ Skipped: {', '.join(p.value for p in self.get_skipped_phases())}")

        # Health checks
        if self.health_checks:
            healthy_checks = [c for c in self.health_checks if c.is_healthy]
            lines.append(f"Health: {len(healthy_checks)}/{len(self.health_checks)} checks passing")

        if self.notes:
            lines.append(f"Notes: {self.notes}")

        return "\n".join(lines)

    def short_status(self) -> str:
        """Generate one-line status summary."""
        status_symbol = "✅" if self.overall_status == PhaseStatus.SUCCESS else "❌"
        return (
            f"{status_symbol} {self.overall_status.value}: "
            f"{self.droplet_name}/{self.branch} "
            f"({len(self.get_successful_phases())}/{len(self.phases)} phases)"
        )

    def phase_summary_table(self) -> str:
        """Generate table of phase results."""
        if not self.phases:
            return "No phases recorded"

        lines = ["Phase          | Status       | Duration"]
        lines.append("-" * 50)

        for phase, result in self.phases.items():
            duration_str = ""
            if result.duration() is not None:
                duration_str = f"{result.duration():.1f}s"
            status_str = result.status.value
            lines.append(f"{phase.value:14s} | {status_str:12s} | {duration_str:>8s}")

        return "\n".join(lines)


class EnvironmentHealthReport(BaseModel):
    """Health status of remote environment (droplet + services)."""

    droplet_id: int
    droplet_status: str  # "active", "offline", etc.
    docker_daemon_running: bool
    compose_version: Optional[str] = None
    services_running: List[str] = Field(default_factory=list)
    services_down: List[str] = Field(default_factory=list)
    disk_usage_percent: Optional[float] = None
    memory_usage_percent: Optional[float] = None
    cpu_load_avg: Optional[float] = None
    error: Optional[str] = None
    checked_at: Optional[datetime] = None

    def is_healthy(self) -> bool:
        """Check if environment is in healthy state."""
        if self.error:
            return False
        if not self.docker_daemon_running:
            return False
        if self.services_down:
            return False
        return True

    def get_issues(self) -> List[str]:
        """Get list of health issues."""
        issues = []
        if self.error:
            issues.append(f"Error: {self.error}")
        if not self.docker_daemon_running:
            issues.append("Docker daemon not running")
        if self.services_down:
            issues.append(f"Services down: {', '.join(self.services_down)}")
        if self.disk_usage_percent and self.disk_usage_percent > 90:
            issues.append(f"High disk usage: {self.disk_usage_percent}%")
        if self.memory_usage_percent and self.memory_usage_percent > 90:
            issues.append(f"High memory usage: {self.memory_usage_percent}%")
        return issues


class DeploymentMetrics(BaseModel):
    """Metrics collected during deployment."""

    total_phases: int
    successful_phases: int
    failed_phases: int
    skipped_phases: int
    total_duration_seconds: float
    average_phase_duration: float
    fastest_phase: Optional[str] = None
    slowest_phase: Optional[str] = None
    health_checks_passed: int = 0
    health_checks_failed: int = 0

    @field_validator("successful_phases", "failed_phases", "skipped_phases")
    @classmethod
    def validate_phase_counts(cls, v: int) -> int:
        """Ensure phase counts are non-negative."""
        if v < 0:
            raise ValueError("Phase count cannot be negative")
        return v

    def success_rate(self) -> float:
        """Calculate success rate as percentage."""
        if self.total_phases == 0:
            return 0.0
        return (self.successful_phases / self.total_phases) * 100

    def health_check_success_rate(self) -> float:
        """Calculate health check success rate."""
        total = self.health_checks_passed + self.health_checks_failed
        if total == 0:
            return 0.0
        return (self.health_checks_passed / total) * 100


def calculate_deployment_metrics(report: DeploymentReport) -> DeploymentMetrics:
    """Calculate metrics from a deployment report."""
    successful = len(report.get_successful_phases())
    failed = len(report.get_failed_phases())
    skipped = len(report.get_skipped_phases())
    total = len(report.phases)

    total_duration = report.total_duration_seconds or 0
    avg_duration = total_duration / total if total > 0 else 0

    # Find fastest and slowest phases
    fastest = None
    slowest = None
    if report.phases:
        durations = [
            (p, r.duration() or 0) for p, r in report.phases.items() if r.duration()
        ]
        if durations:
            slowest = min(durations, key=lambda x: x[1])[0].value
            fastest = max(durations, key=lambda x: x[1])[0].value

    health_checks_passed = len([c for c in report.health_checks if c.is_healthy])
    health_checks_failed = len([c for c in report.health_checks if not c.is_healthy])

    return DeploymentMetrics(
        total_phases=total,
        successful_phases=successful,
        failed_phases=failed,
        skipped_phases=skipped,
        total_duration_seconds=total_duration,
        average_phase_duration=avg_duration,
        fastest_phase=fastest,
        slowest_phase=slowest,
        health_checks_passed=health_checks_passed,
        health_checks_failed=health_checks_failed,
    )
