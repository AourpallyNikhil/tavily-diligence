"""Console rendering. Labels, controls, data -- nothing else."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .findings import Status
from .pipeline import Report

TIER_STYLE = {1: "green", 2: "cyan", 3: "yellow"}


def render(report: Report, console: Console) -> None:
    header = Text(report.vendor, style="bold")
    if report.vendor_domain:
        header.append(f"  {report.vendor_domain}", style="dim")
    console.print()
    console.print(header)
    console.print()

    for result in report.results:
        table = Table(
            title=result.dimension.title,
            title_style="bold",
            title_justify="left",
            show_lines=False,
            expand=True,
        )
        table.add_column("", width=3)
        table.add_column("Finding", ratio=3)
        table.add_column("Sources", ratio=2)

        if not result.findings:
            table.add_row(
                Text("—", style="dim"),
                Text("No finding established", style="dim italic"),
                Text("", style="dim"),
            )
        for f in result.findings:
            if f.status is Status.SUPPORTED:
                mark = (
                    Text("✓", style="bold green")
                    if f.provenance_ok
                    else Text("~", style="bold yellow")
                )
                claim = Text(f.claim)
                srcs = Text()
                for eid in f.evidence_ids:
                    ev = next((e for e in result.pool if e.id == eid), None)
                    if ev is None:
                        continue
                    srcs.append(f"[{ev.role}] ", style=TIER_STYLE.get(ev.tier, ""))
                    srcs.append(f"{ev.url}\n", style="dim")
            else:
                mark = Text("?", style="bold yellow")
                claim = Text(f.claim, style="dim")
                srcs = Text(f.demoted_reason or "unverified", style="dim yellow")
            table.add_row(mark, claim, srcs)

        console.print(table)
        console.print()

    supported = sum(len(r.supported) for r in report.results)
    weak = sum(1 for r in report.results for f in r.supported if not f.provenance_ok)
    unverified = sum(len(r.unverified) for r in report.results)
    evidence = sum(len(r.pool) for r in report.results)

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column()
    summary.add_row("supported", str(supported))
    summary.add_row("weak provenance", str(weak))
    summary.add_row("unverified", str(unverified))
    summary.add_row("evidence", str(evidence))
    summary.add_row("llm calls", str(report.usage.calls))
    summary.add_row(
        "tokens", f"{report.usage.prompt_tokens} in / {report.usage.completion_tokens} out"
    )
    summary.add_row("elapsed", f"{report.elapsed_s:.1f}s")
    console.print(summary)
    console.print()
