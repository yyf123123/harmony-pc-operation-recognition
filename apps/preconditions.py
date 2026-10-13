"""Explicit, app-neutral precondition checks for operation execution."""

from __future__ import annotations

from typing import Any, Mapping

from .base import OperationSpec


class PreconditionFailed(RuntimeError):
    """The observed UI/process state does not satisfy an operation contract."""

    def __init__(self, operation_id: str, precondition: str) -> None:
        super().__init__(f"{operation_id} requires {precondition}")
        self.operation_id = operation_id
        self.precondition = precondition


def _fixture_tab_id(state: Mapping[str, Any], page: int) -> str | None:
    tabs = state.get("fixture_tab_ids")
    if not isinstance(tabs, Mapping):
        return None
    value = tabs.get(str(page), tabs.get(page))
    return str(value) if value else None


def _two_fixture_tabs_page_2_active(state: Mapping[str, Any]) -> bool:
    page_1 = _fixture_tab_id(state, 1)
    page_2 = _fixture_tab_id(state, 2)
    tab_ids = state.get("tab_ids")
    return (
        bool(state.get("browser_running"))
        and int(state.get("tab_count", 0)) == 2
        and bool(state.get("page_loaded"))
        and int(state.get("page_number", 0) or 0) == 2
        and bool(page_1)
        and bool(page_2)
        and page_1 != page_2
        and state.get("active_tab") == page_2
        and isinstance(tab_ids, list)
        and set(tab_ids) == {page_1, page_2}
    )


def precondition_satisfied(operation: OperationSpec, state: Mapping[str, Any]) -> bool:
    """Check the small set of state facts needed by the catalog contracts.

    Device adapters may provide richer validation, but they must return the same
    boolean contract.  A zero exit code from an automation command is never used
    as a substitute for these checks.
    """
    condition = operation.precondition
    app_id = operation.app_id
    if app_id == "huawei_browser":
        if condition == "BROWSER_NOT_RUNNING":
            return not bool(state.get("browser_running"))
        if condition == "BROWSER_RUNNING":
            return bool(state.get("browser_running"))
        if condition == "BROWSER_RUNNING_WITH_EMPTY_TAB":
            return bool(state.get("browser_running")) and int(state.get("tab_count", 0)) == 1 and not bool(state.get("page_loaded"))
        if condition == "BROWSER_RUNNING_WITH_ONE_OR_MORE_TABS":
            return bool(state.get("browser_running")) and int(state.get("tab_count", 0)) >= 1
        if condition == "BROWSER_RUNNING_WITH_AT_LEAST_TWO_TABS":
            return bool(state.get("browser_running")) and int(state.get("tab_count", 0)) >= 2
        if condition == "BROWSER_RUNNING_WITH_TWO_FIXED_TABS_PAGE_2_ACTIVE":
            return _two_fixture_tabs_page_2_active(state)
        if condition == "FIXED_PAGE_LOADED":
            return bool(state.get("browser_running")) and bool(state.get("page_loaded"))
        if condition == "FIXED_PAGE_1_ACTIVE":
            return (
                bool(state.get("browser_running"))
                and bool(state.get("page_loaded"))
                and int(state.get("tab_count", 0)) == 1
                and int(state.get("page_number", 0) or 0) == 1
            )
        if condition == "FIXED_PAGE_2_WITH_PAGE_1_HISTORY":
            return (
                bool(state.get("browser_running"))
                and bool(state.get("page_loaded"))
                and int(state.get("page_number", 0) or 0) == 2
                and list(state.get("history_chain", [])) == [1, 2]
                and int(state.get("history_depth", 0)) >= 1
            )
        if condition == "FIXED_PAGE_1_WITH_PAGE_2_FORWARD":
            return (
                bool(state.get("browser_running"))
                and bool(state.get("page_loaded"))
                and int(state.get("page_number", 0) or 0) == 1
                and list(state.get("history_chain", [])) == [1, 2]
                and int(state.get("forward_depth", 0)) >= 1
            )
        if condition == "FIXED_PAGE_LOADED_WITH_PRIOR_HISTORY_ENTRY":
            return precondition_satisfied(OperationSpec("x", app_id, "", "", "", "FIXED_PAGE_LOADED", {}, ""), state) and int(state.get("history_depth", 0)) >= 1
        if condition == "BROWSER_AFTER_BACK_WITH_FORWARD_ENTRY":
            return bool(state.get("browser_running")) and int(state.get("forward_depth", 0)) >= 1
        if condition == "FIXED_PAGE_LOADED_WITH_SCROLLABLE_CONTENT":
            return bool(state.get("page_loaded")) and bool(state.get("scrollable"))
        if condition == "BROWSER_RUNNING_WITH_ACTIVE_TAB_AND_ADDRESS_FOCUS":
            return bool(state.get("browser_running")) and int(state.get("tab_count", 0)) >= 1 and bool(state.get("address_focus"))
        if condition == "FIXED_PAGE_LOADED_WITH_ASSET":
            return bool(state.get("page_loaded")) and bool(state.get("asset_available"))
        if condition == "BROWSER_RUNNING_WITH_NETWORK":
            return bool(state.get("browser_running")) and bool(state.get("network_available"))
        if condition == "LOGIN_TEST_ACCOUNT_AVAILABLE":
            return bool(state.get("test_account_available"))
    if app_id == "bilibili":
        if condition == "BILIBILI_NOT_RUNNING":
            return not bool(state.get("app_running"))
        if condition == "BILIBILI_RUNNING_WITH_NETWORK":
            return bool(state.get("app_running")) and bool(state.get("network_available"))
        if condition == "FIXED_VIDEO_LOADED_WITH_DURATION_KNOWN":
            return bool(state.get("video_loaded")) and float(state.get("video_duration", 0) or 0) > 0
        if condition == "FIXED_VIDEO_LOADED_NOT_FULLSCREEN":
            return bool(state.get("video_loaded")) and not bool(state.get("fullscreen"))
        if condition == "FIXED_VIDEO_LOADED_FULLSCREEN":
            return bool(state.get("video_loaded")) and bool(state.get("fullscreen"))
        if condition == "FIXED_VIDEO_LOADED_AND_SECOND_FIXED_VIDEO_AVAILABLE":
            return bool(state.get("video_loaded")) and bool(state.get("second_video_available"))
        if condition == "FIXED_VIDEO_PAGE_LOADED":
            return bool(state.get("video_loaded"))
        if condition == "BILIBILI_SECONDARY_PAGE":
            return (
                bool(state.get("app_running"))
                and state.get("foreground_state") == "foreground"
                and bool(state.get("secondary_page"))
                and state.get("page_kind") in {"comments", "search", "profile"}
            )
        if condition == "BILIBILI_FOREGROUND_WITH_FIXED_VIDEO":
            return bool(state.get("video_loaded")) and state.get("foreground_state") == "foreground"
        if condition == "BILIBILI_BACKGROUND_WITH_FIXED_VIDEO_STATE":
            return bool(state.get("video_loaded")) and state.get("foreground_state") == "background"
        if condition == "FIXED_VIDEO_LOADED_PAUSED":
            return bool(state.get("video_loaded")) and state.get("playback_state") == "paused"
        if condition == "FIXED_VIDEO_PLAYING":
            return bool(state.get("video_loaded")) and state.get("playback_state") == "playing"
    # WPS preconditions are retained as legacy labels and are validated by the
    # existing WPS runner.  An unknown device state is not considered proven.
    if app_id == "wps":
        return bool(state.get("legacy_precondition_valid", False))
    # Unknown contracts must be explicitly attested by the device adapter.
    return bool(state.get("precondition_valid", False))


def require_precondition(operation: OperationSpec, state: Mapping[str, Any]) -> None:
    if not precondition_satisfied(operation, state):
        raise PreconditionFailed(operation.operation_id, operation.precondition)
