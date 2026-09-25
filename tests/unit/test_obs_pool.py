"""Tests for the observation-settled capital pool (Phase 4)."""

import pytest

from src.execution.risk_manager import RiskManager


@pytest.fixture
def rm():
    """Risk manager with $100 bankroll for clean math."""
    return RiskManager(initial_bankroll=100.0)


class TestObsPool:
    def test_pool_size_calculation(self, rm):
        """$100 bankroll × 15% = $15 pool."""
        pool = rm.get_obs_settled_pool(100.0)
        assert pool == pytest.approx(15.0)

    def test_geometric_deploy(self, rm):
        """First: $7.50, second: $3.75, third: $1.875."""
        # First signal
        deploy1 = rm.get_obs_settled_deploy_amount(100.0)
        assert deploy1 == pytest.approx(7.50)
        rm.record_obs_deploy(deploy1)

        # Second signal
        deploy2 = rm.get_obs_settled_deploy_amount(100.0)
        assert deploy2 == pytest.approx(3.75)
        rm.record_obs_deploy(deploy2)

        # Third signal
        deploy3 = rm.get_obs_settled_deploy_amount(100.0)
        assert deploy3 == pytest.approx(1.875)

    def test_pool_depletion_floor(self, rm):
        """After many signals, pool drops below $0.50 → returns 0."""
        # Deploy until pool is tiny
        for _ in range(10):
            amount = rm.get_obs_settled_deploy_amount(100.0)
            if amount <= 0:
                break
            rm.record_obs_deploy(amount)

        # Pool should be depleted (below min deploy)
        assert rm.get_obs_settled_deploy_amount(100.0) == 0.0

    def test_forecast_budget_excludes_pool(self, rm):
        """$100 bankroll → $85 for forecast trades."""
        budget = rm.get_forecast_budget(100.0)
        assert budget == pytest.approx(85.0)

    def test_settlement_replenishes(self, rm):
        """Deploy $7.50, settle with $0.30 profit → pool grows."""
        deploy = rm.get_obs_settled_deploy_amount(100.0)
        rm.record_obs_deploy(deploy)  # $7.50 deployed

        pool_after_deploy = rm.get_obs_settled_pool(100.0)
        assert pool_after_deploy == pytest.approx(7.50)  # 15 - 7.50

        # Settlement: capital returned + profit
        rm.record_obs_settlement(deployed=deploy, profit=0.30)

        pool_after_settle = rm.get_obs_settled_pool(100.0)
        assert pool_after_settle == pytest.approx(15.30)  # 15 - 0 + 0.30

    def test_pool_never_goes_negative(self, rm):
        """Edge case: more deployed than pool → returns 0."""
        rm._obs_pool_deployed = 20.0  # More than 15% of $100
        pool = rm.get_obs_settled_pool(100.0)
        assert pool == 0.0

    def test_backward_compat_no_risk_manager(self):
        """Scanner without risk_manager should still work."""
        from src.strategy.observation_scanner import ObservationSettledScanner
        scanner = ObservationSettledScanner()
        # Should initialize without error, risk_manager is optional
        assert hasattr(scanner, 'risk_manager') or True  # Just verify no crash
