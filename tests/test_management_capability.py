"""Shared clan-management decision contract tests."""

from unittest.mock import patch

from capabilities.management import get_management_decisions


class _Connection:
    def close(self):
        pass


class _ManagementSource:
    def get_connection(self):
        return _Connection()

    def get_members_at_risk(self, **kwargs):
        return {"criteria": kwargs, "members": [{"tag": "#A", "kick_state": "recommended"}]}

    def get_promotion_candidates(self):
        return {"members": [{"tag": "#B", "promote_state": "eligible"}]}

    def get_demotion_candidates(self):
        return {"members": []}

    def list_leader_actions(self, **kwargs):
        return [{"action_id": 1, "status": kwargs.get("status", "approved")}]

    def decision_case_snapshot(self, **kwargs):
        return {"due": [{"case_id": 2}], "open": [], "query": kwargs}

    def list_pending_revisits(self, **kwargs):
        return [{"revisit_id": 3, "query": kwargs}]


def test_management_contract_declares_engine_policy_authority():
    with patch(
        "capabilities.management.management_read_summary",
        return_value={
            "actionable": {"kick": [{"player_tag": "#A"}], "promote": [], "demote": []},
            "building_counts": {"kick": 1, "promote": 0, "demote": 0},
            "members_evaluated": 48,
        },
    ):
        result = get_management_decisions(
            view="summary", source=_ManagementSource()
        )

    assert result["capability"] == "management_decisions"
    assert result["contract_version"] == 1
    assert result["audience"] == "leadership"
    assert result["policy"]["rescored"] is False
    assert result["data"]["actionable"]["kick"][0]["player_tag"] == "#A"


def test_management_board_packages_verdicts_and_workflow_state():
    with patch(
        "capabilities.management.management_read_summary",
        return_value={"actionable": {}, "building_counts": {}, "members_evaluated": 2},
    ):
        result = get_management_decisions(view="board", source=_ManagementSource())

    data = result["data"]
    assert data["verdicts"]["kick"]["members"][0]["kick_state"] == "recommended"
    assert data["verdicts"]["promote"]["members"][0]["promote_state"] == "eligible"
    assert data["workflow"]["open_actions"][0]["action_id"] == 1
    assert data["workflow"]["decision_cases"]["due"][0]["case_id"] == 2
    assert data["workflow"]["pending_revisits"][0]["revisit_id"] == 3
