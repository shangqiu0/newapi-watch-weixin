"""Raster plots from one aggregate snapshot; no untrusted names are drawn into images."""

import io
from pathlib import Path
from typing import Literal

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties

from .analytics import Snapshot
from .money import CurrencyConfig

ChartKind = Literal["trend", "ranking"]
METRIC_FIELDS = {
    "quota": "quota",
    "calls": "success_records",
    "errors": "error_records",
    "tokens": "tokens",
}


def render(
    snapshot: Snapshot,
    kind: ChartKind,
    font: Path | None = None,
    demo: bool = False,
    currency: CurrencyConfig | None = None,
) -> bytes:
    figure = Figure(figsize=(9, 4.6), dpi=130, facecolor="#f5f7fa")
    axis = figure.subplots()
    metric = snapshot.query.metric
    field = METRIC_FIELDS[metric]
    in_cny = metric == "quota" and currency is not None and currency.confirmed

    def value(amount: int) -> float:
        return (
            float(currency.amount(amount, precision=12) or "0")
            if in_cny and currency is not None
            else float(amount)
        )

    unit = "CNY (¥)" if in_cny else f"{metric} (raw units)"
    if kind == "ranking":
        labels = [f"#{index + 1}" for index in range(len(snapshot.ranking))]
        values = [value(getattr(item, field)) for item in snapshot.ranking]
        axis.barh(labels[::-1], values[::-1], color="#237a73")
        axis.set_xlabel(unit)
        axis.set_ylabel(f"{snapshot.query.dimension} ranking (see text legend)")
    else:
        labels = [
            item.label[11:16] + item.label[-6:] if "T" in item.label else item.label[5:]
            for item in snapshot.series
        ]
        values = [value(getattr(item, field)) for item in snapshot.series]
        axis.bar(range(len(values)), values, color="#237a73", width=0.68)
        step = max(1, (len(values) + 9) // 10)
        axis.set_xticks(range(0, len(values), step), labels[::step], rotation=35)
        axis.set_ylabel(unit)
        axis.set_xlabel(snapshot.timezone)
    title = f"{'DEMO | ' if demo else ''}NewAPI | {snapshot.query.start} - {snapshot.query.end} | {kind}"
    if not snapshot.coverage.complete:
        title += " | PARTIAL DATA"
    axis.set_title(
        title, loc="left", fontproperties=FontProperties(fname=str(font)) if font else None
    )
    axis.spines[["top", "right"]].set_visible(False)
    axis.grid(axis="y", alpha=0.16)
    axis.set_axisbelow(True)
    figure.tight_layout()
    output = io.BytesIO()
    FigureCanvasAgg(figure).print_png(
        output, metadata={"Description": "DEMO" if demo else "NewAPI statistics"}
    )  # type: ignore[no-untyped-call]
    figure.clear()
    return output.getvalue()
