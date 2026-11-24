"""Built-in mode catalog (0.11).

Maps historical goals + the default trace pipeline onto ModeSpec entries.
Stage ids for ``trace`` mirror the existing reservation chain gates so
verdict.json / REPORT.md stay aligned.
"""

from __future__ import annotations

from .base import ModeRegistry, ModeSpec, StageSpec

# Reservation chain stages already used by synthesize_verdict / REPORT.md
TRACE_STAGES = (
    StageSpec("ARRIVAL", "Channel inbound / booking-gateway accept path"),
    StageSpec("INVENTORY", "Inventory / listing visibility (often follow-only)"),
    StageSpec("ACCEPTANCE", "CM acceptance / processor ack"),
    StageSpec("CM_STORE", "Hub / channel-store persistence"),
    StageSpec("PMS_RECEIVE", "PMS hub receive / booking-events"),
    StageSpec("CONVERSION", "Mapping / conversion faults"),
    StageSpec("DELIVERY", "Terminal success / delivered markers"),
)

# ARI discrepancy methodology (generic names) — stages recorded for design;
# 0.11 still runs the existing ``ari`` task. Branching engine is a later patch.
ARI_DISCREPANCY_STAGES = (
    StageSpec("DISCOVERY", "Route catalog + blob pools"),
    StageSpec("SENT", "CM outbound value vs expected for stay date"),
    StageSpec("RECEIVED", "Bridge / PMS receive of room-rate push"),
    StageSpec("OTA_FETCH", "Channel-side retrieval / visibility"),
    StageSpec("ACTOR", "Who wrote the value (GUI vs system)"),
    StageSpec("DERIVATION", "Base/derived rate relationship in window"),
)


def build_default_registry() -> ModeRegistry:
    reg = ModeRegistry()

    reg.register(
        ModeSpec(
            mode_id="trace",
            task_ids=("trace",),
            required_anchors=("PT_REF",),
            optional_anchors=("PT_CHANNEL", "PT_PMS", "PT_AT", "PT_FROM", "PT_TO"),
            stages=TRACE_STAGES,
            description="OTA → CM → PMS reservation path investigator",
        )
    )
    reg.register(
        ModeSpec(
            mode_id="fidelity",
            task_ids=("fidelity",),
            required_anchors=(),
            optional_anchors=("PT_REF",),
            description="OTA vs PMS document field preservation",
        )
    )
    reg.register(
        ModeSpec(
            mode_id="ari",
            task_ids=("ari",),
            required_anchors=(),
            optional_anchors=("PT_CHANNEL", "PT_AT", "PT_FROM", "PT_TO", "PT_STAY_DATES"),
            description="ARI / rate push evidence sweeps",
        )
    )
    reg.register(
        ModeSpec(
            mode_id="ari-discrepancy",
            task_ids=("ari",),
            required_anchors=("PT_CHANNEL", "PT_AT", "PT_FROM", "PT_TO", "PT_STAY_DATES"),
            optional_anchors=("PT_HOTEL_UUID", "PT_TARGET_VALUES", "PT_PROPERTY_ID"),
            stages=ARI_DISCREPANCY_STAGES,
            description="Causal rate discrepancy graph (SENT→RECEIVED→OTA / ACTOR)",
            legacy_task_bridge=True,
        )
    )
    reg.register(
        ModeSpec(
            mode_id="guiattrib",
            task_ids=("guiattrib",),
            required_anchors=(),
            optional_anchors=("PT_AT", "PT_FROM", "PT_TO"),
            description="GUI actor attribution from link-api / session / audit",
        )
    )
    reg.register(
        ModeSpec(
            mode_id="kb-test",
            task_ids=("kb-test",),
            required_anchors=(),
            description="KB vs live catalog reachability",
        )
    )
    reg.register(
        ModeSpec(
            mode_id="bridgeflush",
            task_ids=("bridgeflush",),
            required_anchors=("PT_PROPERTY_ID", "PT_AT"),
            optional_anchors=("PT_CLOUDPMS_PROPERTY_ID", "PT_FROM", "PT_TO", "PT_REGION"),
            description="CloudPMS / bridge flush blocker isolation",
        )
    )

    reg.register(
        ModeSpec(
            mode_id="overbooking",
            task_ids=("ari",),
            required_anchors=(
                "PT_CHANNEL",
                "PT_AT",
                "PT_FROM",
                "PT_TO",
                "PT_PROPERTY_ID",
                "PT_HOTEL_UUID",
                "PT_STAY_DATES",
            ),
            optional_anchors=("PT_REF", "PT_BOOKING_REF"),
            description="Goal alias: overbooking-oriented ARI sweeps (0.10 compatible)",
        )
    )
    reg.register(
        ModeSpec(
            mode_id="missing-reservation",
            task_ids=("trace",),
            required_anchors=("PT_REF", "PT_AT", "PT_FROM", "PT_TO", "PT_CHANNEL"),
            stages=TRACE_STAGES,
            description="Goal alias: missing reservation path",
        )
    )
    reg.register(
        ModeSpec(
            mode_id="rate-mismatch",
            task_ids=("ari",),
            required_anchors=(
                "PT_CHANNEL",
                "PT_AT",
                "PT_FROM",
                "PT_TO",
                "PT_TARGET_VALUES",
                "PT_STAY_DATES",
                "PT_HOTEL_UUID",
            ),
            stages=ARI_DISCREPANCY_STAGES,
            description="Goal alias: rate mismatch (bridges toward ari-discrepancy)",
        )
    )
    reg.register(
        ModeSpec(
            mode_id="delivery-failure",
            task_ids=("trace", "fidelity"),
            required_anchors=("PT_REF", "PT_AT", "PT_FROM", "PT_TO"),
            stages=TRACE_STAGES,
            description="Goal alias: delivery failure + fidelity",
        )
    )
    reg.register(
        ModeSpec(
            mode_id="billing-trace",
            task_ids=("ari",),
            required_anchors=("PT_PROPERTY_ID", "PT_BILLING_FROM", "PT_BILLING_TO"),
            description="Goal alias: billing window ARI",
        )
    )
    reg.register(
        ModeSpec(
            mode_id="bridge-flush",
            task_ids=("bridgeflush",),
            required_anchors=(
                "PT_PROPERTY_ID",
                "PT_CLOUDPMS_PROPERTY_ID",
                "PT_AT",
                "PT_FROM",
                "PT_TO",
                "PT_REGION",
            ),
            description="Goal alias: bridge-flush (hyphen form)",
        )
    )

    return reg


# Process-wide default registry
DEFAULT_REGISTRY = build_default_registry()
