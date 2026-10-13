"""Collect a UI-confirmed Huawei Browser VMA database over HDC.

The collector keeps device setup outside the measured action window.  Each
formal trial is therefore:

    prepare -> precondition -> 2 baseline captures -> action -> confirmation
    -> post-action capture -> feature -> save

The active Browser pilot design is five operations by ten rounds plus balanced
no-op controls.  The legacy eight-operation collector remains reproducible via
an explicit ``--operations`` override; only device-confirmed, non-mock records
are eligible for the later session-held-out evaluator.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.huawei_browser import HuaweiBrowserAdapter
from apps.preconditions import require_precondition
from apps.shared.collection_contract import cross_app_hdc_contract
from apps.shared.hdc_trial import (
    arm_reference_window,
    failure_code,
    feature_identifier,
    feature_payload,
    normalize_hdc_argument,
    now_id,
    snapshot,
    timestamp,
)
from apps.shared.hdc_ui import HdcLayoutUi
from collector.automation.base import BackendActionResult
from collector.automation.hdc_backend import HdcBackend, HdcError
from collector.reference_windows import CaptureProfile, ReferenceWindowCapture
from collector.schedule import balanced_trial_schedule
from collector.dataset.database import DatabaseStore
from collector.dataset.manifest import DatasetLayout
from collector.dataset.models import QualityRecord, SessionRecord, TrialRecord
from features.feature_schema import CROSS_APP_FEATURE_SCHEMA_VERSION
from features.vma_feature import build_cross_app_feature_pair
from collector.runtime.timers import TimingRecorder
from collector.runtime.trial_state import TrialState, TrialStateMachine


DEFAULT_OPERATIONS = (
    "huawei_browser.browser.navigate_fixed_url",
    "huawei_browser.browser.open_settings",
    "huawei_browser.browser.open_history_page",
    "huawei_browser.browser.open_downloads_page",
    "huawei_browser.browser.open_tab_overview",
)

KEY_ENTER = "2054"
KEY_CTRL_LEFT = "2072"
KEY_A = "2017"
NETWORK_CONFIRMATION_TIMEOUT_S = 30.0
NETWORK_PREFLIGHT_ATTEMPTS = 2
NETWORK_PREFLIGHT_TIMEOUT_S = 15.0
BACK_CONTROL_IDS = ("pc_icon_backward_btn", "pc_icon_back_btn", "pc_back_btn")
FORWARD_CONTROL_IDS = ("pc_icon_forward_btn", "pc_forward_btn")
COLLECTION_CONTRACT = cross_app_hdc_contract("huawei_browser")


TIME_CUTOFF_ERROR = "TIME_CUTOFF_NOT_STARTED"


def parse_stop_before_utc(value: str | None) -> datetime | None:
    """Parse an optional absolute collection cutoff without changing the profile.

    The collector checks this only between trials.  A trial that has already
    started remains allowed to finish through its ordinary error-handling path;
    every scheduled trial that never starts receives a distinct INVALID record
    rather than disappearing from the manifest.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("--stop-before-utc must be an ISO-8601 UTC timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("--stop-before-utc must include a UTC offset or Z")
    return parsed.astimezone(timezone.utc)


def record_time_cutoff_trial(
    database: DatabaseStore,
    *,
    session_id: str,
    operation_id: str,
    trial_id: str,
    round_index: int,
    schedule_position: int,
    stop_before_utc: datetime,
) -> dict[str, Any]:
    """Materialize a planned but unstarted trial as auditable INVALID evidence."""
    detail = f"planned HDC trial was not started before cutoff {stop_before_utc.isoformat()}"
    state = {
        "round": round_index,
        "schedule_position": schedule_position,
        "stop_before_utc": stop_before_utc.isoformat(),
        "reason": TIME_CUTOFF_ERROR,
    }
    database.insert_trial(
        TrialRecord(
            trial_id,
            session_id,
            operation_id,
            "failed",
            source="hdc",
            is_mock=False,
            state=state,
            current_state="FAILED",
            quality_status="INVALID",
            error_code=TIME_CUTOFF_ERROR,
            error_detail=detail,
        )
    )
    database.insert_quality(
        QualityRecord(
            trial_id,
            False,
            None,
            source="hdc",
            is_mock=False,
            quality_status="INVALID",
            error_code=TIME_CUTOFF_ERROR,
            reason=detail,
        )
    )
    database.insert_event(
        trial_id,
        "FAILED",
        timestamp(),
        {"error_code": TIME_CUTOFF_ERROR, "error_detail": detail, "state_history": ["PREPARE", "FAILED"]},
        source="hdc",
        is_mock=False,
    )
    database.insert_timing(trial_id, {"T_total": None}, source="hdc", is_mock=False)
    return {
        "trial_id": trial_id,
        "operation_id": operation_id,
        "round": round_index,
        "schedule_position": schedule_position,
        "status": "failed",
        "quality_status": "INVALID",
        "error_code": TIME_CUTOFF_ERROR,
        "error_detail": detail,
    }


def probe_network(backend: HdcBackend, url: str, *, attempts: int = NETWORK_PREFLIGHT_ATTEMPTS) -> dict[str, Any]:
    """Require repeated device-side reachability before opening a network pilot.

    Browser confirmation remains authoritative for every measured navigation.
    This preflight only prevents a known-offline device from creating a new
    session that is guaranteed to contain invalid network trials.
    """
    if attempts < 1:
        raise ValueError("network preflight requires at least one attempt")
    observations: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        result = backend.run(
            ("shell", "curl", "-I", "--max-time", str(int(NETWORK_PREFLIGHT_TIMEOUT_S)), url),
            timeout_s=NETWORK_PREFLIGHT_TIMEOUT_S + 5.0,
        )
        response = f"{result.stdout}\n{result.stderr}"
        reachable = result.ok and "HTTP/" in response
        observations.append(
            {
                "attempt": attempt,
                "reachable": reachable,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "stdout_tail": result.stdout[-300:],
                "stderr_tail": result.stderr[-300:],
            }
        )
    return {
        "url": url,
        "attempts": observations,
        "required_successes": attempts,
        "passed": all(item["reachable"] for item in observations),
    }


def require_initial_browser_ui(state: dict[str, Any], bundle: str) -> None:
    """Reject a live PID with no inspectable Browser UI before opening a session.

    Browser can remain alive after all of its tabs/windows disappear.  That is
    a device-side Gate 2 failure, not an empty Browser tab that the collector
    may invent.  The caller deliberately does not manipulate foreground state
    here; another owner can restore it and re-run discovery safely.
    """
    if int(state.get("layout_node_count", 0) or 0) <= 1:
        raise SystemExit(
            json.dumps(
                {
                    "status": "blocked",
                    "error": "BROWSER_UI_UNAVAILABLE",
                    "bundle": bundle,
                    "state": state,
                },
                ensure_ascii=False,
            )
        )
    if int(state.get("tab_count", 0) or 0) < 1:
        raise SystemExit(
            json.dumps(
                {
                    "status": "blocked",
                    "error": "BROWSER_NO_TAB",
                    "bundle": bundle,
                    "state": state,
                },
                ensure_ascii=False,
            )
        )


def parse_bounds(value: str) -> tuple[int, int, int, int]:
    left_top, right_bottom = value.strip("[]").split("][")
    left, top = (int(item) for item in left_top.split(","))
    right, bottom = (int(item) for item in right_bottom.split(","))
    return left, top, right, bottom


def center(value: str) -> tuple[int, int]:
    left, top, right, bottom = parse_bounds(value)
    return (left + right) // 2, (top + bottom) // 2


class BrowserUi(HdcLayoutUi):
    """Small, observable UI adapter for the validated Browser window."""

    layout_operation_name = "Browser layout dump"

    def __init__(self, backend: HdcBackend, bundle: str, fixture_base_url: str, navigate_url: str) -> None:
        self.backend = backend
        self.bundle = bundle
        self.fixture_base_url = fixture_base_url.rstrip("/")
        self.network_navigate_url = navigate_url
        parsed = urlparse(navigate_url)
        if not parsed.hostname:
            raise ValueError(f"navigate URL has no hostname: {navigate_url}")
        self.navigate_host = parsed.hostname.lower()
        self._history_depth = 0
        self._forward_depth = 0
        self._history_chain: tuple[int, ...] = ()
        self._fixture_tab_ids: dict[int, str] = {}

    def fixture_url(self, page: int) -> str:
        return f"{self.fixture_base_url}/page_{page:02d}.html"

    @staticmethod
    def host_matches(actual: str | None, expected: str | None) -> bool:
        if not actual or not expected:
            return False
        expected_base = expected.lower().removeprefix("www.")
        return actual.lower() == expected_base or actual.lower().endswith(f".{expected_base}")

    def state(self, token: str) -> dict[str, Any]:
        nodes = self._nodes(token)
        visible = [node for node in nodes if node.get("visible") == "true"]
        visible_ids = {node.get("id", "") for node in visible if node.get("id")}
        tabs = sorted(
            (node for node in visible if node.get("id", "").startswith("pc_tab_title_")),
            key=lambda node: parse_bounds(node["bounds"])[0],
        )
        selected = [node for node in tabs if node.get("selected") == "true"]
        inputs = [node for node in visible if node.get("type") == "TextInput" and parse_bounds(node.get("bounds", "[0,0][0,0]"))[1] < 500]
        address = inputs[-1] if inputs else {}
        text_values = [node.get("text", "") for node in visible if node.get("text")]
        title_values = [value for value in text_values if "Huawei Browser VMA Fixture" in value]
        title = title_values[-1] if title_values else ""
        page_match = re.search(r"Fixture (\d+)", title)
        if page_match is None:
            page_match = re.search(r"page_(\d+)\.html", address.get("text", ""))
        page_number = int(page_match.group(1)) if page_match else None
        heading = next((node for node in visible if node.get("type") == "heading" and "VMA Fixture" in node.get("text", "")), None)
        heading_y = parse_bounds(heading["bounds"])[1] if heading else None
        address_text = address.get("text", "")
        address_url = address_text if "://" in address_text else f"http://{address_text}" if address_text else ""
        url_host = (urlparse(address_url).hostname or "").lower() or None
        error_markers = ("err_", "无法访问", "无法打开", "timed out", "connection refused", "can't reach")
        error_text = " ".join(text_values).lower()
        network_error = any(marker in error_text for marker in error_markers)
        content_nodes = [node for node in visible if node.get("type") in {"heading", "paragraph", "button", "TextInput"} and parse_bounds(node.get("bounds", "[0,0][0,0]"))[1] >= 500 and node.get("text")]
        network_page_loaded = (bool(url_host and (url_host.endswith(".baidu.com") or url_host == "baidu.com")) and bool(content_nodes) and not network_error)
        fixture_page_loaded = page_number is not None
        if address_text.startswith("hwbrowser://settings") and "pc_settings_menu" in visible_ids:
            internal_page = "settings"
        elif address_text.startswith("hwbrowser://history") and ({"history_list_14", "pc_title_in_history"} & visible_ids):
            internal_page = "history"
        elif address_text.startswith("hwbrowser://downloads") and "pc_title_in_download" in visible_ids:
            internal_page = "downloads"
        elif {"pc_open_tabs_title", "pc_tabs_search"}.issubset(visible_ids):
            internal_page = "tab_overview"
        else:
            internal_page = None
        tab_overview_visible = internal_page == "tab_overview"
        return {
            "browser_running": True,
            "tab_count": len(tabs),
            "tab_ids": [node.get("id", "") for node in tabs],
            "active_tab": selected[0].get("id") if selected else None,
            "active_url": address_text,
            "url_host": url_host,
            "address_focus": address.get("focused") == "true",
            "page_loaded": fixture_page_loaded or network_page_loaded or internal_page is not None,
            "network_page_loaded": network_page_loaded,
            "network_error": network_error,
            "visible_content_count": len(content_nodes),
            "page_number": page_number,
            "page_title": title,
            "internal_page": internal_page,
            "tab_overview_visible": tab_overview_visible,
            "visible_ids": sorted(visible_ids),
            "scrollable": page_number is not None,
            "heading_y": heading_y,
            "history_depth": self._history_depth,
            "forward_depth": self._forward_depth,
            "history_chain": list(self._history_chain),
            "fixture_tab_ids": {str(page): tab_id for page, tab_id in self._fixture_tab_ids.items()},
            "layout_token": token,
            "layout_node_count": len(nodes),
        }

    def wait_for(self, predicate: Callable[[dict[str, Any]], bool], *, timeout_s: float = 15.0, label: str) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.state(now_id("browser_layout"))
            if predicate(last):
                return last
            time.sleep(0.4)
        raise RuntimeError(f"Browser confirmation timeout: {label}; last_state={last}")

    def wait_for_stable_state(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        key: Callable[[dict[str, Any]], tuple[Any, ...]],
        *,
        label: str,
        timeout_s: float = 15.0,
    ) -> dict[str, Any]:
        """Require two consecutive observable layout states before capture."""
        deadline = time.monotonic() + timeout_s
        previous_key: tuple[Any, ...] | None = None
        stable_count = 0
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.state(now_id("browser_stable_layout"))
            if predicate(last):
                current_key = key(last)
                stable_count = stable_count + 1 if current_key == previous_key else 1
                previous_key = current_key
                if stable_count >= 2:
                    return last
            else:
                previous_key = None
                stable_count = 0
            time.sleep(0.4)
        raise RuntimeError(f"Browser stable confirmation timeout: {label}; last_state={last}")

    def wait_for_fixture_page(
        self,
        page: int,
        *,
        label: str,
        expected_tab: str | None = None,
        timeout_s: float = 15.0,
    ) -> dict[str, Any]:
        return self.wait_for_stable_state(
            lambda value: value.get("page_number") == page
            and bool(value.get("page_loaded"))
            and (expected_tab is None or value.get("active_tab") == expected_tab),
            lambda value: (
                value.get("active_tab"),
                value.get("page_number"),
                value.get("active_url"),
                value.get("page_loaded"),
            ),
            label=label,
            timeout_s=timeout_s,
        )

    def _fixture_tab_id(self, page: int) -> str:
        tab_id = self._fixture_tab_ids.get(page)
        if not tab_id:
            raise RuntimeError(f"Browser fixture tab mapping has no page {page}")
        return tab_id

    def _click(self, node: dict[str, str], *, operation: str) -> BackendActionResult:
        x, y = center(node["bounds"])
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str(x), str(y)))
        if not result.ok:
            self.backend._raise_for_result(result, operation=operation)
        return BackendActionResult("pass", {"click_x": x, "click_y": y}, {"control": node.get("id", ""), "x": x, "y": y}, source="hdc", is_mock=False)

    def _unique_node(self, token: str, node_id: str) -> dict[str, str]:
        nodes = [node for node in self._nodes(token) if node.get("id") == node_id and node.get("visible") == "true" and node.get("enabled", "true") != "false"]
        if len(nodes) != 1:
            raise RuntimeError(f"expected one visible {node_id}, found {len(nodes)}")
        return nodes[0]

    def click_control(self, token: str, node_id: str, *, operation: str) -> BackendActionResult:
        return self._click(self._unique_node(token, node_id), operation=operation)

    def click_first_control(self, token: str, node_ids: tuple[str, ...], *, operation: str) -> BackendActionResult:
        for node_id in node_ids:
            nodes = [node for node in self._nodes(token) if node.get("id") == node_id and node.get("visible") == "true" and node.get("enabled", "true") != "false"]
            if len(nodes) == 1:
                return self._click(nodes[0], operation=operation)
            if len(nodes) > 1:
                raise RuntimeError(f"expected one visible {node_id}, found {len(nodes)}")
        raise RuntimeError(f"Browser control is not observable for {operation}: {node_ids}")

    def address_node(self, token: str) -> dict[str, str]:
        nodes = [node for node in self._nodes(token) if node.get("type") == "TextInput" and node.get("visible") == "true" and parse_bounds(node.get("bounds", "[0,0][0,0]"))[1] < 500]
        if len(nodes) != 1:
            raise RuntimeError(f"expected one Browser address input, found {len(nodes)}")
        return nodes[0]

    def focus_address(self, token: str) -> dict[str, Any]:
        self._click(self.address_node(token), operation="Browser address focus")
        return self.wait_for(lambda state: bool(state["address_focus"]), label="address focus")

    def navigate_url(self, url: str, *, token: str, expected_page: int | None = None, expected_host: str | None = None) -> BackendActionResult:
        self.focus_address(token)
        ctrl = self.backend.run(("shell", "uitest", "uiInput", "keyEvent", KEY_CTRL_LEFT, KEY_A))
        if not ctrl.ok:
            self.backend._raise_for_result(ctrl, operation="Browser address select-all")
        text_result = self.backend.run(("shell", "uitest", "uiInput", "text", url))
        if not text_result.ok:
            self.backend._raise_for_result(text_result, operation="Browser address input")
        enter = self.backend.run(("shell", "uitest", "uiInput", "keyEvent", KEY_ENTER))
        if not enter.ok:
            self.backend._raise_for_result(enter, operation="Browser address submit")
        if expected_page is not None:
            state = self.wait_for_fixture_page(expected_page, label=f"fixture page {expected_page}")
        else:
            state = self.wait_for_stable_state(
                lambda value: self.host_matches(value.get("url_host"), expected_host)
                and bool(value.get("network_page_loaded"))
                and not bool(value.get("network_error")),
                lambda value: (
                    value.get("active_tab"),
                    value.get("active_url"),
                    value.get("url_host"),
                    value.get("visible_content_count"),
                ),
                label=f"network page {expected_host or url}",
                timeout_s=NETWORK_CONFIRMATION_TIMEOUT_S,
            )
        final_address = str(state.get("active_url", ""))
        requested = urlparse(url)
        final_url = final_address if "://" in final_address else f"{requested.scheme or 'http'}://{final_address}" if final_address else ""
        observation = {
            "network_required": expected_page is None,
            "requested_url": url,
            "final_url": final_url,
            "final_address_text": final_address,
            "final_host": state.get("url_host"),
            "redirected": bool(final_url and final_url.rstrip("/") != url.rstrip("/")),
            "redirect_chain": [url] if final_url.rstrip("/") == url.rstrip("/") else [url, final_url],
            "cache_impact": "unknown_from_hdc_ui",
            "cache_observation": "Browser cache telemetry is not exposed by the validated HDC UI boundary",
            "page_loaded_confirmed": bool(state.get("network_page_loaded") if expected_page is None else state.get("page_loaded")),
        }
        event = {
            "action": "navigate",
            "url": url,
            "expected_page": expected_page,
            "expected_host": expected_host,
            "page_confirmation": "two_consecutive_observable_layout_states",
            "network_confirmation_timeout_s": NETWORK_CONFIRMATION_TIMEOUT_S if expected_page is None else None,
            "network_observation": observation,
        }
        return BackendActionResult("pass", state, event, source="hdc", is_mock=False)

    def navigate(self, page: int, *, token: str) -> BackendActionResult:
        return self.navigate_url(self.fixture_url(page), token=token, expected_page=page)

    def navigate_internal(self, url: str, *, token: str, expected_page: str, marker_ids: tuple[str, ...]) -> BackendActionResult:
        """Open a Browser internal page and require its observable page marker."""
        self.focus_address(token)
        ctrl = self.backend.run(("shell", "uitest", "uiInput", "keyEvent", KEY_CTRL_LEFT, KEY_A))
        if not ctrl.ok:
            self.backend._raise_for_result(ctrl, operation="Browser internal-page select-all")
        text_result = self.backend.run(("shell", "uitest", "uiInput", "text", url))
        if not text_result.ok:
            self.backend._raise_for_result(text_result, operation="Browser internal-page input")
        enter = self.backend.run(("shell", "uitest", "uiInput", "keyEvent", KEY_ENTER))
        if not enter.ok:
            self.backend._raise_for_result(enter, operation="Browser internal-page submit")
        state = self.wait_for_stable_state(
            lambda value: value.get("internal_page") == expected_page
            and all(marker in value.get("visible_ids", []) for marker in marker_ids),
            lambda value: (
                value.get("active_tab"),
                value.get("tab_count"),
                value.get("active_url"),
                value.get("internal_page"),
                tuple(marker in value.get("visible_ids", []) for marker in marker_ids),
            ),
            label=f"Browser internal page {expected_page}",
        )
        return BackendActionResult(
            "pass",
            state,
            {
                "action": "open_internal_page",
                "requested_url": url,
                "internal_page": expected_page,
                "page_markers": list(marker_ids),
                "page_confirmation": "two_consecutive_observable_layout_states",
            },
            source="hdc",
            is_mock=False,
        )

    def dismiss_tab_overview(self) -> None:
        """Close the validated tab-overview overlay before the next trial."""
        state = self.state(now_id("browser_tab_overview_check"))
        if not state.get("tab_overview_visible"):
            return
        self.click_control(state["layout_token"], "pc_search_icon", operation="Browser tab-overview dismiss")
        self.wait_for_stable_state(
            lambda value: not value.get("tab_overview_visible"),
            lambda value: (value.get("active_tab"), value.get("active_url"), value.get("tab_overview_visible")),
            label="Browser tab-overview dismissal",
        )

    def home(self, *, token: str) -> BackendActionResult:
        result = self.click_control(token, "pc_icon_home_btn", operation="Browser home")
        state = self.wait_for(lambda value: not value.get("page_loaded"), label="Browser empty home tab")
        return BackendActionResult("pass", state, {**dict(result.event), "action": "home"}, source="hdc", is_mock=False)

    def new_tab(self, *, token: str) -> BackendActionResult:
        return self.click_control(token, "pc_add_btn_in_web", operation="Browser new-tab click")

    def close_newest_tab(
        self,
        *,
        token: str,
        cleanup: bool = False,
        expected_active_tab: str | None = None,
        expected_page: int | None = None,
    ) -> BackendActionResult:
        return self._close_edge(
            token=token,
            newest=True,
            cleanup=cleanup,
            expected_active_tab=expected_active_tab,
            expected_page=expected_page,
        )

    def close_oldest_tab(self, *, token: str, cleanup: bool = False) -> BackendActionResult:
        return self._close_edge(token=token, newest=False, cleanup=cleanup, expected_active_tab=None, expected_page=None)

    def _close_edge(
        self,
        *,
        token: str,
        newest: bool,
        cleanup: bool,
        expected_active_tab: str | None,
        expected_page: int | None,
    ) -> BackendActionResult:
        nodes = [node for node in self._nodes(token) if node.get("id") == "pc_close_btn_in_web" and node.get("visible") == "true"]
        if len(nodes) < 2:
            raise RuntimeError(f"close tab requires at least two visible close buttons, found {len(nodes)}")
        before = self.state(token)
        node = (max if newest else min)(nodes, key=lambda item: parse_bounds(item["bounds"])[0])
        result = self._click(node, operation="Browser tab close")
        state = self.wait_for(
            lambda value: value.get("tab_count") == before.get("tab_count", 0) - 1,
            label="tab count decrease",
        )
        if expected_page is not None:
            state = self.wait_for_fixture_page(
                expected_page,
                expected_tab=expected_active_tab,
                label=f"remaining fixture page {expected_page}",
            )
        return BackendActionResult(
            "pass",
            state,
            {
                **dict(result.event),
                "action": "close_tab",
                "cleanup": cleanup,
                "expected_active_tab": expected_active_tab,
                "expected_page": expected_page,
                "page_confirmation": "two_consecutive_observable_layout_states" if expected_page is not None else None,
            },
            source="hdc",
            is_mock=False,
        )

    def switch_to_tab(self, tab_id: str, *, token: str, expected_page: int) -> BackendActionResult:
        nodes = [node for node in self._nodes(token) if node.get("id") == tab_id and node.get("visible") == "true"]
        if len(nodes) != 1:
            raise RuntimeError(f"expected one target tab {tab_id}, found {len(nodes)}")
        before = self.state(token)["active_tab"]
        if before == tab_id:
            raise RuntimeError("switch tab target is already active")
        result = self._click(nodes[0], operation="Browser switch-tab click")
        state = self.wait_for_fixture_page(expected_page, expected_tab=tab_id, label=f"active fixture tab {tab_id}")
        return BackendActionResult(
            "pass",
            state,
            {
                **dict(result.event),
                "action": "switch_tab",
                "target_tab": tab_id,
                "expected_page": expected_page,
                "page_confirmation": "two_consecutive_observable_layout_states",
            },
            source="hdc",
            is_mock=False,
        )

    def scroll_fixed(self, *, token: str) -> BackendActionResult:
        before = self.state(token)
        result = self.backend.run(("shell", "uitest", "uiInput", "swipe", "1450", "1300", "1450", "760", "800"))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Browser fixed scroll")
        # The fixture intentionally contains a large blank spacer.  After a
        # 480px scroll its heading can leave the accessibility tree entirely;
        # the observable proof is therefore the same page URL plus a changed
        # (or absent) heading, never the HDC exit code alone.
        state = self.wait_for_stable_state(
            lambda value: value.get("page_number") == before.get("page_number")
            and bool(value.get("page_loaded"))
            and (
                value.get("heading_y") is None
                or before.get("heading_y") is None
                or value["heading_y"] < before["heading_y"] - 20
            ),
            lambda value: (
                value.get("active_tab"),
                value.get("page_number"),
                value.get("active_url"),
                value.get("heading_y"),
            ),
            label="fixed scroll offset",
        )
        return BackendActionResult("pass", state, {"action": "fixed_scroll", "from": [1450, 1300], "to": [1450, 760]}, source="hdc", is_mock=False)

    def refresh(self, *, token: str) -> BackendActionResult:
        before = self.state(token)
        result = self.click_first_control(token, ("pc_icon_refresh_btn", "pc_refresh_btn"), operation="Browser refresh")
        state = self.wait_for_stable_state(
            lambda value: bool(value.get("page_loaded")) and value.get("active_url") == before.get("active_url"),
            lambda value: (value.get("active_tab"), value.get("active_url"), value.get("page_number")),
            label="Browser refresh settled page",
        )
        # This build does not expose a load-generation counter.  The UI action
        # is recorded but cannot be treated as confirmed until Gate 3 observes
        # an explicit reload transition on the device.
        return BackendActionResult("needs_device_validation", state, {**dict(result.event), "action": "refresh", "confirmation_limit": "no_observable_load_generation"}, source="hdc", is_mock=False)

    def go_back(self, *, token: str, expected_page: int) -> BackendActionResult:
        result = self.click_first_control(token, BACK_CONTROL_IDS, operation="Browser go back")
        state = self.wait_for_fixture_page(expected_page, label=f"Browser history page {expected_page}")
        self._history_depth = max(0, self._history_depth - 1)
        self._forward_depth += 1
        state["history_depth"] = self._history_depth
        state["forward_depth"] = self._forward_depth
        state["history_chain"] = list(self._history_chain)
        return BackendActionResult("pass", state, {**dict(result.event), "action": "go_back", "expected_page": expected_page}, source="hdc", is_mock=False)

    def go_forward(self, *, token: str, expected_page: int) -> BackendActionResult:
        result = self.click_first_control(token, FORWARD_CONTROL_IDS, operation="Browser go forward")
        state = self.wait_for_fixture_page(expected_page, label=f"Browser forward page {expected_page}")
        self._history_depth += 1
        self._forward_depth = max(0, self._forward_depth - 1)
        state["history_depth"] = self._history_depth
        state["forward_depth"] = self._forward_depth
        state["history_chain"] = list(self._history_chain)
        return BackendActionResult("pass", state, {**dict(result.event), "action": "go_forward", "expected_page": expected_page}, source="hdc", is_mock=False)

    def prepare_for(self, operation_id: str) -> dict[str, Any]:
        self.dismiss_tab_overview()
        if operation_id == "huawei_browser.control.settled_noop":
            # A control must not inherit an operation's delayed network/page
            # effects.  Resetting and loading fixture page 1 happens before
            # the measured reference windows; the no-op action itself remains
            # deliberately state-preserving.
            empty = self.reset_to_one_empty()
            return self.navigate(1, token=empty["layout_token"]).state
        if operation_id in {"huawei_browser.browser.open_fixed_page", "huawei_browser.browser.new_tab"}:
            return self.reset_to_one_empty()
        if operation_id in {
            "huawei_browser.browser.close_tab",
            "huawei_browser.browser.switch_tab",
        }:
            return self.prepare_two_fixture_tabs()
        if operation_id == "huawei_browser.browser.navigate_fixed_url":
            state = self.reset_to_one_empty()
            self.focus_address(state["layout_token"])
            return self.state(now_id("browser_prepare_network_check"))
        if operation_id in {
            "huawei_browser.browser.open_settings",
            "huawei_browser.browser.open_downloads_page",
        }:
            empty = self.reset_to_one_empty()
            return self.navigate(1, token=empty["layout_token"]).state
        if operation_id == "huawei_browser.browser.open_history_page":
            self.reset_to_one_empty()
            self.navigate(1, token=now_id("browser_prepare_history_page1"))
            page_2 = self.navigate(2, token=now_id("browser_prepare_history_page2"))
            self._history_depth = 1
            self._forward_depth = 0
            self._history_chain = (1, 2)
            page_2.state.update({"history_depth": 1, "forward_depth": 0, "history_chain": [1, 2]})
            return page_2.state
        if operation_id == "huawei_browser.browser.open_tab_overview":
            return self.prepare_two_fixture_tabs()
        if operation_id == "huawei_browser.browser.fixed_scroll":
            self.reset_to_one_empty()
            self.navigate(1, token=now_id("browser_prepare_page1"))
            return self.state(now_id("browser_prepare_check"))
        if operation_id == "huawei_browser.browser.refresh":
            self.reset_to_one_empty()
            self.navigate(1, token=now_id("browser_prepare_refresh"))
            return self.state(now_id("browser_prepare_refresh_check"))
        if operation_id in {"huawei_browser.browser.go_back", "huawei_browser.browser.go_forward"}:
            self.reset_to_one_empty()
            self.navigate(1, token=now_id("browser_prepare_history_page1"))
            self.navigate(2, token=now_id("browser_prepare_history_page2"))
            self._history_depth = 1
            self._forward_depth = 0
            self._history_chain = (1, 2)
            if operation_id == "huawei_browser.browser.go_back":
                return self.state(now_id("browser_prepare_back_check"))
            self.go_back(token=now_id("browser_prepare_forward_back"), expected_page=1)
            return self.state(now_id("browser_prepare_forward_check"))
        raise ValueError(f"unsupported formal Browser operation: {operation_id}")

    def prepare_two_fixture_tabs(self) -> dict[str, Any]:
        """Create the invariant two-tab state used by close and switch trials."""
        initial = self.reset_to_one_empty()
        page_1 = self.navigate(1, token=initial["layout_token"]).state
        page_1_tab = page_1.get("active_tab")
        if not page_1_tab:
            raise RuntimeError("fixture page 1 has no observable active tab")
        self.new_tab(token=page_1["layout_token"])
        empty_second_tab = self.wait_for(
            lambda value: value.get("tab_count") == 2 and value.get("active_tab") != page_1_tab,
            label="new empty second tab",
        )
        page_2 = self.navigate(2, token=empty_second_tab["layout_token"]).state
        page_2_tab = page_2.get("active_tab")
        if not page_2_tab or page_2_tab == page_1_tab:
            raise RuntimeError("fixture page 2 did not become a distinct active tab")
        self._fixture_tab_ids = {1: str(page_1_tab), 2: str(page_2_tab)}
        prepared = self.wait_for_fixture_page(2, expected_tab=str(page_2_tab), label="two fixed fixture tabs")
        tab_ids = set(prepared.get("tab_ids", []))
        if prepared.get("tab_count") != 2 or tab_ids != {str(page_1_tab), str(page_2_tab)}:
            raise RuntimeError(f"two fixture-tab preparation mismatch: {prepared}")
        return prepared

    def reset_to_one_empty(self) -> dict[str, Any]:
        self.dismiss_tab_overview()
        state = self.state(now_id("browser_reset"))
        while state.get("tab_count", 0) > 1:
            self.close_newest_tab(token=state["layout_token"], cleanup=True)
            state = self.state(now_id("browser_reset_loop"))
        if state.get("tab_count") != 1:
            raise RuntimeError(f"Browser reset expected one tab, got {state.get('tab_count')}")
        if state.get("page_loaded"):
            # Home is not a reliable state reset on this Browser build.  Open
            # a fresh empty tab, then close the old page by its left edge so
            # the measured trial starts from one genuinely empty tab.
            self.new_tab(token=state["layout_token"])
            state = self.wait_for(lambda value: value.get("tab_count") == 2, label="fresh empty tab")
            self.close_oldest_tab(token=state["layout_token"], cleanup=True)
            state = self.wait_for(lambda value: value.get("tab_count") == 1 and not value.get("page_loaded"), label="empty tab after old-page close")
        self._history_depth = 0
        self._forward_depth = 0
        self._history_chain = ()
        self._fixture_tab_ids = {}
        return self.state(now_id("browser_reset_done"))

    def action(self, operation_id: str, *, token: str) -> BackendActionResult:
        if operation_id == "huawei_browser.control.settled_noop":
            return BackendActionResult("pass", self.state(now_id("browser_control_noop")), {"action": "noop"}, source="hdc", is_mock=False)
        if operation_id == "huawei_browser.browser.open_fixed_page":
            return self.navigate(1, token=token)
        if operation_id == "huawei_browser.browser.new_tab":
            before = self.state(token)
            result = self.new_tab(token=token)
            state = self.wait_for(lambda value: value.get("tab_count") == before["tab_count"] + 1 and value.get("active_tab") != before.get("active_tab"), label="new tab count and selection")
            return BackendActionResult("pass", state, {**dict(result.event), "action": "new_tab"}, source="hdc", is_mock=False)
        if operation_id == "huawei_browser.browser.close_tab":
            page_1_tab = self._fixture_tab_id(1)
            page_2_tab = self._fixture_tab_id(2)
            before = self.state(token)
            if before.get("active_tab") != page_2_tab or before.get("tab_ids", [])[-1:] != [page_2_tab]:
                raise RuntimeError(f"close tab requires page-2 newest active tab: {before}")
            return self.close_newest_tab(
                token=token,
                expected_active_tab=page_1_tab,
                expected_page=1,
            )
        if operation_id == "huawei_browser.browser.switch_tab":
            return self.switch_to_tab(self._fixture_tab_id(1), token=token, expected_page=1)
        if operation_id == "huawei_browser.browser.navigate_fixed_url":
            return self.navigate_url(self.network_navigate_url, token=token, expected_host=self.navigate_host)
        if operation_id == "huawei_browser.browser.open_settings":
            return self.navigate_internal("hwbrowser://settings", token=token, expected_page="settings", marker_ids=("pc_settings_menu",))
        if operation_id == "huawei_browser.browser.open_history_page":
            return self.navigate_internal("hwbrowser://history", token=token, expected_page="history", marker_ids=("pc_title_in_history",))
        if operation_id == "huawei_browser.browser.open_downloads_page":
            return self.navigate_internal("hwbrowser://downloads", token=token, expected_page="downloads", marker_ids=("pc_title_in_download",))
        if operation_id == "huawei_browser.browser.open_tab_overview":
            before = self.state(token)
            result = self.click_control(token, "pc_search_icon", operation="Browser tab overview")
            state = self.wait_for_stable_state(
                lambda value: value.get("tab_overview_visible")
                and value.get("tab_count") == before.get("tab_count")
                and value.get("active_tab") == before.get("active_tab"),
                lambda value: (value.get("tab_count"), value.get("active_tab"), value.get("internal_page"), value.get("active_url")),
                label="Browser tab overview",
            )
            return BackendActionResult("pass", state, {**dict(result.event), "action": "open_tab_overview", "page_confirmation": "two_consecutive_observable_layout_states"}, source="hdc", is_mock=False)
        if operation_id == "huawei_browser.browser.fixed_scroll":
            return self.scroll_fixed(token=token)
        if operation_id == "huawei_browser.browser.refresh":
            return self.refresh(token=token)
        if operation_id == "huawei_browser.browser.go_back":
            return self.go_back(token=token, expected_page=1)
        if operation_id == "huawei_browser.browser.go_forward":
            return self.go_forward(token=token, expected_page=2)
        raise ValueError(operation_id)

    def confirm(self, operation_id: str, before: dict[str, Any], action_state: dict[str, Any]) -> BackendActionResult:
        if operation_id == "huawei_browser.control.settled_noop":
            ok = action_state.get("browser_running") and action_state.get("tab_count") == before.get("tab_count") and action_state.get("active_tab") == before.get("active_tab") and action_state.get("active_url") == before.get("active_url")
        elif operation_id == "huawei_browser.browser.open_fixed_page":
            ok = action_state.get("page_number") == 1 and action_state.get("page_loaded")
        elif operation_id == "huawei_browser.browser.new_tab":
            ok = action_state.get("tab_count") == before.get("tab_count", 0) + 1 and action_state.get("active_tab") != before.get("active_tab")
        elif operation_id == "huawei_browser.browser.close_tab":
            page_1_tab = self._fixture_tab_id(1)
            page_2_tab = self._fixture_tab_id(2)
            ok = (
                before.get("tab_count") == 2
                and before.get("active_tab") == page_2_tab
                and before.get("page_number") == 2
                and action_state.get("tab_count") == 1
                and action_state.get("active_tab") == page_1_tab
                and action_state.get("page_number") == 1
                and action_state.get("page_loaded")
            )
        elif operation_id == "huawei_browser.browser.switch_tab":
            page_1_tab = self._fixture_tab_id(1)
            page_2_tab = self._fixture_tab_id(2)
            ok = (
                before.get("tab_count") == 2
                and before.get("active_tab") == page_2_tab
                and before.get("page_number") == 2
                and action_state.get("tab_count") == 2
                and action_state.get("active_tab") == page_1_tab
                and action_state.get("page_number") == 1
                and action_state.get("page_loaded")
            )
        elif operation_id == "huawei_browser.browser.navigate_fixed_url":
            ok = self.host_matches(action_state.get("url_host"), self.navigate_host) and action_state.get("network_page_loaded") and not action_state.get("network_error")
        elif operation_id == "huawei_browser.browser.open_settings":
            ok = action_state.get("internal_page") == "settings" and "pc_settings_menu" in action_state.get("visible_ids", [])
        elif operation_id == "huawei_browser.browser.open_history_page":
            ok = action_state.get("internal_page") == "history" and "pc_title_in_history" in action_state.get("visible_ids", [])
        elif operation_id == "huawei_browser.browser.open_downloads_page":
            ok = action_state.get("internal_page") == "downloads" and "pc_title_in_download" in action_state.get("visible_ids", [])
        elif operation_id == "huawei_browser.browser.open_tab_overview":
            ok = (
                action_state.get("tab_overview_visible")
                and action_state.get("tab_count") == before.get("tab_count")
                and action_state.get("active_tab") == before.get("active_tab")
            )
        elif operation_id == "huawei_browser.browser.fixed_scroll":
            ok = action_state.get("page_number") == before.get("page_number") and action_state.get("page_loaded") and (action_state.get("heading_y") is None or before.get("heading_y") is None or action_state["heading_y"] < before["heading_y"] - 20)
        elif operation_id == "huawei_browser.browser.go_back":
            ok = (
                before.get("history_depth", 0) >= 1
                and before.get("history_chain") == [1, 2]
                and action_state.get("page_number") == 1
                and action_state.get("page_loaded")
                and action_state.get("forward_depth", 0) >= 1
            )
        elif operation_id == "huawei_browser.browser.go_forward":
            ok = (
                before.get("forward_depth", 0) >= 1
                and before.get("history_chain") == [1, 2]
                and action_state.get("page_number") == 2
                and action_state.get("page_loaded")
            )
        else:
            ok = False
        event = {"operation_id": operation_id, "confirmed": bool(ok), "before": before, "after_action": action_state, "confirmation_method": "observable_layout_state"}
        return BackendActionResult("pass" if ok else "fail", action_state, event, source="hdc", is_mock=False)


def run_trial(
    backend: HdcBackend,
    ui: BrowserUi,
    database: DatabaseStore,
    layout: DatasetLayout,
    adapter: HuaweiBrowserAdapter,
    operation_id: str,
    session_id: str,
    trial_id: str,
    round_index: int,
    *,
    capture_profile: CaptureProfile | None = None,
    sleep: Callable[[float], None] = time.sleep,
    schedule_position: int | None = None,
) -> dict[str, Any]:
    capture_profile = capture_profile or CaptureProfile()
    contract_metadata = COLLECTION_CONTRACT.to_metadata()
    operation = adapter.catalog.get(operation_id)
    if database.trial_exists(trial_id):
        raise ValueError(f"duplicate trial id: {trial_id}")
    machine = TrialStateMachine()
    timing = TimingRecorder(source="hdc", is_mock=False)
    started = timing.start_total()
    database.insert_trial(TrialRecord(trial_id, session_id, operation_id, "running", source="hdc", is_mock=False, current_state=machine.state.value, quality_status="INVALID"))
    captured: list[tuple[Any, Path, str]] = []
    quality_written = False
    expected_pid: str | None = None
    prepared: dict[str, Any] = {}
    baseline_ts: str | None = None
    action_start_ts: str | None = None
    action_end_ts: str | None = None
    post_ts: str | None = None
    action_duration_ms: float | None = None
    window_capture: ReferenceWindowCapture[Any] | None = None
    try:
        prepared = ui.prepare_for(operation_id)
        machine.advance(TrialState.PRECONDITION_CHECK)
        require_precondition(operation, prepared)
        database.insert_event(
            trial_id,
            "PRECONDITION_CHECK",
            timestamp(),
            {
                "operation_id": operation_id,
                "state": prepared,
                "confirmed": True,
                "capture_profile": capture_profile.to_metadata(),
                "collection_contract": contract_metadata,
            },
            source="hdc",
            is_mock=False,
        )

        # Preparation can navigate, create tabs, or populate history.  It is
        # not the measured action, so allow it to settle before arming either
        # reference window.  This isolates residual preparation work from the
        # action signature; the profile identity records the timing choice.
        if capture_profile.prepare_settle_ms:
            sleep(capture_profile.prepare_settle_ms / 1000.0)
            prepared = ui.state(now_id("browser_prepare_settle"))
            require_precondition(operation, prepared)
            database.insert_event(
                trial_id,
                "PREPARE_SETTLE",
                timestamp(),
                {
                    "operation_id": operation_id,
                    "duration_ms": capture_profile.prepare_settle_ms,
                    "state": prepared,
                    "confirmed": True,
                    "capture_profile": capture_profile.to_metadata(),
                    "collection_contract": contract_metadata,
                },
                source="hdc",
                is_mock=False,
            )

        def clear_current_process() -> None:
            nonlocal expected_pid
            expected_pid = arm_reference_window(backend, adapter.app_id, expected_pid, timing=timing)

        window_capture = ReferenceWindowCapture(clear_refs=clear_current_process, sleep=sleep)

        def capture_armed(phase: str) -> Any:
            nonlocal expected_pid, baseline_ts, post_ts
            item, path, expected_pid = window_capture.capture(
                phase,
                lambda: snapshot(
                    backend,
                    layout,
                    adapter.app_id,
                    trial_id,
                    phase,
                    expected_pid,
                    clear_before_capture=False,
                    timing=timing,
                ),
            )
            captured.append((item, path, expected_pid))
            database.insert_snapshot(item.snapshot_id, trial_id, item.phase, item.timestamp, str(path), source="hdc", is_mock=False)
            if phase == "BASELINE_1":
                baseline_ts = item.timestamp
            if phase == "POST_3000":
                post_ts = item.timestamp
            return item

        machine.advance(TrialState.BASELINE)
        for phase in capture_profile.baseline_phases:
            window_capture.arm(phase)
            window_capture.wait_ms(phase, capture_profile.baseline_window_ms)
            capture_armed(phase)
        machine.advance(TrialState.ACTION)
        before_action = ui.state(now_id("browser_before_action"))
        window_capture.arm(capture_profile.action_phase)
        action_started = time.perf_counter()
        action_start_ts = timestamp()
        action = ui.action(operation_id, token=before_action["layout_token"])
        action_end_ts = timestamp()
        action_duration_ms = (time.perf_counter() - action_started) * 1000.0
        if action.status == "needs_device_validation":
            raise HdcError("NEEDS_DEVICE_VALIDATION", str(action.event))
        if action.status != "pass":
            raise RuntimeError(f"Browser action returned {action.status}")
        machine.advance(TrialState.ACTION_CONFIRM)
        confirmation = ui.confirm(operation_id, before_action, dict(action.state))
        if confirmation.status != "pass":
            raise RuntimeError("observable action confirmation failed")
        database.insert_event(trial_id, "ACTION_CONFIRM", timestamp(), dict(confirmation.event), source="hdc", is_mock=False)
        action_item = capture_armed(capture_profile.action_phase)
        machine.advance(TrialState.POST_ACTION)
        post_items: list[Any] = []
        for phase, window_ms in capture_profile.post_windows:
            window_capture.arm(phase)
            window_capture.wait_ms(phase, window_ms)
            post_items.append(capture_armed(phase))
        machine.advance(TrialState.QUALITY_CHECK)
        with timing.measure("T_feature"):
            features = build_cross_app_feature_pair(
                sample_id=trial_id,
                app_id=adapter.app_id,
                operation_id=operation_id,
                baselines=[item for item, _, _ in captured[:2]],
                action=action_item,
                post_action=post_items[0],
                post_actions=post_items[1:],
                metadata={
                    "source": "hdc",
                    "is_mock": False,
                    "target": backend.target,
                    "pid": expected_pid,
                    "round": round_index,
                    "schedule_position": schedule_position,
                    "capture_profile": capture_profile.to_metadata(),
                    "collection_contract": contract_metadata,
                    "action_duration_ms": action_duration_ms,
                    "before_action": before_action,
                    "action_event": dict(action.event),
                    "confirmation_event": dict(confirmation.event),
                    "state_history": [item.value for item in machine.history],
                },
            )
        feature = features[0]
        if len(feature.raw_vector) != 2048 or len(features[1].raw_vector) != 8192 or any(item.signal_strength <= 0.0 for item in features):
            raise RuntimeError("feature has no usable signal")
        database.insert_event(trial_id, "QUALITY_CHECK", timestamp(), {"schema_signals": {item.schema_version: item.signal_strength for item in features}, "vector_dims": {item.schema_version: len(item.raw_vector) for item in features}}, source="hdc", is_mock=False)
        machine.advance(TrialState.SAVE)
        feature_paths: dict[str, str] = {}
        for item in features:
            feature_path = layout.write_derived_json("feature", feature_identifier(trial_id, item.schema_version), feature_payload(item))
            feature_paths[item.schema_version] = str(feature_path)
        database.insert_features(
            (trial_id, item.schema_version, feature_paths[item.schema_version], len(item.raw_vector), "hdc", False)
            for item in features
        )
        database.insert_quality(QualityRecord(trial_id, True, feature.signal_strength, source="hdc", is_mock=False, quality_status="VALID"))
        quality_written = True
        timing.finish_total(started)
        database.insert_timing(trial_id, timing.values, source="hdc", is_mock=False)
        database.finalize_trial_success(
            trial_id,
            state={"target": backend.target, "pid": expected_pid, "round": round_index, "schedule_position": schedule_position, "capture_profile": capture_profile.to_metadata(), "collection_contract": contract_metadata, "action_duration_ms": action_duration_ms, "window_events": window_capture.events, "state_history": [item.value for item in machine.history]},
            baseline_ts=baseline_ts,
            action_start_ts=action_start_ts,
            action_end_ts=action_end_ts,
            post_ts=post_ts,
        )
        return {"trial_id": trial_id, "operation_id": operation_id, "round": round_index, "schedule_position": schedule_position, "status": "success", "quality_status": "VALID", "signal_strength": feature.signal_strength, "feature_path": feature_paths[CROSS_APP_FEATURE_SCHEMA_VERSION], "feature_paths": feature_paths, "pid": expected_pid, "state_history": [item.value for item in machine.history], "timing": timing.values}
    except Exception as error:
        code = failure_code(error, machine.state)
        if machine.state != TrialState.SAVE:
            machine.fail()
        database.mark_trial_failed(trial_id, error_code=code.value, error_detail=str(error), state={"target": backend.target, "round": round_index, "schedule_position": schedule_position, "capture_profile": capture_profile.to_metadata(), "collection_contract": contract_metadata, "prepared": prepared, "pid": expected_pid, "window_events": window_capture.events if window_capture else [], "state_history": [item.value for item in machine.history]})
        if not quality_written:
            database.insert_quality(QualityRecord(trial_id, False, None, source="hdc", is_mock=False, quality_status="INVALID", error_code=code.value, reason=str(error)))
        database.insert_event(trial_id, "FAILED", timestamp(), {"error_code": code.value, "error_detail": str(error), "state_history": [item.value for item in machine.history], "window_events": window_capture.events if window_capture else [], "captured_snapshot_count": len(captured)}, source="hdc", is_mock=False)
        timing.finish_total(started)
        database.insert_timing(trial_id, timing.values, source="hdc", is_mock=False)
        return {"trial_id": trial_id, "operation_id": operation_id, "round": round_index, "status": "failed", "quality_status": "INVALID", "error_code": code.value, "error_detail": str(error), "state_history": [item.value for item in machine.history]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--bundle", default="com.huawei.hmos.browser")
    parser.add_argument("--process", default="com.huawei.hmos.browser")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--operations", default=",".join(DEFAULT_OPERATIONS))
    parser.add_argument("--fixture-base-url", required=True)
    parser.add_argument("--navigate-url", default="https://www.baidu.com/")
    parser.add_argument("--baseline-window-ms", type=int, default=750)
    parser.add_argument("--post-short-window-ms", type=int, default=750)
    parser.add_argument("--post-long-window-ms", type=int, default=3000)
    parser.add_argument("--prepare-settle-ms", type=int, default=0)
    parser.add_argument("--schedule-seed", type=int, default=20260914)
    parser.add_argument("--control-trials", type=int, default=10)
    parser.add_argument("--collection-stage", choices=("smoke", "pilot", "formal"), default="pilot")
    parser.add_argument("--formal-gate-report", type=Path)
    parser.add_argument("--stop-before-utc", help="optional ISO-8601 UTC cutoff; remaining planned trials become auditable INVALID records")
    parser.add_argument("--root", default=".")
    parser.add_argument("--hdc", nargs="+", metavar="HDC")
    args = parser.parse_args(argv)
    if not 1 <= args.rounds <= 25:
        parser.error("--rounds must be between 1 and 25")
    operation_ids = tuple(item.strip() for item in args.operations.split(",") if item.strip())
    if not operation_ids:
        parser.error("at least one operation is required")
    if args.control_trials not in {0, args.rounds}:
        parser.error("--control-trials must be 0 or equal --rounds so no-op controls stay balanced")
    if min(args.baseline_window_ms, args.post_short_window_ms, args.post_long_window_ms, args.prepare_settle_ms) < 0:
        parser.error("capture window durations must not be negative")
    if args.collection_stage == "formal":
        if args.rounds != 20:
            parser.error("formal collection requires exactly 20 valid-trial attempts per frozen operation")
        if args.formal_gate_report is None or not args.formal_gate_report.exists():
            parser.error("formal collection requires an existing stable-set gate report via --formal-gate-report")
    try:
        stop_before_utc = parse_stop_before_utc(args.stop_before_utc)
    except ValueError as error:
        parser.error(str(error))
    capture_profile = CaptureProfile(
        name="cross_app.reference_windows.v2_settled" if args.prepare_settle_ms else "cross_app.reference_windows.v1",
        baseline_window_ms=args.baseline_window_ms,
        post_short_window_ms=args.post_short_window_ms,
        post_long_window_ms=args.post_long_window_ms,
        prepare_settle_ms=args.prepare_settle_ms,
    )
    scheduled_ids = (*operation_ids, "huawei_browser.control.settled_noop") if args.control_trials else operation_ids
    schedule = balanced_trial_schedule(scheduled_ids, rounds=args.rounds, seed=args.schedule_seed)
    backend = HdcBackend(hdc=normalize_hdc_argument(args.hdc), target=args.target)
    adapter = HuaweiBrowserAdapter()
    layout = DatasetLayout(args.root)
    database = DatabaseStore(Path(args.root) / "database/vma_workload.sqlite")
    session_id = now_id("browser_real_vma")
    results: list[dict[str, Any]] = []
    try:
        targets = backend.discover_targets()
        if targets != (args.target,):
            raise SystemExit(json.dumps({"status": "blocked", "error": "HDC target selection is not unique or changed", "targets": targets, "requested": args.target}, ensure_ascii=False))
        permissions = backend.check_permissions()
        backend.register_validated_app(adapter.app_id, bundle=args.bundle, process=args.process)
        pid = backend.ensure_process_alive(adapter.app_id)
        ui = BrowserUi(backend, args.bundle, args.fixture_base_url, args.navigate_url)
        initial = ui.state(now_id("browser_gate2_layout"))
        require_initial_browser_ui(initial, args.bundle)
        network_operation_ids = [operation_id for operation_id in operation_ids if adapter.catalog.get(operation_id).requires_network]
        network_preflight: dict[str, Any] | None = None
        if network_operation_ids:
            network_preflight = probe_network(backend, args.navigate_url)
            if not network_preflight["passed"]:
                raise SystemExit(
                    json.dumps(
                        {
                            "status": "blocked",
                            "error": "NETWORK_UNAVAILABLE",
                            "operations": network_operation_ids,
                            "network_preflight": network_preflight,
                        },
                        ensure_ascii=False,
                    )
                )
        database.insert_catalog(adapter.catalog)
        database.insert_session(SessionRecord(session_id, adapter.app_id, timestamp(), "hdc", False, {"target": backend.target, "bundle": args.bundle, "process": args.process, "initial_pid": pid, "collection_stage": args.collection_stage, "rounds": args.rounds, "operation_ids": list(operation_ids), "control_operation_id": "huawei_browser.control.settled_noop" if args.control_trials else None, "control_trials": args.control_trials, "schedule_seed": args.schedule_seed, "schedule": [{"round": item.round_index, "position": item.position, "operation_id": item.operation_id} for item in schedule], "capture_profile": capture_profile.to_metadata(), "collection_contract": COLLECTION_CONTRACT.to_metadata(), "fixture_base_url": args.fixture_base_url, "navigate_url": args.navigate_url, "navigate_host": urlparse(args.navigate_url).hostname, "network_preflight": network_preflight, "stop_before_utc": stop_before_utc.isoformat() if stop_before_utc else None, "collection_design": "balanced randomized order with no-op controls; v2 and signed v3 dual-write"}))
        print(json.dumps({"status": "ready", "target": backend.target, "permissions": permissions, "browser_pid": pid, "initial_state": initial, "operations": list(operation_ids), "rounds": args.rounds, "schedule_seed": args.schedule_seed, "capture_profile": capture_profile.to_metadata(), "collection_contract": COLLECTION_CONTRACT.to_metadata(), "network_preflight": network_preflight}, ensure_ascii=False))
        stopped_at_cutoff = False
        for schedule_index, scheduled in enumerate(schedule):
            if stop_before_utc is not None and datetime.now(timezone.utc) >= stop_before_utc:
                stopped_at_cutoff = True
                for remaining in schedule[schedule_index:]:
                    operation_id = remaining.operation_id
                    trial_id = f"{session_id}_r{remaining.round_index:02d}_p{remaining.position:02d}_{operation_id.rsplit('.', 1)[-1]}"
                    result = record_time_cutoff_trial(
                        database,
                        session_id=session_id,
                        operation_id=operation_id,
                        trial_id=trial_id,
                        round_index=remaining.round_index,
                        schedule_position=remaining.position,
                        stop_before_utc=stop_before_utc,
                    )
                    results.append(result)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                break
            operation_id = scheduled.operation_id
            trial_id = f"{session_id}_r{scheduled.round_index:02d}_p{scheduled.position:02d}_{operation_id.rsplit('.', 1)[-1]}"
            result = run_trial(backend, ui, database, layout, adapter, operation_id, session_id, trial_id, scheduled.round_index, capture_profile=capture_profile, schedule_position=scheduled.position)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if result["status"] != "success" and result.get("error_code") in {"HDC_DISCONNECTED", "HDC_TIMEOUT", "PROCESS_NOT_FOUND", "PROCESS_AMBIGUOUS", "PROCESS_RESTARTED", "PERMISSION_DENIED"}:
                raise RuntimeError(f"device became unavailable after {trial_id}: {result}")
        success = sum(item["status"] == "success" for item in results)
        print(json.dumps({"status": "completed", "target": backend.target, "session_id": session_id, "permissions": permissions, "trials_requested": len(schedule), "success": success, "failed": len(results) - success, "stopped_at_cutoff": stopped_at_cutoff, "results": results}, ensure_ascii=False, indent=2))
        return 0 if success == len(results) else 2
    finally:
        backend.close()
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
